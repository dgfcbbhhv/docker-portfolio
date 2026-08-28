"""每日收盘后价值投资选股。

流程:
1. 候选池 = config.watch_list, 已持有名单 = config.portfolio
2. 用 build_portfolio_df(扩展池) 计算评分 / Kelly / 估值
3. 按规则分类: 可建仓 / 可加仓 / 可关注 / 不推送
4. 生成带可溯源字段的理由文案

规则阈值来自 config.json daily_push 段。
数据缺失一律写"数据不足"，不做推断填充。
"""
import datetime
import logging

logger = logging.getLogger(__name__)

from modules.config import DAILY_PUSH, PORT_A, PORT_HK, PORT_REIT, PORT_US, WATCH_LIST
from modules.cost_tier import calc_overall_tier
from modules.holdings import get_actual_holdings
from modules.portfolio_engine import build_portfolio_df


def _held_codes():
    """已持有代码：真实持仓优先；无真实持仓源时回退到目标组合（视为规划持有）。"""
    holdings = get_actual_holdings()
    if holdings:
        return {code: True for code in (holdings.get("positions") or {})}
    held = {}
    for mapping in (PORT_A, PORT_HK, PORT_US, PORT_REIT):
        for code in mapping:
            held[code] = True
    return held


def _prep_holdings(held, df):
    """预计算真实持仓市值占比映射，供 _classify/_reason_lines 使用。

    有真实持仓时返回:
      {"positions": {...}, "cash":..., "_per": {code: 市值}, "_mv_total": 总市值}
    否则返回 {}（回退目标占比语义）。
    """
    holdings = get_actual_holdings()
    if not holdings:
        return {}
    positions = holdings.get("positions") or {}
    cash = holdings.get("cash") or 0
    price_by = {}
    for _, r in df.iterrows():
        try:
            p = float(r.get("price", 0) or 0)
        except (TypeError, ValueError):
            p = 0.0
        if p > 0:
            price_by[r["code"]] = p
    per = {}
    mv_total = 0.0
    for code, pos in positions.items():
        p = price_by.get(code)
        if not p:
            p = pos.get("avg_cost") or 0
        mv = (pos.get("shares") or 0) * p
        per[code] = mv
        mv_total += mv
    holdings["_per"] = per
    holdings["_mv_total"] = mv_total
    holdings["cash"] = cash
    return holdings


def _effective_ratio(code, row, holdings):
    """解析"占比"与标签。

    真实持仓存在时按市值权重（预计算 _per/_mv_total）计算；否则用目标组合的
    **未归一化原始 ratio**（配置里的绝对权重，非 sum=100 的 ratio_norm）。

    返回 (ratio_pct, is_actual)。
    """
    positions = (holdings or {}).get("positions") or {}
    per = (holdings or {}).get("_per") or {}
    mv_total = (holdings or {}).get("_mv_total") or 0
    cash = (holdings or {}).get("cash") or 0
    if code in positions and per.get(code):
        if mv_total + cash > 0:
            return round(per[code] / (mv_total + cash) * 100.0, 1), True
    raw = row.get("ratio")
    if raw is None:
        raw = row.get("ratio_norm", 0) or 0
    return float(raw or 0), False


def _market_for_code(code):
    """由 watch_list 判断股票市场: A/HK/US/REIT。"""
    w = WATCH_LIST.get(code, {})
    mkt = str(w.get("market", "")).upper()
    if mkt in ("A", "HK", "US"):
        return mkt
    if code in PORT_REIT:
        return "REIT"
    # 兜底: 由代码形态判断
    if code.isdigit():
        if code.startswith(("60", "00", "30", "68", "50")):
            return "A"
        return "HK"
    return "US"


def _fmt(v, nd=1, suffix=""):
    """None/0 返回 '数据不足'。"""
    if v is None or (isinstance(v, (int, float)) and (v != v or v == 0)):
        return "数据不足"
    try:
        return f"{float(v):.{nd}f}{suffix}"
    except (ValueError, TypeError):
        return "数据不足"


def _build_watch_pool():
    """把 watch_list 全量作为扩展池, 计算每只股票的评分/Kelly/估值。

    已持有的股票沿用 config.portfolio 的真实配置(名称+仓位), 保证
    ratio_norm / Kelly 空间计算与主界面一致; 未持有的按 ratio=0 处理。
    """
    port_cfg = {}
    for mapping in (PORT_A, PORT_HK, PORT_REIT, PORT_US):
        for code, cfg in mapping.items():
            port_cfg[code] = cfg
    pm = {"A": {}, "HK": {}, "REIT": {}, "US": {}}
    for code, info in WATCH_LIST.items():
        mkt = _market_for_code(code)
        if mkt not in pm:
            continue
        held = port_cfg.get(code)
        if held is not None:
            pm[mkt][code] = {"name": held.get("name", code),
                             "ratio": float(held.get("ratio", 0) or 0)}
        else:
            pm[mkt][code] = {"name": info.get("name", code), "ratio": 0}
    df, meta = build_portfolio_df(cfg=None, portfolio_map=pm)
    return df, meta


def _tier_text(code, current_price, held):
    """成本档位文案。未持有/无成本数据按 当前价=成本 估算。"""
    if not held:
        return "未持有，无成本记录；按当前价估算成本"
    info = calc_overall_tier(code, current_price)
    tier = info.get("tier", "数据不足")
    if tier == "数据不足":
        return "数据不足（无成本或分红数据）"
    cost_dy = info.get("cost_dy")
    cur_dy = info.get("current_dy")
    parts = [f"成本档位: {tier}"]
    if cost_dy is not None and cur_dy is not None:
        parts.append(f"(成本股息率 {cost_dy}% vs 当前 {cur_dy}%)")
    return " ".join(parts)


def _classify(row, held, buy_th, watch_th, add_gap, holdings=None):
    """返回 (category, reason_lines)。

    category: 减仓 / 可建仓 / 可加仓 / 可关注（持有）/ 不推送
    holdings: 真实持仓上下文（见 _effective_ratio），None 表示无真实持仓。
    """
    total = float(row.get("lishing", 0) or 0)
    flags = str(row.get("lishing_flags", "") or "").strip()
    growth_path = bool(row.get("growth_factors"))
    price = float(row.get("price", 0) or 0)

    # 数据不足 / 硬红线(一票否决) → 不推送
    # 硬红线(审计异常/ST/连续两年现金流为负)在 lishing 中 total=0, 由 total<=0 拦截。
    # 软评分 flag(PB评分/净利低增长等) total>0, 不再阻断建仓, 仅作风险提示(见 _reason_lines)。
    if total <= 0 or price <= 0:
        return "不推送", ["数据不足或触发硬红线，无法评估"]

    kelly = float(row.get("kelly_final", 0) or 0)
    ratio, _is_actual = _effective_ratio(row["code"], row, holdings)
    gap = kelly - ratio

    if not held:
        # 建仓: 评分≥买入线 且 Kelly>0
        if total >= buy_th and kelly > 0:
            return "可建仓", [f"未持有，评分 {total:.0f} 达标、Kelly {kelly:.1f}%，符合建仓条件"]
        if total >= watch_th:
            return "可关注", [f"未持有，评分 {total:.0f}，Kelly {kelly:.1f}% 未达标建仓"]
        return "不推送", [f"评分 {total:.0f} 低于关注线 {watch_th:.0f}"]

    # 已持有
    tier = calc_overall_tier(row["code"], price).get("tier", "数据不足")
    # 减仓：成本档位警戒，或评分跌破关注线，或"超配"（仅当 Kelly 真实可知）
    # 注意：Kelly 缺失(<=0)时不触发超配降仓，避免用数据缺口制造噪声减仓信号
    if tier == "警戒" or total < watch_th or (kelly > 0 and gap < -add_gap):
        reason = []
        if tier == "警戒":
            reason.append(f"成本档位为警戒")
        if total < watch_th:
            reason.append(f"评分 {total:.0f} 低于关注线 {watch_th:.0f}")
        if kelly > 0 and gap < -add_gap:
            reason.append(f"建议仓位 {kelly:.1f}% 低于当前 {ratio:.1f}%（超配 {abs(gap):.1f}%）")
        return "减仓", [f"建议逢高减仓：{'；'.join(reason)}"]
    # 加仓：评分达标且有加仓空间
    if total >= buy_th and gap > add_gap:
        return "可加仓", [f"评分达标，加仓空间 {gap:.1f}%（Kelly {kelly:.1f}% vs 当前 {ratio:.1f}%）"]
    # 已持有、评分尚可但加仓空间不足 → 持有观察
    if total >= watch_th:
        return "可关注", [f"评分 {total:.0f} 达标，但加仓条件未满足（空间 {gap:.1f}%），持有观望"]
    return "不推送", [f"评分 {total:.0f} 低于关注线 {watch_th:.0f}"]


def _reason_lines(row, held, category, market, add_gap, holdings=None):
    """生成理由条目列表。"""
    total = float(row.get("lishing", 0) or 0)
    price = float(row.get("price", 0) or 0)
    kelly = float(row.get("kelly_final", 0) or 0)
    ratio, is_actual = _effective_ratio(row["code"], row, holdings)
    ratio_label = "当前占比" if is_actual else "目标占比"

    lines = []
    if market == "US":
        lines.append(f"- 成长评分 {_fmt(total, 0)}/100，基于成长因子评估")
    else:
        flags_line = str(row.get("lishing_flags", "") or "").strip()
        if flags_line:
            lines.append(f"- 李式评分 {_fmt(total, 0)}/100，⚠ 不达标项: {flags_line}")
        else:
            lines.append(f"- 李式评分 {_fmt(total, 0)}/100，未触发红旗")
    pe = _fmt(row.get("pe"), 1)
    pb = _fmt(row.get("pb"), 1)
    dy = _fmt(row.get("dy"), 1)
    lines.append(f"- PE {pe}、PB {pb}，股息率 {dy}%")
    roe = _fmt(row.get("roe"), 1)
    debt = _fmt(row.get("debt"), 1)
    ocf = _fmt(row.get("ocf_ratio"), 1)
    lines.append(f"- ROE {roe}%，资产负债率 {debt}%，经营现金流/净利润 {ocf}")

    try:
        from modules.valuation import push_lines, valuate
        lines.extend(push_lines(valuate(row)))
    except Exception as _e:
        logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    if category in ("可加仓", "可关注"):
        lines.append(f"- Kelly 参考仓位 {_fmt(kelly, 1)}%，{ratio_label} {_fmt(ratio, 1)}%")
        if held:
            lines.append(_tier_text(row["code"], price, True))
    elif category == "可建仓":
        lines.append(_tier_text(row["code"], price, False))
    return lines


def _classify_reit(code, held):
    """REIT 独立分类: 分派率+估值分位+趋势综合分 (REIT 无 ROE/EPS, 不适用 L氏)。

    综合分 = 分派率(40) + 估值分位(30) + 均线趋势(30)。返回 (category, signal_dict|None)。
    """
    from modules.reit_monitor import reit_signal
    s = reit_signal(code)
    if s is None:
        return "不推送", None
    score = s["lishing_score"]
    dy = s["div_yield"] or 0
    if not held:
        if score >= 70 and dy >= 3:
            return "可建仓", s
        if score >= 55:
            return "可关注", s
        return "不推送", s
    if score < 40:
        return "减仓", s
    return "可关注", s


def _reit_reason_lines(cat, s):
    """REIT 分类理由文案。"""
    score = s["lishing_score"]
    dy = s["div_yield"] or 0
    pctl = s["pe_pctl"]
    base = (f"综合分 {score:.0f}（分派率 {dy:.2f}% + 估值分位 {pctl:.0f} + 均线趋势）")
    if cat == "可建仓":
        lines = [f"未持有，{base}，符合建仓条件"]
    elif cat == "减仓":
        lines = [f"建议逢高减仓：{base} 恶化"]
    else:
        lines = [f"{base}，持有观察"]
    flags = "、".join(s.get("lishing_flags") or [])
    if flags:
        lines.append(f"⚠ 不达标项: {flags}")
    return lines


def build_picks(include_announcements=True):
    """生成每日选股结果。

    Returns:
        dict: {
            "date": "YYYY-MM-DD",
            "market": "AH" | "US",
            "picks": [{"code","name","category","held","market","lines":[..],"announcement": {...|None}}],
            "skipped": [code...]   # 数据不足被跳过
        }
    """
    held = _held_codes()
    df, meta = _build_watch_pool()
    if df is None or df.empty:
        return {"date": datetime.date.today().isoformat(), "picks": [],
                "skipped": list(WATCH_LIST.keys()), "meta": meta}
    holdings = _prep_holdings(held, df)

    buy_th = DAILY_PUSH["lishing_buy"]
    watch_th = DAILY_PUSH["lishing_watch"]
    add_gap = DAILY_PUSH["add_gap"]

    picks = []
    skipped = []
    blocked_meta = list(meta.get("blocked") or [])  # 保留外部注入, 避免覆盖

    # REIT 独立处理: 不依赖 build_portfolio_df 的 REIT 行 (其分红源东财接口不稳定),
    # 评分走 reit_signal (天天基金分红 + 腾讯行情 + 历史分位/均线)。
    for code in [c for c in PORT_REIT if c in WATCH_LIST]:
        is_held = code in held
        rcat, rs = _classify_reit(code, is_held)
        if rcat == "不推送":
            skipped.append(code)
            continue
        picks.append({
            "code": code,
            "name": (WATCH_LIST.get(code, {}) or {}).get("name", code),
            "category": rcat,
            "held": is_held,
            "market": "REIT",
            "price": rs["price"],
            "kelly_final": rs["kelly_final"],
            "risk_flags": "、".join(rs.get("lishing_flags") or []),
            "lines": _reit_reason_lines(rcat, rs),
            "announcement": None,
        })

    for _, r in df.iterrows():
        code = r["code"]
        market = _market_for_code(code)
        is_held = code in held
        cat, _r = _classify(r, is_held, buy_th, watch_th, add_gap, holdings=holdings)
        if cat == "不推送":
            if float(r.get("lishing", 0) or 0) <= 0 or float(r.get("price", 0) or 0) <= 0:
                skipped.append(code)
                continue
            # 评分达标但被红旗/其他条件拦截 → 记录供 6 个月可信度验证
            flags = str(r.get("lishing_flags", "") or "").strip()
            if flags and float(r.get("lishing", 0) or 0) >= watch_th:
                blocked_meta.append({
                    "code": code,
                    "name": r.get("name", code),
                    "market": market,
                    "held": is_held,
                    "category": "红旗拦截",
                    "price": float(r.get("price", 0) or 0),
                    "kelly_final": float(r.get("kelly_final", 0) or 0),
                    "lishing": float(r.get("lishing", 0) or 0),
                    "flags": flags,
                })
            continue
        lines = _reason_lines(r, is_held, cat, market, add_gap, holdings=holdings)
        ann = None
        if include_announcements and market == "A":
            ann = _fetch_announcement(code)
            if ann:
                lines.append(_announcement_line(ann))
        picks.append({
            "code": code,
            "name": r.get("name", code),
            "category": cat,
            "held": is_held,
            "market": market,
            "price": float(r.get("price", 0) or 0),
            "kelly_final": float(r.get("kelly_final", 0) or 0),
            "lishing": float(r.get("lishing", 0) or 0),
            "mos": r.get("mos"),
            "pe": r.get("pe"),
            "pb": r.get("pb"),
            "roe": r.get("roe"),
            "dy": r.get("dy"),
            "risk_flags": str(r.get("lishing_flags", "") or "").strip(),
            "lines": lines,
            "announcement": ann,
        })
    meta = dict(meta)
    meta["blocked"] = blocked_meta
    return {"date": datetime.date.today().isoformat(), "picks": picks,
            "skipped": skipped, "meta": meta, "_df": df}


def _fetch_announcement(code):
    from modules.data.announcements import latest
    end = datetime.date.today().isoformat()
    begin = (datetime.date.today()
             - datetime.timedelta(days=DAILY_PUSH["announcement_days"])).isoformat()
    try:
        return latest(code, begin, end, max_age_days=1)
    except Exception:
        return None


def _announcement_line(ann):
    date = ann.get("date", "")
    title = ann.get("title", "")
    url = ann.get("url", "")
    if not title:
        return "- 最新公告: 数据不足"
    if url:
        return f"- 最新公告: {date}《{title}》\n  {url}"
    return f"- 最新公告: {date}《{title}》（原文链接数据不足）"


def _preamble():
    return ("以价值投资视角看，先问三个问题：生意是否真赚钱、现金是否进得来、"
            "价格是否留有余地。以下是今日按数据规则筛选的持仓+候选观察清单。")


def classify_operation(row, held, holdings=None):
    """给定一行持仓行，返回 (category, 中文操作标签)。

    与每日推送 _classify 同一套逻辑，供持仓看板逐只标出建仓/加仓/减仓。
    不推送(数据不足/红旗)映射为"观望"。
    """
    buy_th = float(DAILY_PUSH.get("lishing_buy", 70) or 70)
    watch_th = float(DAILY_PUSH.get("lishing_watch", 60) or 60)
    add_gap = float(DAILY_PUSH.get("add_gap", 2.0) or 2.0)
    cat, _ = _classify(row, held, buy_th, watch_th, add_gap, holdings=holdings)
    label = ACTION_LABEL.get(cat)
    if label is None:
        label = "观望" if cat == "不推送" else cat
    return cat, label

# category -> 推送文案里的明确操作建议
ACTION_LABEL = {
    "可建仓": "观察候选（初筛，非买入建议）",
    "可加仓": "建议加仓",
    "减仓": "建议减仓",
    "可关注": "持有观察",
}


def _footer():
    return ("内容由本地数据规则生成，仅供个人复盘，不构成投资建议，"
            "也不代表李嘉诚本人观点。\n"
            "李嘉诚：先求稳，再求赢；现金与低负债是过冬的棉袄。\n"
            "芒格：用合理价格买入优秀企业，远胜用便宜价格买入平庸企业。"
            "反过来想，总是反过来想。")


def compose_message(picks_result, market="AH", data_note=None):
    """把 picks_result 渲染成推送文案。每只标的给出明确操作建议。"""
    picks = picks_result.get("picks", [])
    if not picks:
        return ""
    head = {"AH": "【A股/港股】", "US": "【美股】"}.get(market, "")
    lines = [f"{head}今日持仓+候选池体检",
             f"日期: {picks_result.get('date')}",
             "", _preamble(), ""]
    if data_note:
        lines.append(data_note)
        lines.append("")
    for p in picks:
        label = ACTION_LABEL.get(p["category"], p["category"])
        lines.append(f"{p['code']} {p['name']}：{label}")
        lines.append("理由:")
        lines.extend(p["lines"])
        lines.append("")
    _append_stop_loss_lines(lines, picks_result.get("_df"))
    lines.append(_footer())
    return "\n".join(lines)


def _append_stop_loss_lines(lines, df=None):
    """把真实持仓的止损提醒并入推送文案（无持仓或未触发时静默）。"""
    try:
        from modules.stop_loss_monitor import stop_loss_from_df
        if df is None:
            from modules.portfolio_engine import build_portfolio_df
            df, _meta = build_portfolio_df()
        r = stop_loss_from_df(df)
        if r["triggers"]:
            lines.append("")
            lines.append("🔴 止损提醒:")
            for t in r["triggers"]:
                lines.append(f"  - {t}")
    except Exception:
        pass

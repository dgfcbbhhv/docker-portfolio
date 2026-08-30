"""AI 四维个股分析（技术 / 基本面 / 财务 / 消息面），接入 OpenAI 兼容 LLM。

复用 build_portfolio_df 产出的指标作为上下文，让 LLM 生成可落盘的复盘。
provider 通过环境变量切换（优先级 AI_REVIEW_* > DEEPSEEK_*），缺失时优雅降级。
- AI_REVIEW_BASE_URL / DEEPSEEK_API_KEY 对应的默认端点为 DeepSeek
- AI_REVIEW_API_KEY / OPENROUTER_API_KEY / DEEPSEEK_API_KEY 任一即可
- AI_REVIEW_MODEL / DEEPSEEK_MODEL 指定模型（如 deepseek-chat / deepseek/deepseek-chat）
"""
import datetime
import json
import logging
import os

import requests

try:
    from modules import realtime as _rt
except Exception:
    _rt = None

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_NOTES_DIR = os.path.join(os.path.dirname(__file__), "..", "notes")


def _ai_review_url():
    return os.environ.get("AI_REVIEW_BASE_URL",
                           "https://api.deepseek.com/v1/chat/completions")


def _api_key():
    return (os.environ.get("AI_REVIEW_API_KEY")
            or os.environ.get("OPENROUTER_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("DEEPSEEK_KEY"))


def _ai_review_model():
    return os.environ.get("AI_REVIEW_MODEL") or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


def _summarize_announcements(ann_list, n=30):
    recent = (ann_list or [])[:n]
    cats = {
        "分红": ["分红", "派息", "利润分配", "送转", "派发"],
        "业绩": ["业绩", "季报", "半年", "年报", "预告", "经营"],
        "增减持": ["减持", "增持", "回购"],
        "风险": ["违规", "处罚", "警示", "停牌", "风险", "立案", "问询"],
    }
    lines = []
    for a in recent:
        t = a.get("title", "")
        cat = "其他"
        for c, kws in cats.items():
            if any(k in t for k in kws):
                cat = c
                break
        lines.append(f"  - {a.get('date', '????-??-??')} [{cat}] {t}")
    if not lines:
        return f"近{n}日公告: 无"
    return f"近期公告清单（共{len(lines)}条，均来自交易所公开披露）:\n" + "\n".join(lines)


def _portfolio_context():
    """构建『真实持仓与组合构成』文本块, 供 AI 结合上下文(封闭视野缺陷 #4)。

    真实持仓以 broker_trades.json(真实成交) 为权威源(get_real_holdings_view),
    而非看板目标仓位。返回 (text_block_or_None, held_weight_map)。
    """
    try:
        from modules.daily_picks import get_real_holdings_view
        view = get_real_holdings_view()
    except Exception as e:
        logger.warning("[ai_analysis] 持仓上下文获取失败: %s", e)
        return None, {}
    if not view:
        return None, {}
    held = {}
    lines = ["【我的真实持仓与组合构成（真实成交口径，仅供结合上下文，勿外传）】:"]
    by_group = {}
    for h in view:
        code = str(h.get("code"))
        name = h.get("name", code)
        rw = h.get("real_weight")
        target = h.get("target")
        grp = h.get("group", "") or ""
        if rw is not None:
            try:
                held[code] = float(rw)
            except (TypeError, ValueError):
                pass
            by_group.setdefault(grp, 0.0)
            by_group[grp] += float(rw)
        seg = [f"{code} {name}"]
        if grp:
            seg.append(f"[{grp}]")
        if target is not None:
            seg.append(f"目标{target}%")
        if rw is not None:
            seg.append(f"真实占比{rw:.1f}%")
        if h.get("shares") is not None:
            seg.append(f"{h['shares']}股")
        seg.append(f"价{h.get('price')} PE{h.get('pe')} PB{h.get('pb')} 股息{h.get('dy')}% "
                   f"ROE{h.get('roe')}% 负债{h.get('debt')}%")
        lines.append("  - " + " ".join(str(x) for x in seg))
    if by_group:
        alloc = "，".join(f"{g}:{v:.1f}%" for g, v in by_group.items() if v)
        lines.append(f"组合权重分布(真实): {alloc}")
    return "\n".join(lines), held


def _analyst_forecast(code):
    """机构一致预期(同花顺): 覆盖机构数 + 预期EPS同比%。返回 dict 或 None。"""
    if not (isinstance(code, str) and code.isdigit() and len(code) == 6):
        return None
    try:
        import akshare as ak
        df = ak.stock_profit_forecast_ths(symbol=code)
        if df is None or df.empty:
            return None
        last = df.iloc[-1]
        n = None
        if "预测机构数" in df.columns:
            try:
                n = int(float(last["预测机构数"]))
            except (TypeError, ValueError):
                n = None
        growth = None
        if "均值" in df.columns and len(df) >= 2:
            try:
                v0, v1 = float(df.iloc[-2]["均值"]), float(df.iloc[-1]["均值"])
                if v0 and v0 > 0:
                    growth = (v1 - v0) / v0 * 100
            except (TypeError, ValueError):
                growth = None
        if n is None and growth is None:
            return None
        return {"n": n, "eps_growth": growth}
    except Exception as e:
        logger.warning("[ai_analysis] 机构预期 %s 失败: %s", code, e)
        return None


def _northbound_net_flow():
    """北向资金(陆股通)当日净流向(亿元)。返回 dict 或 None。"""
    try:
        import akshare as ak
        df = ak.stock_hsgt_fund_flow_summary_em()
        if df is None or df.empty:
            return None
        sub = df[df.get("资金方向") == "北向"]
        if sub.empty:
            return None
        col = "资金净流入" if "资金净流入" in df.columns else "成交净买额"
        total = float(sub[col].fillna(0).sum())
        dt = None
        try:
            dt = str(sub.iloc[0].get("交易日"))
        except Exception:
            dt = None
        return {"net_yi": total, "date": dt}
    except Exception as e:
        logger.warning("[ai_analysis] 北向资金获取失败: %s", e)
        return None


def _extra_info_block(code, market, northbound):
    """扩展信息面文本块(机构预期 + 北向资金)。无数据返回 None。"""
    parts = []
    af = _analyst_forecast(code)
    if af:
        s = []
        if af.get("n"):
            s.append(f"{af['n']}家机构覆盖")
        if af.get("eps_growth") is not None:
            s.append(f"一致预期EPS同比约{af['eps_growth']:.0f}%")
        if s:
            parts.append("机构一致预期: " + "，".join(s))
    if northbound:
        d = northbound.get("net_yi")
        if d is not None:
            direction = "净流入" if d >= 0 else "净流出"
            dt = northbound.get("date") or ""
            parts.append(f"北向资金({dt})当日{direction}约{abs(d):.1f}亿元")
    if not parts:
        return None
    return "；".join(parts) + "。"


def build_prompt(name, row, rt=None, portfolio_ctx=None, held_info=None, extra_info=None):
    ctx = (
        f"股票 {row.get('code')} {name} 当前指标：\n"
        f"股价 {row.get('price')}，市盈率 {row.get('pe')}，市净率 {row.get('pb')}，\n"
        f"股息率 {row.get('dy')}%，李氏评分 {row.get('lishing')}，Kelly 建议仓位 {row.get('kelly_final')}%，\n"
        f"ROE {row.get('roe')}%，年化波动率 {row.get('vol')}%，市值 {row.get('mcap')}亿，\n"
        f"盈利同比 {row.get('earn_np_yoy')}，评分标记 {row.get('lishing_flags')}。"
    )
    if rt and not rt.get("error"):
        cap = rt.get("capital") or {}
        ind = rt.get("indicators") or {}
        main_wan = round((rt.get("main_net") or 0) / 1e4, 1)
        small_wan = round((cap.get("small_net") or 0) / 1e4, 1)
        ctx += (
            f"\n实时盘面（通达信直连）：现价 {rt.get('price')}（日内 {rt.get('pct')}%）"
            f"，换手率 {rt.get('turnover')}%，量比 {rt.get('vol_ratio')}，涨速 {rt.get('speed_pct')}%；\n"
            f"主力资金：净流入 {main_wan} 万元（散户净 {'流入' if small_wan >= 0 else '流出'} {abs(small_wan)} 万）；\n"
            f"技术指标：MACD(DIF/DEA/HIST)={ind.get('MACD_DIF')}/{ind.get('MACD_DEA')}/{ind.get('MACD_HIST')}"
            f"，KDJ(K/D/J)={ind.get('KDJ_K')}/{ind.get('KDJ_D')}/{ind.get('KDJ_J')}"
            f"，RSI={ind.get('RSI')}，乖离率BIAS={ind.get('BIAS1')}%。"
        )
        ann = rt.get("announcements")
        if isinstance(ann, list) and ann:
            ctx += "\n" + _summarize_announcements(ann)
        sec = rt.get("sector")
        if isinstance(sec, list) and sec:
            top = "，".join(
                f"{s.get('name')}(主力+{s.get('main_net_yi')}亿,涨{s.get('change_pct')}%)"
                for s in sec[:5]
            )
            ctx += f"\n行业资金风向（主力净流入前5）: {top}。"
    extra = ""
    if portfolio_ctx:
        extra += portfolio_ctx + "\n\n"
    if held_info:
        extra += f"【该标的与我的持仓关系】{held_info}\n\n"
    if extra_info:
        extra += f"【扩展信息面（机构预期/北向资金，来源公开数据）】{extra_info}\n\n"
    prompt = (
        extra + ctx + "\n\n请从以下五个维度，用中文、每条 2-3 句、务实可操作地分析该红利股：\n"
        "1. 技术面：结合上述实时 MACD/KDJ/RSI/乖离率与趋势、支撑压力、量价，判断当前位置\n"
        "2. 基本面：分红持续性 / 护城河 / 行业位置\n"
        "3. 财务面：ROE / 负债 / 现金流质量与风险点\n"
        "4. 资金面：结合上述主力/散户实时净流入与行业资金风向，判断资金态度与持续性\n"
        "5. 消息面：结合下方『近期公告清单』，提示需关注的事件与风险；只能引用清单内已列明的内容\n"
        "6. 信息面(扩展)：若上方【扩展信息面】提供了机构一致预期与北向资金，请结合判断预期差与聪明钱方向；未提供则跳过。\n"
        "\n【数据纪律（必须严格遵守）】\n"
        "- 只能依据上面提供的字段与公告清单作答，不得编造任何数字。\n"
        "- 凡下方未明确给出的具体数值（资本充足率、不良率、拨备覆盖率、除权除息日、每股分红金额、EPS/BPS 等），"
        "一律不写具体数字，改用定性描述（如『资本充足率较高』『分红率稳定』）。\n"
        "- 技术面的支撑/压力价位若给出，必须标注『（模型估计，非成交数据）』。\n"
        "- 公告只能引用清单内的日期与标题，不得推断清单外的日期或金额。\n"
        "最后给一句结论：当前是否适合按 Kelly 仓位建 / 加仓，理由一句话。"
    )
    return prompt


def analyze(name, row, model=None, portfolio_ctx=None, held_info=None, extra_info=None):
    key = _api_key()
    if not key:
        logger.warning("[ai_analysis] AI_REVIEW_API_KEY / DEEPSEEK_API_KEY 未配置，跳过")
        return None
    model = model or _ai_review_model()
    prompt = build_prompt(name, row, rt=row.get("_rt"),
                          portfolio_ctx=portfolio_ctx, held_info=held_info,
                          extra_info=extra_info)
    try:
        resp = requests.post(
            _ai_review_url(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning("[ai_analysis] call failed: %s", e)
        return None


def ai_review_candidates(payload=None, top_n=5, worker=None):
    """读取扫描结果，对 Top 候选做 AI 四维分析，落盘 notes/ai_review_<date>.md。"""
    if payload is None:
        p = os.path.join(_DATA_DIR, "dividend_candidates.json")
        if not os.path.exists(p):
            logger.info("[ai_analysis] no candidate file")
            return None
        with open(p, encoding="utf-8") as f:
            payload = json.load(f)
    cands = (payload or {}).get("candidates", [])[: int(top_n)]
    if not cands:
        return None
    sector = None
    if _rt is not None:
        try:
            sector = _rt.get_sector_sentiment(5)
        except Exception as e:
            logger.warning("[ai_analysis] sector sentiment 失败: %s", e)
    pctx, held_map = _portfolio_context()
    northbound = _northbound_net_flow()
    blocks = [f"# AI 四维复盘 {datetime.datetime.now().strftime('%Y-%m-%d')}\n"]
    for c in cands:
        if _rt is not None:
            try:
                prof = _rt.get_realtime_profile(c["code"], with_indicators=True)
                if isinstance(sector, list):
                    prof["sector"] = sector
                c["_rt"] = prof
            except Exception as e:
                logger.warning("[ai_analysis] realtime %s 失败: %s", c.get("code"), e)
        held_code = str(c.get("code"))
        held_info = None
        if held_code in held_map:
            held_info = (f"该标的已在我的持仓中，当前实仓权重约 {held_map[held_code]:.1f}%，"
                         f"属组合一部分；分析加/减仓时请结合整体仓位与行业集中度。")
        extra_info = _extra_info_block(held_code, c.get("market"), northbound)
        text = analyze(c["name"], c, portfolio_ctx=pctx, held_info=held_info,
                       extra_info=extra_info)
        blocks.append(
            f"## {c['code']} {c['name']}\n\n{c.get('reason', '')}\n\n"
            + (text or "_（未配置 DEEPSEEK_API_KEY 或调用失败）_")
        )
    md = "\n\n".join(blocks)
    os.makedirs(_NOTES_DIR, exist_ok=True)
    out = os.path.join(_NOTES_DIR, "ai_review_%s.md" % datetime.datetime.now().strftime("%Y%m%d"))
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    logger.info("[ai_analysis] wrote %s", out)
    if worker is not None:
        try:
            worker.sender.send_multi("[AI复盘] 红利候选", f"已生成 {len(cands)} 只候选四维分析：{out}")
        except Exception:
            pass
    return out


def run_ai_review(worker=None):
    from modules.config import AI_REVIEW
    return ai_review_candidates(top_n=int(AI_REVIEW.get("top_n", 5)), worker=worker)

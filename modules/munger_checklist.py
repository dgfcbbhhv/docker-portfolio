"""芒格清单 — 6 问程序化决策引擎

把情绪决策换成程序化思考：股价大跌时不问"要不要抄底/割肉"，
而是按 Q1-Q6 拆结构。本模块只做纯逻辑（输入由调用方提供），
便于单元测试；界面层在 app.py 的"芒格清单" Tab。
"""
import datetime

BUY_LINE = 70.0       # 李氏评分买入线（≥70 才允许加仓）
WATCH_LINE = 60.0     # 李氏评分观察线（<60 视为基本面恶化）
RED_SCORE = 0.0       # 红线一票否决 → 评分归零
DIV_HOLD = 3.0        # 股息率逻辑成立线（%）
DIV_WATCH = 2.0       # 股息率观察线（%）
OVERSELL_CHG = -8.0   # 单日超跌阈值（%）
BETTER_GAP = 10.0     # Q5"有更优标的"的评分差门槛


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _fmt(v, nd=2, suffix=""):
    if v is None:
        return "N/A"
    return f"{v:.{nd}f}{suffix}"


def _score_trend(score_hist, code):
    """返回 (最新分, 最新日期, 30天前分, 变化)。"""
    entries = (score_hist or {}).get(code) or []
    rows = sorted(entries, key=lambda e: str(e.get("date", "")))
    if not rows:
        return None, None, None, None
    latest = rows[-1]
    latest_s = _num(latest.get("score"))
    before_s = None
    cutoff = datetime.date.today() - datetime.timedelta(days=30)
    for e in rows:
        try:
            d = datetime.datetime.strptime(str(e.get("date", "")), "%Y-%m-%d").date()
            if d < cutoff:
                before_s = _num(e.get("score"))
        except ValueError:
            pass
    delta = round(latest_s - before_s, 1) if (latest_s is not None and before_s is not None) else None
    return latest_s, latest.get("date"), before_s, delta


def build_checklist(code, row, *, df=None, layer_summaries=None, score_hist=None,
                    manual_div_yields=None, layers_cfg=None, kelly_cfg=None,
                    watch_list=None, regime=None, freshness_issues=None,
                    max_drawdown_line=10.0, buy_scores=None, mood=None):
    """对单只标的运行芒格 6 问，返回结构化报告 dict。"""
    name = row.get("name", code)
    group = row.get("group", "")
    ratio = _num(row.get("ratio"), 0) or _num(row.get("ratio_norm"), 0)
    ls = _num(row.get("lishing"))
    chg = _num(row.get("chg"), 0)
    dy = _num(row.get("dy"))
    roe = _num(row.get("roe"))
    kelly_raw = _num(row.get("kelly_final"))
    kelly_adj = _num(row.get("kelly_portfolio_adj"), kelly_raw)
    red_line = (ls is not None and ls == RED_SCORE)
    is_us = "美股" in group

    # ── Q1 公司变了吗？──
    ls_now, ls_date, ls_before, ls_delta = _score_trend(score_hist, code)
    ls_eff = ls if ls is not None else ls_now
    flags = row.get("lishing_flags") or ""
    if red_line:
        q1_conc = "基本面恶化（红线触发，一票否决）"
        q1_action = "卖出候选"
    elif ls_eff is not None and ls_eff < WATCH_LINE:
        q1_conc = "基本面恶化（评分 < 60）"
        q1_action = "卖出候选"
    elif ls_eff is not None and ls_eff < BUY_LINE:
        q1_conc = "基本面转弱（评分 < 70，暂停加仓）"
        q1_action = "暂停加仓"
    else:
        q1_conc = "公司基本面未变（评分仍 ≥ 70）"
        q1_action = "维持评估"
    q1_evidence = [
        f"李氏评分: {_fmt(ls_eff)}（{ls_date or '今日'}" +
        (f"，30天前 {_fmt(ls_before)}，Δ{_fmt(ls_delta, 1, '')}" if ls_delta is not None else "，无历史对比") + "）",
        f"红线标志: {flags if flags else '无'}",
        f"ROE: {_fmt(roe, 1, '%')} | 单日涨跌: {_fmt(chg, 1, '%')}",
    ]

    # ── Q2 我当初为什么买？──
    md = (manual_div_yields or {}).get(code) or {}
    dy_eff = _num(md.get("ttm"), _num(md.get("lfy"), dy))
    if is_us and ls_eff is not None and ls_eff >= WATCH_LINE:
        q2_conc = "买入逻辑仍成立（美股成长评分 ≥ 60）"
        q2_action = "逻辑未破"
    elif dy_eff is not None and dy_eff >= DIV_HOLD:
        q2_conc = "买入逻辑仍成立（股息率仍 ≥ 3%）"
        q2_action = "逻辑未破"
    elif dy_eff is not None and dy_eff >= DIV_WATCH:
        q2_conc = "买入逻辑转弱（股息率 2-3%，观察）"
        q2_action = "逻辑转弱"
    else:
        q2_conc = "买入逻辑已破（股息率 < 2% 且成长评分不足）"
        q2_action = "逻辑已破"
    q2_evidence = [
        f"股息率: 手动ttm={_fmt(_num(md.get('ttm')))} / lfy={_fmt(_num(md.get('lfy')))} | 当前={_fmt(dy, 2, '%')}",
        f"Kelly 建议: {_fmt(kelly_adj, 1, '%')}（组合约束后）",
        f"设计权重: {_fmt(ratio, 1, '%')}",
    ]

    # ── Q3 市场在怕什么？──
    mkt_median = None
    if df is not None and not df.empty and "chg" in df.columns:
        mkt_median = _num(df["chg"].median())
    diff = chg - mkt_median if mkt_median is not None else None
    if mkt_median is not None and diff is not None:
        if diff <= -2.0:
            q3_conc = "个股利空（跌幅显著大于市场）"
        elif diff >= 2.0:
            q3_conc = "个股逆势抗跌（跌幅小于市场）"
        else:
            q3_conc = "跟随市场波动（系统性因素为主）"
    elif regime and regime.get("regime") == "overvalued":
        q3_conc = "市场处于高估区域（regime 提示）"
    else:
        q3_conc = "市场状态数据不足"
    fresh = freshness_issues or []
    q3_evidence = []
    if regime:
        q3_evidence.append(
            f"市场诊断: {regime.get('label', 'N/A')} | PE中位 {regime.get('pe_median', 'N/A')}"
            + (f" | 风险提示: {regime.get('risk_warning', '')}" if regime.get("risk_warning") else ""))
    q3_evidence.append(f"个股 vs 市场: 个股 {_fmt(chg, 1, '%')} vs 市场中位 {_fmt(mkt_median, 1, '%')}"
                       if mkt_median is not None else "市场数据缺失")
    q3_evidence.append(f"数据新鲜度: {'异常 ⚠ ' + '；'.join(fresh) if fresh else '正常'}")

    # ── Q4 仓位还合理吗？──
    layer_name = row.get("layer")
    cap = None
    for s in (layer_summaries or []):
        if s.get("layer") == layer_name:
            cap = _num(s.get("invested_cap"))
            break
    cap = cap if cap is not None else _num((layers_cfg or {}).get(layer_name, {}).get("invested_cap"))
    gap = round(kelly_adj - ratio, 1) if (kelly_adj is not None and ratio) else None
    if cap is not None and ratio > cap:
        q4_conc = f"仓位超上限（设计 {_fmt(ratio, 1, '%')} > 层上限 {_fmt(cap, 1, '%')}）"
        q4_action = "需减仓"
    elif gap is not None and gap >= 5.0:
        q4_conc = f"仓位偏低（Kelly 建议比当前高 {_fmt(gap, 1, '%')}）"
        q4_action = "可加仓"
    elif gap is not None and gap <= -5.0:
        q4_conc = f"仓位偏高（Kelly 建议比当前低 {_fmt(-gap, 1, '%')}）"
        q4_action = "可减仓"
    else:
        q4_conc = "仓位与 Kelly 参考基本匹配"
        q4_action = "维持"
    q4_evidence = [
        f"Kelly 建议(组合约束后): {_fmt(kelly_adj, 1, '%')} vs 设计权重 {_fmt(ratio, 1, '%')}",
        f"层上限: {_fmt(cap, 1, '%') if cap is not None else 'N/A'} | 最大回撤预警线: {_fmt(max_drawdown_line, 1, '%')}",
    ]

    # ── Q5 有更好的机会吗？──
    if buy_scores:
        cur_score = buy_scores.get(code)
        best_code, best_score = None, None
        for c, s in buy_scores.items():
            if c == code or s is None:
                continue
            if best_score is None or s > best_score:
                best_code, best_score = c, s
    else:
        cur_score = ls_eff
        best_code, best_score = None, None
        if df is not None and not df.empty:
            for _, rr in df.iterrows():
                if rr.get("code") == code:
                    continue
                s2 = _num(rr.get("lishing"))
                if s2 is None:
                    continue
                if best_score is None or s2 > best_score:
                    best_code, best_score = rr.get("code"), s2
    if (cur_score is not None and best_score is not None
            and best_code and best_score - cur_score >= BETTER_GAP):
        q5_conc = f"有更优标的：{best_code}（评分 {_fmt(best_score)} vs 本标的 {_fmt(cur_score)}）"
        q5_action = "换仓候选"
    else:
        q5_conc = "组合内无明显更优标的"
        q5_action = "不换仓"
    q5_evidence = [
        f"本标的评分: {_fmt(cur_score)}" + (f" | 最优: {best_code} {_fmt(best_score)}" if best_code else " | 无可比标的"),
        "换仓前提: 评分/股息率/置信度显著更优，否则不动",
    ]

    # ── Q6 决策门 ──
    if fresh:
        action, label = "freeze", "冻结不动"
        reason = "数据新鲜度异常，先刷新数据，禁止据此调仓"
    elif red_line:
        action, label = "sell", "卖出/清仓"
        reason = "红线触发（审计/ST/现金流断裂），一票否决"
    elif ls_eff is not None and ls_eff < WATCH_LINE:
        action, label = "sell", "减仓/卖出"
        reason = f"评分 {_fmt(ls_eff)} < {WATCH_LINE:.0f}，基本面恶化"
    elif q2_action == "逻辑已破":
        action, label = "reduce", "减仓"
        reason = "买入逻辑已破（股息率/成长逻辑失效）"
    elif (q1_action != "卖出候选" and ls_eff is not None and ls_eff >= BUY_LINE
          and chg <= OVERSELL_CHG and (kelly_adj is None or kelly_adj > 0)):
        action, label = "add", "加仓（超跌买点）"
        reason = "基本面未变 + 评分≥70 + 单日超跌 + Kelly 建议为正"
    else:
        action, label = "hold", "继续持有"
        reason = "基本面未变、逻辑未破、仓位合理，无动作依据"

    qty = None
    if action in ("add", "reduce", "sell") and gap is not None:
        qty = abs(gap)
    exec_note = ""
    if group in ("A股持仓", "REIT持仓"):
        exec_note = "A股/REIT: T+1、最小100股、涨跌停限制"
    elif group == "港股持仓":
        exec_note = "港股: 无涨跌停、T+0、最小按手数"

    counter = []
    if red_line:
        counter.append("分红/审计问题进一步恶化")
    if ls_eff is not None and ls_eff >= WATCH_LINE:
        counter.append(f"评分跌破 {WATCH_LINE:.0f}")
    if dy_eff is not None:
        counter.append("股息率大幅下调或分红取消")
    if regime and regime.get("risk_warning"):
        counter.append("系统性风险升级（" + regime["risk_warning"] + "）")
    counter_evidence = "若出现：" + "、".join(counter) + " → 说明我错了，立即重新评估。" if counter else "暂无明确反转条件，仍需每期跟踪评分与股息率。"

    return {
        "code": code,
        "name": name,
        "group": group,
        "date": datetime.date.today().isoformat(),
        "mood": mood,
        "chg": chg,
        "q1": {"conclusion": q1_conc, "action": q1_action, "evidence": q1_evidence},
        "q2": {"conclusion": q2_conc, "action": q2_action, "evidence": q2_evidence},
        "q3": {"conclusion": q3_conc, "action": None, "evidence": q3_evidence},
        "q4": {"conclusion": q4_conc, "action": q4_action, "evidence": q4_evidence},
        "q5": {"conclusion": q5_conc, "action": q5_action, "evidence": q5_evidence},
        "q6": {"action": action, "label": label, "reason": reason,
               "quantity_pct": qty, "execution_note": exec_note},
        "counter_evidence": counter_evidence,
    }
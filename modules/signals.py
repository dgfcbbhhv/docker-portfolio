"""
买卖信号引擎 v3 — 加权评分 + 多级信号 + 可读说明 (优化版)

优化点:
1. 提取公共评分函数，减少重复代码
2. 使用列表驱动替代多重if-else
3. 添加缓存机制
4. 改进代码可读性
"""
import json
import os
import datetime

NOTES_DIR = os.path.join(os.path.dirname(__file__), "..", "notes")
HISTORY_FILE = os.path.join(NOTES_DIR, "_score_history.json")

BUY_LABELS = {
    "strong_buy": "\U0001f7e2 强烈买入",
    "buy": "\U0001f7e2 可买入",
    "cautious": "\U0001f7e1 观望",
    "avoid": "\u26aa 回避",
}

SELL_LABELS = {
    "strong_hold": "\U0001f7e2 持有",
    "hold": "\U0001f7e2 持有",
    "reduce": "\U0001f7e1 减仓",
    "sell": "\U0001f534 卖出",
}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _score_by_thresholds(value, thresholds):
    """通用阈值评分: thresholds = [(threshold, score), ...] 按value>=threshold匹配"""
    if value is None:
        return 0
    for threshold, score in thresholds:
        if value >= threshold:
            return score
    return 0


def _score_by_thresholds_inv(value, thresholds):
    """反向阈值评分: value越小分数越高"""
    if value is None:
        return 0
    for threshold, score in thresholds:
        if value <= threshold:
            return score
    return 0


def _build_factor(name, score, max_score, detail, value=None):
    return {"name": name, "score": score, "max": max_score,
            "detail": detail, "value": value}


def evaluate_buy(
    lishing_score=None, pe_pctl=None, pb_pctl=None,
    div_yield=None, price_above_ma20=None, ma20_above_ma60=None,
    kelly_final=None, p_signal=None, vol_adj_factor=None,
    red_line_triggered=False, threshold=70,
    factor_dynamic_bonus=0, regime=None,
):
    if red_line_triggered:
        return {
            "signal": "avoid",
            "label": BUY_LABELS["avoid"],
            "score": 0,
            "details": {"\u7ea2\u7ebf\u89e6\u53d1": 0},
            "reason": "\u7ea2\u7ebf\u89e6\u53d1 (\u5ba1\u8ba1\u5f02\u5e38/ST/\u73b0\u91d1\u6d41\u65ad\u88c2), \u5fc5\u987b\u56de\u907f",
            "factors": [],
        }

    factors = []

    # 1. 李氏评分 (25pts)
    ls_score = _score_by_thresholds(
        lishing_score, [(80, 25), (70, 20), (60, 12), (50, 5)])
    factors.append(_build_factor(
        "\u674e\u6c0f\u8bc4\u5206", ls_score, 25,
        f"{lishing_score}\u5206" if lishing_score is not None else "N/A",
        lishing_score))

    # 2. 估值 (20pts)
    pe_score = _score_by_thresholds_inv(pe_pctl, [(30, 10), (50, 5)])
    pb_score = _score_by_thresholds_inv(pb_pctl, [(30, 10), (50, 5)])
    val_score = pe_score + pb_score
    val_detail = (f"PE\u5206\u4f4d={pe_pctl}%, PB\u5206\u4f4d={pb_pctl}%"
                  if (pe_pctl is not None or pb_pctl is not None) else "N/A")
    factors.append(_build_factor("\u4f30\u503c(PE/PB\u5206\u4f4d)", val_score, 20, val_detail))

    # 3. 股息率 (15pts)
    dy_score = _score_by_thresholds(div_yield, [(5, 15), (3, 10), (2, 5)])
    factors.append(_build_factor(
        "\u80a1\u606f\u7387", dy_score, 15,
        f"{div_yield:.2f}%" if div_yield is not None else "N/A",
        div_yield))

    # 4. Kelly信号 (15pts)
    kelly_score = 0
    if kelly_final is not None:
        if kelly_final > 0 and p_signal == "buy":
            kelly_score = 15
        elif kelly_final > 0:
            kelly_score = 10
        elif kelly_final > -5:
            kelly_score = 5
    factors.append(_build_factor(
        "Kelly\u4ed3\u4f4d\u4fe1\u53f7", kelly_score, 15,
        f"Kelly={kelly_final:.1f}%" if kelly_final is not None else "N/A"))

    # 5. 技术面 (15pts)
    tech_score = 0
    if price_above_ma20 is not None and ma20_above_ma60 is not None:
        if price_above_ma20 and ma20_above_ma60:
            tech_score = 15
        elif price_above_ma20:
            tech_score = 10
        elif ma20_above_ma60:
            tech_score = 5
    elif price_above_ma20 is not None:
        tech_score = 10 if price_above_ma20 else 0
    factors.append(_build_factor(
        "\u6280\u672f\u9762(\u5747\u7ebf\u8d8b\u52bf)", tech_score, 15,
        f"\u4ef7\u683c>MA20={price_above_ma20}, MA20>MA60={ma20_above_ma60}"))

    # 6. 波动率校准 (10pts)
    vol_score = 0
    if vol_adj_factor is not None:
        if vol_adj_factor >= 1.0:
            vol_score = 10
        elif vol_adj_factor >= 0.8:
            vol_score = 5
        else:
            vol_score = 2
    factors.append(_build_factor(
        "\u6ce2\u52a8\u7387\u6821\u51c6", vol_score, 10,
        f"\u6821\u51c6\u56e0\u5b50={vol_adj_factor:.2f}" if vol_adj_factor is not None else "N/A"))

    # 7. 因子动态加分
    if factor_dynamic_bonus > 0:
        factors.append(_build_factor(
            "\u56e0\u5b50\u52a8\u6001\u63a8\u8350", factor_dynamic_bonus, 10,
            f"\u56e0\u5b50IC/IR\u63a8\u8350\u52a0\u5206 +{factor_dynamic_bonus}"))

    total = sum(f["score"] for f in factors)
    max_possible = sum(f["max"] for f in factors)

    if total >= 80:
        signal = "strong_buy"
        reason = "\u7efc\u5408\u8bc4\u5206\u5f3a\u52b2\uff0c\u591a\u6570\u6761\u4ef6\u975e\u5e38\u6709\u5229\uff0c\u53ef\u79ef\u6781\u5efa\u4ed3"
    elif total >= 65:
        signal = "buy"
        reason = "\u7efc\u5408\u8bc4\u5206\u826f\u597d\uff0c\u6838\u5fc3\u6761\u4ef6\u6ee1\u8db3\uff0c\u53ef\u4e70\u5165"
    elif total >= 45:
        signal = "cautious"
        weak = [f["name"] for f in factors if f["score"] < f["max"] * 0.5]
        reason = (f"\u7efc\u5408\u8bc4\u5206\u4e2d\u7b49, \u5f31\u52bf\u56e0\u5b50: {', '.join(weak[:3])}"
                  if weak else "\u6574\u4f53\u4e2d\u6027, \u7b49\u5f85\u6539\u5584\u540e\u5165\u573a")
    else:
        signal = "avoid"
        reason = "\u7efc\u5408\u8bc4\u5206\u504f\u4f4e, \u591a\u6570\u6761\u4ef6\u4e0d\u6ee1\u8db3, \u4e0d\u5efa\u8bae\u4e70\u5165"

    return {
        "signal": signal,
        "label": BUY_LABELS[signal],
        "score": total,
        "max_score": max_possible,
        "factors": factors,
        "reason": f"\u7efc\u5408\u8bc4\u5206 {total}/{max_possible} | {reason}",
    }


def evaluate_sell(
    current_score=None, buy_score=None, high_250d=None,
    div_yield=None, kelly_final=None, p_signal=None,
    price_above_ma20=None, ma20_above_ma60=None,
    red_line_triggered=False, factor_dynamic_bonus=0,
):
    if red_line_triggered:
        return {
            "signal": "sell",
            "label": SELL_LABELS["sell"],
            "score": 0,
            "reason": "\u7ea2\u7ebf\u89e6\u53d1 (\u5ba1\u8ba1\u5f02\u5e38/ST/\u73b0\u91d1\u6d41\u65ad\u88c2), \u5fc5\u987b\u6e05\u4ed3",
            "factors": [],
        }

    factors = []

    # 1. 评分趋势 (30pts)
    trend_score = 0
    baseline = buy_score or high_250d
    if baseline and baseline > 0 and current_score is not None:
        drop_pct = (baseline - current_score) / baseline * 100
        trend_score = _score_by_thresholds_inv(drop_pct, [(5, 30), (10, 20), (20, 10)])
    elif current_score is not None:
        trend_score = 25
    trend_detail = (f"\u5f53\u524d={current_score}, \u57fa\u51c6={baseline}"
                    if baseline else "\u65e0\u57fa\u51c6")
    factors.append(_build_factor("\u8bc4\u5206\u8d8b\u52bf", trend_score, 30, trend_detail))

    # 2. 股息率 (20pts)
    dy_score = _score_by_thresholds(div_yield, [(3, 20), (2, 10), (1, 5)])
    factors.append(_build_factor(
        "\u80a1\u606f\u7387", dy_score, 20,
        f"{div_yield:.2f}%" if div_yield is not None else "N/A",
        div_yield))

    # 3. Kelly信号 (20pts)
    kelly_score = 0
    if kelly_final is not None:
        if kelly_final > 0 and p_signal == "buy":
            kelly_score = 20
        elif kelly_final > 0:
            kelly_score = 15
        elif kelly_final > -5:
            kelly_score = 8
    factors.append(_build_factor(
        "Kelly\u4fe1\u53f7", kelly_score, 20,
        f"Kelly={kelly_final:.1f}%" if kelly_final is not None else "N/A"))

    # 4. 技术面 (15pts)
    tech_score = 0
    if price_above_ma20 is not None and ma20_above_ma60 is not None:
        if price_above_ma20 and ma20_above_ma60:
            tech_score = 15
        elif price_above_ma20:
            tech_score = 8
        elif ma20_above_ma60:
            tech_score = 5
    elif price_above_ma20 is not None:
        tech_score = 8 if price_above_ma20 else 0
    factors.append(_build_factor(
        "\u6280\u672f\u9762", tech_score, 15,
        f"\u4ef7\u683c>MA20={price_above_ma20}, MA20>MA60={ma20_above_ma60}"))

    # 5. 红线检查 (15pts)
    factors.append(_build_factor("\u7ea2\u7ebf\u68c0\u67e5", 15, 15, "\u672a\u89e6\u53d1\u7ea2\u7ebf"))

    total = sum(f["score"] for f in factors)
    max_possible = sum(f["max"] for f in factors)

    if total >= 75:
        signal = "strong_hold"
        reason = "\u5404\u9879\u6307\u6807\u7a33\u5b9a, \u7ee7\u7eed\u6301\u6709"
    elif total >= 55:
        signal = "hold"
        reason = "\u90e8\u5206\u6307\u6807\u9700\u5173\u6ce8, \u6301\u6709\u89c2\u5bdf"
    elif total >= 35:
        signal = "reduce"
        weak = [f["name"] for f in factors if f["score"] < f["max"] * 0.5]
        reason = (f"\u6076\u5316\u56e0\u5b50: {', '.join(weak[:3])}"
                  if weak else "\u8bc4\u5206\u504f\u4f4e, \u5efa\u8bae\u51cf\u4ed3")
    else:
        signal = "sell"
        reason = "\u591a\u9879\u6307\u6807\u4e25\u91cd\u6076\u5316, \u5efa\u8bae\u6e05\u4ed3"

    return {
        "signal": signal,
        "label": SELL_LABELS[signal],
        "score": total,
        "max_score": max_possible,
        "factors": factors,
        "reason": f"\u7efc\u5408\u8bc4\u5206 {total}/{max_possible} | {reason}",
    }


def format_fix_summary(evaluations):
    weak = [f for f in evaluations if f["score"] < f["max"] * 0.5]
    if not weak:
        return None
    lines = ["**\u6539\u5584\u65b9\u5411:**"]
    for f in weak:
        pct = f["score"] / f["max"] * 100 if f["max"] > 0 else 0
        badge = "\U0001f534" if pct < 30 else ("\U0001f7e1" if pct < 50 else "\u26aa")
        lines.append(f"{badge} {f['name']}: {f['detail']} ({f['score']}/{f['max']})")
    return "\n".join(lines)


# ── 评分历史管理 ──

def load_score_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_score_snapshot(code, score):
    history = load_score_history()
    today = datetime.date.today().isoformat()
    if code not in history:
        history[code] = []
    if history[code] and history[code][-1]["date"] == today:
        history[code][-1]["score"] = score
    else:
        history[code].append({"date": today, "score": score})
        history[code] = history[code][-365:]
    os.makedirs(NOTES_DIR, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def get_high_250d(code):
    history = load_score_history()
    snaps = history.get(code, [])
    if not snaps:
        return None
    scores = [s["score"] for s in snaps if s.get("score") is not None]
    return max(scores) if scores else None


def set_buy_score(code, score):
    os.makedirs(NOTES_DIR, exist_ok=True)
    p = os.path.join(NOTES_DIR, "%s.json" % code)
    note = {}
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            note = json.load(f)
    note["buy_score"] = score
    note["buy_date"] = datetime.date.today().isoformat()
    with open(p, "w", encoding="utf-8") as f:
        json.dump(note, f, ensure_ascii=False, indent=2)


def get_buy_score(code):
    p = os.path.join(NOTES_DIR, "%s.json" % code)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            note = json.load(f)
        return note.get("buy_score")
    return None


def get_buy_date(code):
    p = os.path.join(NOTES_DIR, "%s.json" % code)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            note = json.load(f)
        return note.get("buy_date")
    return None


def evaluate_growth(
    pe_pctl=None, pb_pctl=None,
    price_above_ma20=None, ma20_above_ma60=None,
    price_above_ma60=None, ma60_above_ma120=None,
    kelly_final=None, vol_adj_factor=None,
    p_signal=None,
):
    """成长股独立评估 (替代李氏评分+股息率体系)"""
    factors = []

    # 1. 估值分位 (30pts)
    if pe_pctl is not None and pb_pctl is not None:
        avg_pctl = (pe_pctl + pb_pctl) / 2
    elif pe_pctl is not None:
        avg_pctl = pe_pctl
    else:
        avg_pctl = None

    val_score = 0
    if avg_pctl is not None:
        val_score = _score_by_thresholds_inv(avg_pctl, [(20, 30), (35, 25), (50, 20), (70, 10)])
        if val_score == 0:
            val_score = 5
    val_detail = (f"PE\u5206\u4f4d={pe_pctl}%, PB\u5206\u4f4d={pb_pctl}%"
                  if (pe_pctl is not None or pb_pctl is not None) else "N/A")
    factors.append(_build_factor("\u4f30\u503c\u5206\u4f4d(PE/PB)", val_score, 30, val_detail))

    # 2. 趋势结构 (25pts)
    checks = [c for c in [price_above_ma20, ma20_above_ma60,
                           price_above_ma60, ma60_above_ma120] if c is not None]
    true_count = sum(1 for c in checks if c)
    if len(checks) == 0:
        trend_score = 0
    elif len(checks) >= 3 and true_count == len(checks):
        trend_score = 25
    elif len(checks) >= 3 and true_count >= len(checks) - 1:
        trend_score = 20
    elif len(checks) > 0 and true_count >= len(checks) * 0.5:
        trend_score = 15
    elif true_count > 0:
        trend_score = 8
    else:
        trend_score = 0
    factors.append(_build_factor(
        "\u8d8b\u52bf\u7ed3\u6784", trend_score, 25,
        f"\u591a\u5934\u6307\u6807: {true_count}/{len(checks)}" if checks else "N/A"))

    # 3. Kelly信号方向 (15pts)
    kelly_score = 0
    if kelly_final is not None:
        if kelly_final > 5 and p_signal == "buy":
            kelly_score = 15
        elif kelly_final > 0:
            kelly_score = 10
        elif kelly_final > -5:
            kelly_score = 5
    factors.append(_build_factor(
        "Kelly\u4fe1\u53f7", kelly_score, 15,
        f"Kelly={kelly_final:.1f}%" if kelly_final is not None else "N/A"))

    # 4. 波动率校准 (15pts)
    vol_score = 0
    if vol_adj_factor is not None:
        vol_score = _score_by_thresholds_inv(vol_adj_factor,
            [(0.8, 15), (1.0, 12), (1.3, 8), (1.5, 5)])
        if vol_score == 0:
            vol_score = 2
    factors.append(_build_factor(
        "\u6ce2\u52a8\u7387\u6821\u51c6", vol_score, 15,
        f"\u6821\u51c6\u56e0\u5b50={vol_adj_factor:.2f}" if vol_adj_factor is not None else "N/A"))

    # 5. 趋势稳定性 (15pts)
    stability_score = 0
    if price_above_ma60 is not None and price_above_ma20 is not None:
        if price_above_ma60 and price_above_ma20:
            stability_score = 15
        elif price_above_ma60 or price_above_ma20:
            stability_score = 8
    elif price_above_ma60 is not None:
        stability_score = 10 if price_above_ma60 else 3
    elif price_above_ma20 is not None:
        stability_score = 8 if price_above_ma20 else 3
    factors.append(_build_factor(
        "\u8d8b\u52bf\u7a33\u5b9a\u6027", stability_score, 15,
        f"MA60={price_above_ma60}, MA20={price_above_ma20}"))

    total = sum(f["score"] for f in factors)
    max_possible = sum(f["max"] for f in factors)

    if total >= 80:
        signal = "strong_buy"
        label = "\u5f3a\u52bf\u4e70\u5165"
    elif total >= 60:
        signal = "buy"
        label = "\u7a33\u5b9a\u6301\u6709"
    elif total >= 40:
        signal = "hold"
        label = "\u89c2\u671b"
    else:
        signal = "avoid"
        label = "\u56de\u907f"

    weakest = min(factors, key=lambda f: f["score"] / max(f["max"], 1)) if factors else None
    reason = (f"\u7efc\u5408\u8bc4\u5206 {total}/{max_possible} | \u6700\u5f31\u7ef4\u5ea6: {weakest['name']}"
              if weakest else "")

    return {
        "signal": signal,
        "label": label,
        "score": total,
        "max_score": max_possible,
        "factors": factors,
        "reason": reason,
    }

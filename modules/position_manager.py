"""仓位管理：把“长期持有吃分红”的规则固化成可执行结论。

规则优先级（任一命中即降级）：
1. 李氏/成长评分不可用或红旗 → 停止加仓
2. 数据新鲜度异常 / 手动兜底 → 暂停
3. 成本档位为警戒 → 暂停
4. 单票已达上限 → 持有
5. Kelly 无加仓空间或加仓空间不足 → 持有
6. 以上全部通过且空间 >= add_gap → 可加仓
"""
from modules.config import LAYERS, MAX_TOTAL, MAXPOS, DAILY_PUSH

_GROUP_LAYER = {
    "A股持仓": "A",
    "港股持仓": "HK",
    "美股": "US",
    "REIT持仓": "REIT",
}


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_position_plan(rows, tier_by_code=None, freshness_codes=None, rules=None):
    """根据持仓行生成逐只仓位结论与组合层状态。

    Args:
        rows: 主界面 DataFrame 的行（dict 或 Series）。
        tier_by_code: {code: "黄金"/"白银"/"警戒"/"数据不足"}。
        freshness_codes: 数据新鲜度有问题的代码集合。
        rules: 可选覆盖 maxpos / max_total / lishing_buy / add_gap。
    """
    rules = rules or {}
    maxpos = _f(rules.get("maxpos", MAXPOS))
    max_total = _f(rules.get("max_total", MAX_TOTAL))
    buy_th = _f(rules.get("lishing_buy", DAILY_PUSH["lishing_buy"]))
    add_gap = _f(rules.get("add_gap", DAILY_PUSH["add_gap"]))
    tier_by_code = tier_by_code or {}
    freshness_codes = set(freshness_codes or [])

    entries = []
    for r in rows:
        code = str(r.get("code", ""))
        name = str(r.get("name", code))
        group = str(r.get("group", ""))
        current = _f(r.get("weight", r.get("ratio", 0)))
        target = _f(r.get("kelly_portfolio_adj", 0))
        ls = _f(r.get("lishing", 0))
        dy = _f(r.get("dy", 0))
        price = _f(r.get("price", 0))
        data_flags = str(r.get("data_flags", "") or "")
        tier = tier_by_code.get(code, "数据不足")

        reasons = []
        verdict = "可加仓"
        if ls <= 0:
            verdict = "停止加仓"
            reasons.append("评分不可用或触发红旗")
        elif code in freshness_codes or "manual" in data_flags:
            verdict = "暂停"
            reasons.append("数据新鲜度异常或手动兜底")
        elif tier == "警戒":
            verdict = "暂停"
            reasons.append("成本档位警戒")
        elif current >= maxpos:
            verdict = "持有"
            reasons.append("已达单票上限")
        elif target <= 0:
            verdict = "持有"
            reasons.append("Kelly 无加仓空间")
        elif target - current < add_gap:
            verdict = "持有"
            reasons.append("加仓空间不足")
        else:
            reasons.append("评分达标且有加仓空间")

        gap = max(0.0, target - current) if verdict == "可加仓" else 0.0
        entries.append({
            "code": code,
            "name": name,
            "group": group,
            "layer": _GROUP_LAYER.get(group, group),
            "current_ratio": round(current, 1),
            "target_ratio": round(target, 1),
            "gap_ratio": round(gap, 1),
            "lishing": ls,
            "dy": dy,
            "price": price,
            "tier": tier,
            "verdict": verdict,
            "reasons": "；".join(reasons),
            "div_reinvest": "可考虑分红再投入" if verdict == "可加仓" and dy > 0 else "分红先留存或等下次机会",
        })

    layers = []
    for key, cfg in (LAYERS or {}).items():
        if not cfg.get("enabled", True):
            continue
        cap = _f(cfg.get("invested_cap", 0))
        sub = [e for e in entries if e["layer"] == key]
        cur = sum(e["current_ratio"] for e in sub)
        tgt = sum(e["target_ratio"] for e in sub)
        layers.append({
            "layer": key,
            "invested_cap": cap,
            "current_ratio": round(cur, 1),
            "target_ratio": round(tgt, 1),
            "spare_ratio": round(max(0.0, cap - cur), 1),
            "status": "超限" if cur > cap + 0.5 else "正常",
        })

    total_current = round(sum(e["current_ratio"] for e in entries), 1)
    total_target = round(sum(e["target_ratio"] for e in entries), 1)
    summary = {
        "total_current": total_current,
        "total_target": total_target,
        "cash_ratio": round(max(0.0, 100.0 - total_current), 1),
        "max_total": max_total,
        "total_cash_spare": round(max(0.0, max_total - total_current), 1),
        "addable": sum(1 for e in entries if e["verdict"] == "可加仓"),
        "paused": sum(1 for e in entries if e["verdict"] == "暂停"),
        "stopped": sum(1 for e in entries if e["verdict"] == "停止加仓"),
        "over_single_limit": sum(1 for e in entries if e["current_ratio"] >= maxpos),
        "freshness_codes": sorted(freshness_codes),
    }
    return {"entries": entries, "layers": layers, "summary": summary, "rules": {
        "maxpos": maxpos, "max_total": max_total, "buy_th": buy_th, "add_gap": add_gap}}

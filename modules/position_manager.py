"""仓位管理：把“长期持有吃分红”的规则固化成可执行结论。

规则优先级（任一命中即降级）：
1. 李氏/成长评分不可用或红旗 → 停止加仓
2. 数据新鲜度异常 / 手动兜底 → 暂停
3. 成本档位为警戒 → 暂停
4. 单票已达上限 → 持有
5. Kelly 无加仓空间或加仓空间不足 → 持有
6. 以上全部通过且空间 >= add_gap → 可加仓
"""
from modules.config import DAILY_PUSH, LAYERS, MAX_TOTAL, MAXPOS, RISK_BUDGET
from modules.add_ladder import plan_add_ladder
from modules.risk_budget import risk_budget_per_add

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


def _resolve_actual_ratios(rows, actual_holdings=None):
    """真实市值占比。

    有真实持仓时：真实市值占比 = 该股市值 / (总市值 + 现金)。
    返回 (ratio_map, source)，ratio_map: {code: float(%)}；source: "actual" | "target"。
    真实持仓为空时返回 ({}, "target")，调用方回退目标权重。
    """
    ah = actual_holdings
    if ah is None:
        try:
            from modules.holdings import get_actual_holdings
            ah = get_actual_holdings()
        except Exception:
            ah = {}
    pos = (ah or {}).get("positions") or {}
    if not pos:
        return {}, "target"
    # 用行内现价估算市值（无现价回退成本）
    price_map = {}
    for r in rows:
        code = str(r.get("code", ""))
        p = _f(r.get("price", 0))
        if code and p > 0:
            price_map[code] = p
    total_mv = 0.0
    per_mv = {}
    for code, p in pos.items():
        shares = _f((p or {}).get("shares", 0))
        avg = _f((p or {}).get("avg_cost", 0))
        px = price_map.get(str(code)) or avg
        mv = shares * px
        per_mv[str(code)] = mv
        total_mv += mv
    cash = _f((ah or {}).get("cash", 0))
    denom = total_mv + cash
    if denom <= 0:
        return {}, "target"
    return {code: round(mv / denom * 100.0, 1) for code, mv in per_mv.items()}, "actual"


def build_position_plan(rows, tier_by_code=None, freshness_codes=None, rules=None,
                        capital=None, actual_holdings=None):
    """根据持仓行生成逐只仓位结论与组合层状态。

    Args:
        rows: 主界面 DataFrame 的行（dict 或 Series）。
        tier_by_code: {code: "黄金"/"白银"/"警戒"/"数据不足"}。
        freshness_codes: 数据新鲜度有问题的代码集合。
        rules: 可选覆盖 maxpos / max_total / lishing_buy / add_gap。
        capital: 组合总资产（元）。提供时启用单笔风险预算约束。
        actual_holdings: 真实持仓 {"positions": {code: {shares, avg_cost}}, "cash": x}。
            提供时该股 current 用真实市值占比；缺省自动调 holdings.get_actual_holdings()。
    """
    rules = rules or {}
    maxpos = _f(rules.get("maxpos", MAXPOS))
    max_total = _f(rules.get("max_total", MAX_TOTAL))
    buy_th = _f(rules.get("lishing_buy", DAILY_PUSH["lishing_buy"]))
    add_gap = _f(rules.get("add_gap", DAILY_PUSH["add_gap"]))
    tier_by_code = tier_by_code or {}
    freshness_codes = set(freshness_codes or [])
    actual_ratios, ratio_source = _resolve_actual_ratios(rows, actual_holdings)

    entries = []
    for r in rows:
        code = str(r.get("code", ""))
        name = str(r.get("name", code))
        group = str(r.get("group", ""))
        # 有真实持仓用真实市值占比，否则回退目标权重（规划缺口口径）
        actual_ratio = actual_ratios.get(code)
        if ratio_source == "actual" and actual_ratio is not None:
            current = actual_ratio
        else:
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
            if ratio_source == "actual":
                reasons.append(f"真实持仓占比 {current:.1f}% 已达单票上限 {maxpos:.0f}%，建议减仓")
            else:
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

        # 加仓台账：cost_tiers.json 的成本批次数 = 已加仓次数（每笔批次即一次加仓）
        add_count = 0
        try:
            from modules.cost_tier import get_stock as _ct_stock
            add_count = len((_ct_stock(code) or {}).get("batches") or [])
        except Exception:
            add_count = 0

        ladder = plan_add_ladder(
            current, target, tier=tier, lishing=ls, buy_th=buy_th,
            maxpos=maxpos, min_gap=add_gap, add_count=add_count)
        ladder_verdict = ladder["verdict"]
        if verdict == "可加仓" and ladder_verdict != "可加仓":
            verdict = "持有"
            reasons.append(ladder["reason"])
            gap = 0.0

        # 单笔风险预算：cap_add_pct = 总资产×risk_pct÷止损距离，与 ladder 建议取小者
        risk = None
        rb_cap_pct = None
        ladder_add_pct = ladder.get("add_pct", 0.0) if verdict == "可加仓" else 0.0
        if verdict == "可加仓" and RISK_BUDGET.get("enabled") and capital:
            risk = risk_budget_per_add(capital)
            rb_cap_pct = round(min(risk["max_value"], capital) / capital * 100.0, 1) if capital else 0.0
            if rb_cap_pct < ladder_add_pct:
                ladder_add_pct = rb_cap_pct

        entries.append({
            "code": code,
            "name": name,
            "group": group,
            "layer": _GROUP_LAYER.get(group, group),
            "current_ratio": round(current, 1),
            "current_source": ratio_source,
            "target_ratio": round(target, 1),
            "gap_ratio": round(gap, 1),
            "lishing": ls,
            "dy": dy,
            "price": price,
            "tier": tier,
            "verdict": verdict,
            "reasons": "；".join(reasons),
            "add_step": ladder.get("step"),
            "add_pct": round(ladder_add_pct, 1),
            "add_count": add_count,
            "ladder_alloc": ladder.get("ladder_alloc", []),
            "ladder_cap_hit": ladder.get("cap_hit", False),
            "risk_budget": risk,
            "risk_cap_pct": rb_cap_pct,
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
        "ratio_source": ratio_source,
    }
    return {"entries": entries, "layers": layers, "summary": summary, "rules": {
        "maxpos": maxpos, "max_total": max_total, "buy_th": buy_th, "add_gap": add_gap}}

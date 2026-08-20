"""单笔风险预算 — 把"单笔最多亏多少"锁进加仓量公式。

风险预算 = 总资产 × risk_pct ÷ 止损距离
  - risk_pct: 单笔最大可亏比例（视频建议 1-2%，默认 1.5%）
  - 止损距离: 触发止损的跌幅（默认 5%-10% 区间，用保守侧 5% 即最大风险距离）
  - 结果 = 该笔最多可投入金额，超过即否决。

与 add_ladder 的层级建议取小者：ladder 决定"节奏"，risk_budget 决定"单笔上限"。
"""
from modules.config import RISK_BUDGET


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def risk_budget_per_add(total_assets, *, risk_pct=None, stop_loss_pct=None):
    """单笔加仓的金额上限。

    Args:
        total_assets: 组合总资产（元）。
        risk_pct: 单笔最大可亏比例（%），默认取配置。
        stop_loss_pct: 止损距离（%），默认取配置。

    Returns:
        dict: {max_value, max_pct_of_assets, risk_pct, stop_loss_pct, note}
    """
    total_assets = _num(total_assets)
    risk_pct = _num(risk_pct, None)
    if risk_pct is None:
        risk_pct = _num(RISK_BUDGET.get("risk_pct"), 1.5)
    stop_pct = _num(stop_loss_pct, None)
    if stop_pct is None:
        stop_pct = _num(RISK_BUDGET.get("stop_loss_pct"), 5.0)

    if total_assets <= 0:
        return {"max_value": 0.0, "max_pct_of_assets": 0.0,
                "risk_pct": risk_pct, "stop_loss_pct": stop_pct,
                "note": "总资产缺失，跳过风险预算"}

    if stop_pct <= 0:
        return {"max_value": 0.0, "max_pct_of_assets": 0.0,
                "risk_pct": risk_pct, "stop_loss_pct": stop_pct,
                "note": "止损距离配置非法，跳过风险预算"}

    max_value = total_assets * (risk_pct / 100.0) / (stop_pct / 100.0)
    return {
        "max_value": round(max_value, 2),
        "max_pct_of_assets": round(max_value / total_assets * 100.0, 2) if total_assets else 0.0,
        "risk_pct": risk_pct,
        "stop_loss_pct": stop_pct,
        "note": f"单笔最多亏 {risk_pct:.1f}%（≈¥{total_assets * risk_pct / 100.0:,.0f}），止损 {stop_pct:.0f}% → 本笔最多投 ¥{max_value:,.0f}",
    }


def effective_add_value(ladder_value, total_assets, *, risk_pct=None, stop_loss_pct=None):
    """ladder 建议金额与风险预算取小者。

    Args:
        ladder_value: add_ladder 建议投入金额（元，可为 None）。
        total_assets: 组合总资产（元）。

    Returns:
        dict: {value, capped_by_risk, budget}。value 为最终建议投入金额。
    """
    budget = risk_budget_per_add(total_assets, risk_pct=risk_pct, stop_loss_pct=stop_loss_pct)
    if ladder_value is None:
        return {"value": budget["max_value"], "capped_by_risk": True, "budget": budget}
    ladder_value = _num(ladder_value)
    capped = ladder_value > budget["max_value"] and budget["max_value"] > 0
    return {"value": min(ladder_value, budget["max_value"]), "capped_by_risk": capped, "budget": budget}
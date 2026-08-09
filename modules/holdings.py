"""真实持仓接入层。

口径
----
- PORT_A/PORT_HK/PORT_US/PORT_REIT 里的 `ratio` 是**目标配置权重**，不是真实持仓。
- 真实持仓优先来自 paper_trader 的虚拟组合 JSON（positions + cash + capital）。
- 接入层统一暴露 `get_actual_holdings()`：有真实持仓返回 dict，否则返回 {}。
- 调用方无真实持仓时应回退到"目标组合"语义（视为规划持有/目标占比）。

后续要接券商真实持仓（broker_import/execution_log）时，改本文件内部实现即可，
对外接口（get_actual_holdings / market_value_ratio）保持不变。
"""
import logging

logger = logging.getLogger(__name__)



def get_actual_holdings():
    """返回真实持仓；无任何持仓数据时返回 {}。

    规范源：真实成交(broker_trades) → 影子账号(paper_trader) 依次回退。

    返回结构: {
        "positions": {code: {"shares": float, "avg_cost": float}},
        "cash": float,          # 剩余现金
        "capital": float,       # 总本金
        "_source": "broker" | "paper"
    }
    """
    try:
        from modules.actual_holdings import get_actual_holdings as _ah
        out = _ah()
        if out:
            return out
    except Exception as _e:
        logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    try:
        from modules.paper_trader import VIRTUAL_CAPITAL, _get_paper_portfolio
        pf = _get_paper_portfolio() or {}
        positions = pf.get("positions") or {}
        if not positions:
            return {}
        cash = float(pf.get("cash") or 0)
        capital = float(pf.get("capital") or VIRTUAL_CAPITAL or 0)
        out = {}
        for code, pos in positions.items():
            shares = float((pos or {}).get("shares") or 0)
            avg = float((pos or {}).get("avg_cost") or 0)
            if shares > 0:
                out[str(code)] = {"shares": shares, "avg_cost": avg}
        if not out:
            return {}
        return {"positions": out, "cash": cash, "capital": capital, "_source": "paper"}
    except Exception:
        return {}


def market_value(positions, price_of):
    """按现价估算持仓市值：price_of(code) -> float 或 None。

    返回 (total_market_value, per_code_value)。缺价的标的按其成本估算。
    """
    total = 0.0
    per_code = {}
    for code, pos in (positions or {}).items():
        shares = pos.get("shares") or 0
        p = None
        try:
            p = price_of(code)
        except Exception:
            p = None
        if not p or p <= 0:
            p = pos.get("avg_cost") or 0
        mv = shares * p
        per_code[code] = mv
        total += mv
    return total, per_code


def market_value_ratio(code, positions, cash, per_code, total_mv):
    """持仓市值占比(%)：市值 / (总市值 + 现金)。无持仓返回 None。"""
    if code not in (positions or {}):
        return None
    denom = total_mv + (cash or 0)
    if denom <= 0:
        return None
    return round(per_code.get(code, 0.0) / denom * 100.0, 1)

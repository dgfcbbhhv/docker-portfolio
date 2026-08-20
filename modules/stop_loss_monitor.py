"""持仓止损提醒 — 把"每笔最多亏 X%"的风险预算落地为卖出提醒。

risk_budget.stop_loss_pct 原本只用于限制加仓量，这里进一步用它对**真实持仓**
做止损监控：现价较持仓成本(avg_cost)回撤 ≥ stop_loss_pct 时给出卖出提醒。

数据源：真实持仓(holdings.get_actual_holdings → broker_trades) 的 avg_cost 为成本，
现价优先取调用方传入的价格表(price_of / df 行)，缺失时回退成本价（不误报）。
"""


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def check_stop_loss(holdings=None, price_map=None, stop_loss_pct=None, rule=None):
    """扫描真实持仓的止损状态。

    Args:
        holdings: {"positions": {code: {shares, avg_cost}}, "cash": x}；缺省自动获取。
        price_map: {code: 现价}；缺省用 avg_cost（无法判断则跳过，不误报）。
        stop_loss_pct: 回撤阈值(%)；缺省取 config RISK_BUDGET.stop_loss_pct。
        rule: 可选覆盖规则（"止损提醒" / "关注" / None 自动）。

    Returns:
        dict: {"ok": bool, "rows": [{code, name, shares, cost, price, drawdown_pct,
                                    verdict, rule, days_held?}], "triggers": [str]}
    """
    from modules.config import RISK_BUDGET
    if stop_loss_pct is None:
        stop_loss_pct = _f((RISK_BUDGET or {}).get("stop_loss_pct", 5.0), 5.0)
    rule = rule or "止损提醒"

    if holdings is None:
        try:
            from modules.holdings import get_actual_holdings
            holdings = get_actual_holdings()
        except Exception:
            holdings = {}

    pos = (holdings or {}).get("positions") or {}
    price_map = price_map or {}
    rows = []
    triggers = []

    for code, p in pos.items():
        code = str(code)
        shares = _f((p or {}).get("shares", 0))
        cost = _f((p or {}).get("avg_cost", 0))
        if shares <= 0 or cost <= 0:
            continue
        px = _f(price_map.get(code, 0))
        if px <= 0:
            px = cost  # 无现价视为成本，drawdown=0 不触发
        drawdown = (cost - px) / cost * 100.0 if cost > 0 else 0.0
        verdict = "止损提醒" if drawdown >= stop_loss_pct else "正常"
        rows.append({
            "code": code,
            "shares": shares,
            "cost": round(cost, 3),
            "price": round(px, 3),
            "drawdown_pct": round(drawdown, 1),
            "verdict": verdict,
            "rule": rule,
        })
        if verdict == "止损提醒":
            triggers.append(
                f"{code} 现价{px:.2f}较成本{cost:.2f}回撤{drawdown:.1f}% ≥ {stop_loss_pct:.0f}%")

    return {"ok": not triggers, "rows": rows, "triggers": triggers,
            "stop_loss_pct": stop_loss_pct}


def stop_loss_from_df(df, holdings=None, stop_loss_pct=None):
    """从持仓 DataFrame 提取现价后做止损检查（供 UI 用）。

    Args:
        df: 主界面 DataFrame（含 code/price 列）。
        holdings: 真实持仓；缺省自动获取。
    Returns:
        同 check_stop_loss。
    """
    price_map = {}
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            code = str(r.get("code", ""))
            px = _f(r.get("price", 0))
            if code and px > 0:
                price_map[code] = px
    return check_stop_loss(holdings=holdings, price_map=price_map,
                           stop_loss_pct=stop_loss_pct)
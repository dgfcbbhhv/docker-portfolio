"""
Paper Executor - 组合状态 & 订单执行引擎
从 paper_trader.py 拆分，处理虚拟组合持仓、订单创建、订单执行
"""
import datetime
import logging

import pandas as pd

logger = logging.getLogger(__name__)

from modules.config import WATCH_LIST
from modules.paper_trader import (
    A_LIMIT_BSE_REIT,
    A_LIMIT_CHINEXT,
    A_LIMIT_DEFAULT,
    A_LIMIT_ST,
    FEE_RATE,
    MAX_POSITION_PCT,
    MIN_LOT,
    PORTFOLIO_FILE,
    SIGNAL_BUY_THRESHOLD,
    SIGNAL_SELL_THRESHOLD,
    STAMP_TAX_RATE,
    VIRTUAL_CAPITAL,
    _load_json,
    _save_json,
)


def _get_paper_portfolio():
    """获取当前虚拟组合状态。"""
    pf = _load_json(PORTFOLIO_FILE)
    if not pf:
        pf = {
            "capital": VIRTUAL_CAPITAL,
            "cash": VIRTUAL_CAPITAL,
            "positions": {},
            "trades": [],
            "pending_orders": [],
            "skipped_orders": [],
            "created_at": datetime.date.today().isoformat(),
        }
        _save_json(PORTFOLIO_FILE, pf)
    pf.setdefault("pending_orders", [])
    pf.setdefault("skipped_orders", [])
    return pf


def _fee_rate():
    from modules.config import get_config
    return float((get_config().get("paper_trade", {}) or {}).get("fee_rate", FEE_RATE))


def _stamp_tax_rate():
    from modules.config import get_config
    return float((get_config().get("paper_trade", {}) or {}).get("stamp_tax_rate", STAMP_TAX_RATE))


def _a_limit_ratio(code):
    """A 股涨跌停幅度：ST 5%，创业板/科创板 20%，北交所/REIT 30%，其余 10%。"""
    c = str(code)
    name = WATCH_LIST.get(c, {}).get("name", "")
    if "ST" in name or "退" in name:
        return A_LIMIT_ST
    if c.startswith(("300", "301", "688", "689")):
        return A_LIMIT_CHINEXT
    if c.startswith(("4", "8", "92", "508", "180", "159")):
        return A_LIMIT_BSE_REIT
    return A_LIMIT_DEFAULT


def _a_limit_prices(bar, code):
    prev = bar.get("prev_close")
    if not prev or prev <= 0:
        return None, None
    ratio = _a_limit_ratio(code)
    return round(prev * (1 + ratio), 2), round(prev * (1 - ratio), 2)


def _fetch_a_bar_for_date(code, date):
    """取某日 A 股 K 线（含昨收）；当日无行情视为停牌/无成交。"""
    from modules.data.market import _fetch_hist_em_or_tx
    end = date.strftime("%Y%m%d")
    start = (date - datetime.timedelta(days=10)).strftime("%Y%m%d")
    df = _fetch_hist_em_or_tx(code, start, end)
    if df is None or df.empty:
        return None
    df = df.copy()
    if "date" not in df.columns:
        df = df.reset_index()
        df.columns = [str(c) for c in df.columns]
        if "date" not in df.columns and "index" in df.columns:
            df = df.rename(columns={"index": "date"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    today = pd.Timestamp(date)
    row = df[df["date"] == today]
    if row.empty:
        return {"suspended": True}
    r = row.iloc[-1]
    prev = df[df["date"] < today]
    prev_close = float(prev["close"].iloc[-1]) if not prev.empty and "close" in prev.columns else None
    return {
        "open": float(r.get("open", r["close"])),
        "close": float(r["close"]),
        "high": float(r.get("high", r["close"])),
        "low": float(r.get("low", r["close"])),
        "prev_close": prev_close,
        "suspended": False,
    }


def _target_buy_shares(snap, pf):
    """按 Kelly 建议仓位计算买入股数（100 股整手）。"""
    code = snap.get("code")
    price = snap.get("price")
    if not code or not price or price <= 0:
        return None
    buy_score = snap.get("buy_score", 0)
    buy_signal = snap.get("buy_signal", "avoid")
    kelly_pct = snap.get("kelly_final", 0)
    if buy_score < SIGNAL_BUY_THRESHOLD or buy_signal not in ("strong_buy", "buy"):
        return None
    factor = 1.0 if buy_signal == "strong_buy" else 0.7
    target_value = pf["capital"] * min(kelly_pct / 100.0 * factor, MAX_POSITION_PCT / 100.0)
    pos = pf["positions"].get(code, {})
    current_value = pos.get("shares", 0) * price
    delta = target_value - current_value
    if delta <= 0:
        return None
    shares = int(delta / price / MIN_LOT) * MIN_LOT
    return shares if shares >= MIN_LOT else None


def _create_pending_orders(snapshots, pf, signal_date):
    """今日收盘快照 → 生成明日开盘执行的挂单。"""
    pending = [p for p in pf.get("pending_orders", [])
               if p.get("signal_date") != signal_date]
    for snap in snapshots:
        code = snap.get("code")
        if not code or snap.get("is_mock"):
            continue
        price = snap.get("price")
        if not price or price <= 0:
            continue
        pos = pf["positions"].get(code, {})
        sell_score = snap.get("sell_score", 100)
        if sell_score < SIGNAL_SELL_THRESHOLD and pos.get("shares", 0) > 0:
            pending = [p for p in pending if p.get("code") != code]
            pending.append({
                "signal_date": signal_date, "code": code, "action": "sell",
                "shares": pos["shares"],
                "sell_score": sell_score, "sell_signal": snap.get("sell_signal"),
                "reason": f"卖出评分 {sell_score} < {SIGNAL_SELL_THRESHOLD}",
                "data_source": snap.get("data_source"),
            })
            continue
        shares = _target_buy_shares(snap, pf)
        if shares:
            pending = [p for p in pending if p.get("code") != code]
            pending.append({
                "signal_date": signal_date, "code": code, "action": "buy",
                "shares": shares,
                "buy_score": snap.get("buy_score", 0),
                "buy_signal": snap.get("buy_signal"),
                "kelly_pct": snap.get("kelly_final", 0),
                "reason": "买入评分 " + str(snap.get("buy_score", 0)) + ", Kelly " + str(snap.get("kelly_final", 0)) + "%",
                "data_source": snap.get("data_source"),
            })
    pf["pending_orders"] = pending
    pf["last_signal_date"] = signal_date
    return pf


def _execute_pending_orders(pf, bars, execution_date):
    """执行到期挂单：A 股按 T+1 开盘价成交，处理涨跌停/停牌/费用。"""
    today = execution_date.isoformat()
    if execution_date.weekday() >= 5:
        return pf  # 周末不执行，挂单顺延
    trades = pf.get("trades", [])
    skipped = pf.get("skipped_orders", [])
    pending = []
    for order in pf.get("pending_orders", []):
        sig_date = order.get("signal_date")
        if sig_date and sig_date >= today:
            pending.append(order)
            continue
        code = order["code"]
        bar = bars.get(code) if bars else None
        if bar is None:
            bar = _fetch_a_bar_for_date(code, execution_date)
        if bar is None or bar.get("suspended"):
            skipped.append({**order, "execution_date": today, "reason": "停牌/无当日行情"})
            continue
        limit_up, limit_down = _a_limit_prices(bar, code)
        price = float(bar["open"])
        if price <= 0:
            skipped.append({**order, "execution_date": today, "reason": "无有效开盘价"})
            continue
        if order["action"] == "buy":
            if limit_up and price >= limit_up:
                skipped.append({**order, "execution_date": today, "reason": "涨停无法买入"})
                continue
            shares = int(order.get("shares", 0) / MIN_LOT) * MIN_LOT
            if shares < MIN_LOT:
                skipped.append({**order, "execution_date": today, "reason": "不足一手"})
                continue
            fee = shares * price * _fee_rate()
            cost = shares * price + fee
            if pf["cash"] < cost:
                skipped.append({**order, "execution_date": today, "reason": "现金不足"})
                continue
            pf["cash"] -= cost
            pos = pf["positions"].get(code, {})
            old_shares = pos.get("shares", 0)
            old_cost = pos.get("avg_cost", price)
            new_shares = old_shares + shares
            new_avg = (old_shares * old_cost + shares * price) / new_shares if new_shares > 0 else price
            pf["positions"][code] = {"shares": new_shares, "avg_cost": round(new_avg, 3)}
            trades.append({
                "date": today, "signal_date": sig_date, "code": code, "action": "buy",
                "shares": shares, "price": round(price, 3), "amount": round(shares * price, 2),
                "fee": round(fee, 2), "fill_status": "filled",
                "buy_score": order.get("buy_score"), "buy_signal": order.get("buy_signal"),
                "kelly_pct": order.get("kelly_pct"),
                "reason": order.get("reason", ""), "data_source": order.get("data_source"),
            })
        elif order["action"] == "sell":
            if limit_down and price <= limit_down:
                skipped.append({**order, "execution_date": today, "reason": "跌停无法卖出"})
                continue
            shares = int(order.get("shares", 0) / MIN_LOT) * MIN_LOT
            if shares < MIN_LOT:
                skipped.append({**order, "execution_date": today, "reason": "不足一手"})
                continue
            fee = shares * price * (_fee_rate() + _stamp_tax_rate())
            proceeds = shares * price - fee
            if proceeds < 0:
                skipped.append({**order, "execution_date": today, "reason": "费用超过卖出金额"})
                continue
            pf["cash"] += proceeds
            pos = pf["positions"].get(code, {})
            remaining = max(0, pos.get("shares", 0) - shares)
            pf["positions"][code] = {
                "shares": remaining, "avg_cost": pos.get("avg_cost", price) if remaining else 0}
            trades.append({
                "date": today, "signal_date": sig_date, "code": code, "action": "sell",
                "shares": shares, "price": round(price, 3), "amount": round(shares * price, 2),
                "fee": round(fee, 2), "fill_status": "filled",
                "sell_score": order.get("sell_score"), "sell_signal": order.get("sell_signal"),
                "reason": order.get("reason", ""), "data_source": order.get("data_source"),
            })
        else:
            pending.append(order)
    pf["trades"] = trades
    pf["pending_orders"] = pending
    pf["skipped_orders"] = skipped[-200:]
    return pf


def _fetch_execution_bars(orders, execution_date):
    """为到期挂单预取当日 K 线，减少重复请求。"""
    today = execution_date.isoformat()
    codes = {o.get("code") for o in orders if o.get("signal_date") and o.get("signal_date") < today}
    return {code: _fetch_a_bar_for_date(code, execution_date) for code in codes}




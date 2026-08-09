"""
Shadow Account — 交易行为分析
基于成本分层买入记录 + 可选卖出记录，分析交易行为特征。
"""
import json
import logging
import os
from datetime import datetime

import pandas as pd

from modules.config import WATCH_LIST
from modules.cost_tier import load_all as load_cost_tiers

logger = logging.getLogger(__name__)
# 卖出记录存储
TRADE_FILE = os.path.join(os.path.dirname(__file__), "..", "notes", "trades.json")


def _load_trades():
    if not os.path.exists(TRADE_FILE):
        return {}
    with open(TRADE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_trades(data):
    os.makedirs(os.path.dirname(TRADE_FILE), exist_ok=True)
    with open(TRADE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_sell(code, date, shares, price, note=""):
    """记录卖出交易; 若(日期,股数,价格)完全相同则跳过, 返回是否新增"""
    trades = _load_trades()
    if code not in trades:
        trades[code] = {"sells": []}
    for s in trades[code]["sells"]:
        if (s.get("date"), s.get("shares"), s.get("price")) == (date, shares, price):
            return False
    trades[code]["sells"].append({
        "date": date, "shares": shares, "price": price, "note": note
    })
    _save_trades(trades)
    return True


def delete_sell(code, index):
    """删除卖出记录"""
    trades = _load_trades()
    if code in trades and 0 <= index < len(trades[code]["sells"]):
        trades[code]["sells"].pop(index)
        _save_trades(trades)


def get_sells(code):
    """获取某只股票的卖出记录"""
    trades = _load_trades()
    return trades.get(code, {}).get("sells", [])


def get_all_trades():
    """获取所有交易记录（买入+卖出）"""
    cost_data = load_cost_tiers()
    sell_data = _load_trades()
    all_trades = []
    for code, stock in cost_data.items():
        name = WATCH_LIST.get(code, {}).get("name", code)
        for b in stock.get("batches", []):
            if b.get("shares", 0) <= 0:
                continue
            all_trades.append({
                "code": code, "name": name, "action": "buy",
                "date": b["date"], "shares": b["shares"],
                "price": b["cost"], "note": b.get("note", "")
            })
        for s in sell_data.get(code, {}).get("sells", []):
            all_trades.append({
                "code": code, "name": name, "action": "sell",
                "date": s["date"], "shares": s["shares"],
                "price": s["price"], "note": s.get("note", "")
            })
    df = pd.DataFrame(all_trades)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    return df


# ── 行为分析指标 ──

def analyze_holding_days(df_trades, current_date=None):
    """持仓天数分析"""
    if current_date is None:
        current_date = datetime.now()
    results = []
    for code in df_trades["code"].unique():
        stock_trades = df_trades[df_trades["code"] == code].sort_values("date")
        name = stock_trades["name"].iloc[0]
        buys = stock_trades[stock_trades["action"] == "buy"]
        sells = stock_trades[stock_trades["action"] == "sell"]

        total_bought = buys["shares"].sum()
        total_sold = sells["shares"].sum()
        remaining = total_bought - total_sold

        # 已平仓部分：用 FIFO 匹配买入和卖出
        holding_days_list = []
        buy_queue = list(buys.itertuples())
        for sell in sells.itertuples():
            shares_to_match = sell.shares
            while shares_to_match > 0 and buy_queue:
                buy = buy_queue[0]
                matched = min(shares_to_match, buy.shares)
                days = (sell.date - buy.date).days
                holding_days_list.append(days)
                shares_to_match -= matched
                if buy.shares <= matched:
                    buy_queue.pop(0)
                else:
                    buy_queue[0] = buy._replace(shares=buy.shares - matched)

        # 未平仓部分：从买入日到当前
        for buy in buy_queue:
            days = (current_date - buy.date).days
            holding_days_list.append(days)

        avg_days = sum(holding_days_list) / len(holding_days_list) if holding_days_list else 0
        results.append({
            "code": code, "name": name,
            "total_bought": int(total_bought),
            "total_sold": int(total_sold),
            "remaining": int(remaining),
            "avg_holding_days": round(avg_days, 0),
            "min_days": min(holding_days_list) if holding_days_list else 0,
            "max_days": max(holding_days_list) if holding_days_list else 0,
        })
    return pd.DataFrame(results)


def analyze_entry_timing(df_trades, df_portfolio):
    """
    入场时机分析：买入时价格在后续走势中的分位数
    分位数越低 = 买得越便宜 = 入场时机越好
    """
    results = []
    for code in df_trades["code"].unique():
        stock_trades = df_trades[df_trades["code"] == code]
        buys = stock_trades[stock_trades["action"] == "buy"]
        name = buys["name"].iloc[0] if not buys.empty else code

        # 从持仓数据获取当前价格
        current_price = 0
        if df_portfolio is not None and not df_portfolio.empty:
            row = df_portfolio[df_portfolio["code"] == code]
            if not row.empty:
                current_price = float(row["price"].iloc[0])

        entry_scores = []
        for buy in buys.itertuples():
            buy_price = buy.price
            # 简化：用当前价格 vs 买入价格比较
            if current_price > 0 and buy_price > 0:
                pnl_pct = (current_price - buy_price) / buy_price * 100
                entry_scores.append({
                    "date": buy.date.strftime("%Y-%m-%d"),
                    "buy_price": buy_price,
                    "current_price": current_price,
                    "pnl_pct": round(pnl_pct, 2),
                    "shares": buy.shares,
                })

        avg_pnl = sum(e["pnl_pct"] for e in entry_scores) / len(entry_scores) if entry_scores else 0
        winners = sum(1 for e in entry_scores if e["pnl_pct"] > 0)
        win_rate = winners / len(entry_scores) * 100 if entry_scores else 0

        results.append({
            "code": code, "name": name,
            "batches": len(entry_scores),
            "win_batches": winners,
            "win_rate": round(win_rate, 1),
            "avg_pnl_pct": round(avg_pnl, 2),
            "best_batch": max(entry_scores, key=lambda x: x["pnl_pct"]) if entry_scores else None,
            "worst_batch": min(entry_scores, key=lambda x: x["pnl_pct"]) if entry_scores else None,
        })
    return results


def analyze_add_behavior(df_trades):
    """
    加仓行为分析：加仓时价格 vs 首次建仓价格
    - 加仓价 < 建仓价 → 逢低加仓（好习惯）
    - 加仓价 > 建仓价 → 追涨加仓（需警惕）
    """
    results = []
    for code in df_trades["code"].unique():
        stock_trades = df_trades[df_trades["code"] == code].sort_values("date")
        buys = stock_trades[stock_trades["action"] == "buy"]
        if len(buys) < 2:
            continue
        name = buys["name"].iloc[0]
        first_price = buys["price"].iloc[0]
        adds = buys.iloc[1:]
        low_adds = sum(1 for p in adds["price"] if p < first_price)
        high_adds = sum(1 for p in adds["price"] if p >= first_price)
        avg_add_price = adds["price"].mean()
        results.append({
            "code": code, "name": name,
            "first_buy_date": buys["date"].iloc[0].strftime("%Y-%m-%d"),
            "first_price": first_price,
            "add_count": len(adds),
            "low_adds": low_adds,
            "high_adds": high_adds,
            "avg_add_price": round(avg_add_price, 2),
            "behavior": "逢低加仓" if low_adds > high_adds else "追涨加仓" if high_adds > low_adds else "均衡加仓",
        })
    return results


def analyze_disposition_effect(df_trades, df_portfolio):
    """
    处置效应检测：
    对比已卖出批次（如果有）的盈亏情况和持有天数。
    - 卖出的盈利仓位持有天数 < 卖出的亏损仓位持有天数 → 有处置效应
    - 没有卖出记录时，分析未平仓批次：浮盈的持仓天数 vs 浮亏的持仓天数
    """
    sell_data = _load_trades()
    has_sells = any(v.get("sells") for v in sell_data.values())

    if has_sells:
        # 有卖出记录：分析实际卖出行为
        results = []
        for code in df_trades["code"].unique():
            stock_trades = df_trades[df_trades["code"] == code].sort_values("date")
            sells = stock_trades[stock_trades["action"] == "sell"]
            buys = stock_trades[stock_trades["action"] == "buy"]
            if sells.empty or buys.empty:
                continue
            name = stock_trades["name"].iloc[0]

            # FIFO 匹配
            buy_queue = list(buys.itertuples())
            win_days = []
            loss_days = []
            for sell in sells.itertuples():
                shares_to_match = sell.shares
                while shares_to_match > 0 and buy_queue:
                    buy = buy_queue[0]
                    matched = min(shares_to_match, buy.shares)
                    pnl = (sell.price - buy.price) / buy.price * 100
                    days = (sell.date - buy.date).days
                    if pnl > 0:
                        win_days.append(days)
                    else:
                        loss_days.append(days)
                    shares_to_match -= matched
                    if buy.shares <= matched:
                        buy_queue.pop(0)
                    else:
                        buy_queue[0] = buy._replace(shares=buy.shares - matched)

            avg_win_days = sum(win_days) / len(win_days) if win_days else 0
            avg_loss_days = sum(loss_days) / len(loss_days) if loss_days else 0

            if avg_win_days > 0 and avg_loss_days > 0:
                effect = "有处置效应" if avg_win_days < avg_loss_days else "无处置效应"
            elif avg_win_days > 0:
                effect = "只卖盈利仓"
            elif avg_loss_days > 0:
                effect = "只卖亏损仓"
            else:
                effect = "数据不足"

            results.append({
                "code": code, "name": name,
                "avg_win_holding_days": round(avg_win_days, 0),
                "avg_loss_holding_days": round(avg_loss_days, 0),
                "win_sells": len(win_days),
                "loss_sells": len(loss_days),
                "effect": effect,
            })
        return results, True
    else:
        # 无卖出记录：分析未平仓浮盈/浮亏的持仓天数
        results = []
        current_date = datetime.now()
        for code in df_trades["code"].unique():
            stock_trades = df_trades[df_trades["code"] == code].sort_values("date")
            buys = stock_trades[stock_trades["action"] == "buy"]
            if buys.empty:
                continue
            name = buys["name"].iloc[0]

            current_price = 0
            if df_portfolio is not None and not df_portfolio.empty:
                row = df_portfolio[df_portfolio["code"] == code]
                if not row.empty:
                    current_price = float(row["price"].iloc[0])
            if current_price <= 0:
                continue

            win_days = []
            loss_days = []
            for buy in buys.itertuples():
                if buy.price <= 0:
                    continue
                pnl = (current_price - buy.price) / buy.price * 100
                days = (current_date - buy.date).days
                if pnl > 0:
                    win_days.append(days)
                else:
                    loss_days.append(days)

            avg_win_days = sum(win_days) / len(win_days) if win_days else 0
            avg_loss_days = sum(loss_days) / len(loss_days) if loss_days else 0

            if avg_win_days > 0 and avg_loss_days > 0:
                effect = "浮盈拿得久" if avg_win_days > avg_loss_days else "浮亏拿得久"
            elif avg_win_days > 0:
                effect = "全部浮盈"
            elif avg_loss_days > 0:
                effect = "全部浮亏"
            else:
                effect = "数据不足"

            results.append({
                "code": code, "name": name,
                "avg_win_holding_days": round(avg_win_days, 0),
                "avg_loss_holding_days": round(avg_loss_days, 0),
                "win_batches": len(win_days),
                "loss_batches": len(loss_days),
                "effect": effect,
            })
        return results, False


def analyze_chasing_behavior(df_trades, df_portfolio):
    """
    追涨杀跌检测：
    分析买入时机相对于近期走势的位置。
    需要历史价格数据来判断买入时是否在近期高点附近（追涨）。
    这里用简化版：对比买入价格与该股票所有批次的最低价。
    """
    results = []
    for code in df_trades["code"].unique():
        stock_trades = df_trades[df_trades["code"] == code].sort_values("date")
        buys = stock_trades[stock_trades["action"] == "buy"]
        if buys.empty:
            continue
        name = buys["name"].iloc[0]
        prices = buys["price"].tolist()
        min_price = min(prices)
        max_price = max(prices)
        price_range = max_price - min_price if max_price > min_price else 1

        # 最近一次买入的价格分位数（0=最低，1=最高）
        last_price = prices[-1]
        price_pctl = (last_price - min_price) / price_range if price_range > 0 else 0.5

        if price_pctl > 0.7:
            behavior = "可能在追涨"
        elif price_pctl < 0.3:
            behavior = "可能在抄底"
        else:
            behavior = "入场价位适中"

        results.append({
            "code": code, "name": name,
            "first_price": prices[0],
            "last_price": last_price,
            "min_price": min_price,
            "max_price": max_price,
            "last_price_pctl": round(price_pctl * 100, 1),
            "behavior": behavior,
        })
    return results


def _market_of(code):
    """粗略判断市场：5位数字为港股，其余数字为A股，字母为美股"""
    if str(code).isdigit():
        return "HK" if len(str(code)) == 5 else "A"
    return "US"


def compute_account_drawdown(df_trades, years=5):
    """根据交易记录 + 历史行情(后复权)构建影子账户净值曲线, 计算最大回撤。

    Returns:
        dict: max_drawdown(%, 正数), peak_date, trough_date, covered, nav_df
    """
    from modules.backtest import fetch_history

    empty = {"max_drawdown": None, "peak_date": None, "trough_date": None,
             "covered": 0, "nav_df": None}
    if df_trades is None or df_trades.empty:
        return empty

    codes = [c for c in df_trades["code"].unique() if _market_of(c) != "US"]
    if not codes:
        return empty

    min_date = df_trades["date"].min()
    span_years = max(1, min(years, max(1, (datetime.now() - min_date).days // 365 + 1)))

    frames = []
    for code in codes:
        sub = df_trades[df_trades["code"] == code].sort_values("date")
        if sub.empty:
            continue
        try:
            hist = fetch_history(code, years=span_years, mkt=_market_of(code))
        except Exception:
            hist = None
        if hist is None or hist.empty:
            continue
        hist = hist[hist.index >= sub["date"].min()].sort_index()
        if hist.empty:
            continue
        pos = pd.Series(0.0, index=hist.index)
        cur = 0.0
        for t in sub.itertuples():
            cur += float(t.shares) if t.action == "buy" else -float(t.shares)
            pos.loc[pos.index >= t.date] = cur
        frames.append((pos * hist["close"]).rename(code))

    if not frames:
        return empty

    value = pd.concat(frames, axis=1).fillna(0.0).sum(axis=1)
    if len(value) < 2 or value.iloc[0] <= 0:
        return empty

    nav = value / value.iloc[0]
    drawdown = (nav - nav.cummax()) / nav.cummax()
    trough_date = drawdown.idxmin()
    peak_date = nav.loc[:trough_date].idxmax()
    nav_df = pd.DataFrame({"净值": nav})
    return {
        "max_drawdown": round(float(drawdown.min()) * 100, 2),
        "peak_date": peak_date.strftime("%Y-%m-%d") if peak_date is not pd.NaT else None,
        "trough_date": trough_date.strftime("%Y-%m-%d") if trough_date is not pd.NaT else None,
        "covered": len(frames),
        "nav_df": nav_df,
    }


def generate_summary(df_trades, df_portfolio):
    """生成行为分析综合报告"""
    if df_trades.empty:
        return None

    holding = analyze_holding_days(df_trades)
    entry = analyze_entry_timing(df_trades, df_portfolio)
    adds = analyze_add_behavior(df_trades)
    disposition, has_sells = analyze_disposition_effect(df_trades, df_portfolio)
    chasing = analyze_chasing_behavior(df_trades, df_portfolio)
    try:
        dd = compute_account_drawdown(df_trades)
    except Exception:
        dd = {"max_drawdown": None, "peak_date": None, "trough_date": None,
              "covered": 0, "nav_df": None}

    # 综合评分
    total_batches = len(df_trades[df_trades["action"] == "buy"])
    total_stocks = df_trades["code"].nunique()
    avg_holding = holding["avg_holding_days"].mean() if not holding.empty else 0

    # 入场胜率
    entry_win_rates = [e["win_rate"] for e in entry if e["batches"] > 0]
    overall_win_rate = sum(entry_win_rates) / len(entry_win_rates) if entry_win_rates else 0

    # 加仓行为
    low_add_stocks = sum(1 for a in adds if a["behavior"] == "逢低加仓")
    chase_add_stocks = sum(1 for a in adds if a["behavior"] == "追涨加仓")

    # 追涨行为
    chase_stocks = sum(1 for c in chasing if "追涨" in c["behavior"])

    return {
        "total_stocks": total_stocks,
        "total_batches": total_batches,
        "avg_holding_days": round(avg_holding, 0),
        "overall_win_rate": round(overall_win_rate, 1),
        "low_add_stocks": low_add_stocks,
        "chase_add_stocks": chase_add_stocks,
        "chase_entry_stocks": chase_stocks,
        "has_sell_records": has_sells,
        "max_drawdown": dd["max_drawdown"],
        "dd_peak_date": dd["peak_date"],
        "dd_trough_date": dd["trough_date"],
        "dd_covered": dd["covered"],
        "nav_df": dd["nav_df"],
        "holding_detail": holding,
        "entry_detail": entry,
        "add_detail": adds,
        "disposition_detail": disposition,
        "chasing_detail": chasing,
    }

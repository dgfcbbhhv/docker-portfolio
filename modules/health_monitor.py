import datetime, numpy as np, pandas as pd
from .data.cache import retry

try:
    import akshare as ak
except ImportError:
    ak = None

def capital_flow(code, name="", days=20):
    """方向一：主力资金流向监控
    返回: {"net_amount": 累计净流入, "net_amount_avg": 日均, "trend": 趋势,
           "alert": 是否预警, "detail": 明细DataFrame}
    """
    result = {"net_amount": None, "net_amount_avg": None,
              "trend": "数据不足", "alert": False, "detail": pd.DataFrame()}
    if ak is None:
        return result
    mkt = "sh" if code.startswith(("5", "6")) else "sz"
    try:
        df = retry(lambda: ak.stock_individual_fund_flow(stock=code, market=mkt))
        if df is None or df.empty:
            return result
        df = df.sort_values("日期", ascending=False).head(days).copy()
        if "主力净流入" in df.columns:
            df["主力净流入"] = pd.to_numeric(df["主力净流入"], errors="coerce")
            net = df["主力净流入"].sum()
            avg = df["主力净流入"].mean()
            last3 = df.head(3)["主力净流入"].mean()
            if net < 0 and abs(net) > 1e7:
                trend = "持续流出" if avg < -1e6 else "流出衰竭"
                alert = avg < -2e6 and last3 < avg * 0.5
            elif net > 0:
                trend = "净流入"
                alert = False
            else:
                trend = "中性"
                alert = False
            result.update({
                "net_amount": round(net / 1e4, 1),
                "net_amount_avg": round(avg / 1e4, 1),
                "trend": trend,
                "alert": alert,
                "detail": df,
            })
    except Exception:
        pass
    return result


def market_sentiment():
    """方向二：全市场涨跌家数比
    返回: {"up": 上涨家数, "down": 下跌家数, "ratio": 涨跌比,
           "zone": 情绪区(恐慌/正常/亢奋), "advice": 建议}
    """
    result = {"up": 0, "down": 0, "ratio": 0, "zone": "数据不足", "advice": ""}
    if ak is None:
        return result
    try:
        df = retry(lambda: ak.stock_zh_a_spot_em())
        if df is None or df.empty:
            return result
        chg_col = None
        for col in df.columns:
            if "涨幅" in col or "涨跌幅" in col:
                chg_col = col
                break
        if chg_col is None:
            return result
        df[chg_col] = pd.to_numeric(df[chg_col], errors="coerce")
        up = int((df[chg_col] > 0).sum())
        down = int((df[chg_col] < 0).sum())
        ratio = round(up / down, 2) if down > 0 else 99
        if ratio < 0.25:
            zone, advice = "极度恐慌", "错杀区，可加倍定投/加仓红利股"
        elif ratio < 0.5:
            zone, advice = "恐慌", "偏低，可适度加仓"
        elif ratio < 2:
            zone, advice = "正常", "中性，按常规节奏操作"
        elif ratio < 4:
            zone, advice = "偏热", "偏高，暂停新增买入，持有现金等待"
        else:
            zone, advice = "极度亢奋", "狂热，红利股可能跑输，持现观望"
        result.update({"up": up, "down": down, "ratio": ratio, "zone": zone, "advice": advice})
    except Exception:
        pass
    return result


def ma250_deviation(code, mkt="A"):
    """方向三：股价偏离年线(250日)幅度
    返回: {"ma250": 年线值, "deviation_pct": 偏离百分比,
           "zone": 位置(靠近年线/适中/远离), "priority": 加仓优先级}
    """
    result = {"ma250": None, "deviation_pct": None, "zone": "数据不足", "priority": 0}
    from .data.market import fetch_sma
    try:
        price = None
        ma250 = fetch_sma(code, 250, mkt)
        if ma250 is None or ma250 <= 0:
            return result
        spot = retry(lambda: ak.stock_zh_a_spot_em()) if ak else None
        if spot is not None and not spot.empty:
            spot = spot[spot["代码"] == code]
            if not spot.empty:
                price = float(spot["最新价"].iloc[0])
        if price is None or price <= 0:
            return result
        dev = round((price - ma250) / ma250 * 100, 2)
        if abs(dev) < 5:
            zone, priority = "靠近年线", 3
        elif abs(dev) < 15:
            zone, priority = "适中区间", 2
        elif abs(dev) < 30:
            zone, priority = "偏离较大", 1
        else:
            zone, priority = "大幅偏离", 0
        result.update({
            "ma250": round(ma250, 2),
            "deviation_pct": dev,
            "zone": zone,
            "priority": priority,
        })
    except Exception:
        pass
    return result


def dividend_momentum(code, months=12):
    """分红日历动量：检查除息日缺口回补情况
    返回: {"ex_date": 最近除息日, "gap_pct": 除息缺口幅度,
           "days_since_ex": 距今天数, "recovered": 是否回补,
           "status": 状态描述, "alert": 是否预警}
    """
    result = {"ex_date": None, "gap_pct": None, "days_since_ex": None,
              "recovered": None, "status": "数据不足", "alert": False}
    if ak is None:
        return result
    try:
        df = retry(lambda: ak.stock_fhps_detail_em(symbol=code))
        if df is None or df.empty:
            return result
        df = df[df["实施方案公告日期"].notna()].sort_values("实施方案公告日期", ascending=False)
        if df.empty:
            return result
        latest = df.iloc[0]
        ex_date_str = latest.get("股权登记日", None) or latest.get("实施方案公告日期", None)
        if ex_date_str is None:
            return result
        ex_date = pd.to_datetime(ex_date_str)
        today = datetime.datetime.now()
        days_since = (today - ex_date).days
        if days_since < 0:
            return result
        from .data.market import _fetch_hist_em_or_tx
        start = (ex_date - datetime.timedelta(days=30)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        hist = _fetch_hist_em_or_tx(code, start, end)
        if hist is None or hist.empty:
            return result
        hist = hist.sort_values("date")
        ex_idx = hist[hist["date"] >= ex_date].index
        if len(ex_idx) == 0:
            return result
        ex_idx = ex_idx[0]
        ex_close = hist.loc[ex_idx, "close"]
        div_per_share_val = float(latest.get("每10股分红", 0)) / 10.0
        gap_pct = round(div_per_share_val / ex_close * 100, 2) if ex_close > 0 else 0
        future = hist.loc[ex_idx:]
        if len(future) > 1:
            max_close = future["close"].max()
            recovered = max_close >= ex_close
        else:
            recovered = None
        if recovered is True:
            status = "已回补"
            alert = False
        elif recovered is False and days_since > 60:
            status = "60日未回补，建议复查基本面"
            alert = True
        elif recovered is False:
            status = f"除息后{days_since}日，尚未回补（观察中）"
            alert = False
        else:
            status = "除息后数据不足"
            alert = False
        result.update({
            "ex_date": ex_date_str,
            "gap_pct": gap_pct,
            "days_since_ex": days_since,
            "recovered": recovered,
            "status": status,
            "alert": alert,
        })
    except Exception:
        pass
    return result


def run_all(code, name=""):
    """对单只股票运行全部健康检查
    返回 dict: {capital_flow, sentiment, ma250, dividend_momentum}
    """
    return {
        "capital_flow": capital_flow(code, name),
        "sentiment": market_sentiment(),
        "ma250": ma250_deviation(code),
        "dividend_momentum": dividend_momentum(code),
    }


def overall_health(portfolio_df, cost_data):
    """对整个持仓运行健康度评估
    返回: {summary: str, alert_count: int, gold_add: [], silver_hold: [], warning_stop: []}
    """
    sentiment = market_sentiment()
    alerts = 0
    gold_add, silver_hold, warning_stop = [], [], []
    for _, r in portfolio_df.iterrows():
        code = r["code"]
        ma_info = ma250_deviation(code)
        alert = ma_info.get("alert", False)
        if alert:
            alerts += 1
        from .cost_tier import calc_overall_tier
        tier_info = calc_overall_tier(code, float(r["price"]))
        tier = tier_info.get("tier", "白银")
        item = {"code": code, "name": r["name"], "tier": tier,
                "ma_dev": ma_info.get("deviation_pct"), "ma_zone": ma_info.get("zone")}
        if tier == "黄金":
            gold_add.append(item)
        elif tier == "白银":
            silver_hold.append(item)
        else:
            warning_stop.append(item)
    return {
        "sentiment": sentiment,
        "alert_count": alerts,
        "gold_add": gold_add,
        "silver_hold": silver_hold,
        "warning_stop": warning_stop,
    }
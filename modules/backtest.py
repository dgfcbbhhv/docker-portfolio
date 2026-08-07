"""回测引擎 — 三场景 + 幸存者偏差声明

假设:
- 回测基于历史后复权数据
- 未剔除退市股票, 存在幸存者偏差
- 交易成本: 单边 0.1%
- 无风险利率: 2.5% (10年国债)
"""
import datetime, json, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
import pandas as pd
import akshare as ak
from .data.cache import load, save, retry

_MAX_WORKERS = 8
_REQ_TIMEOUT = 180

_TX_KLINE_URL = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="

def _tx_code(code):
    if code.startswith("6"):
        return "sh" + code
    return "sz" + code

def _fetch_tx_history(code, days=1500):
    """腾讯K线回退"""
    try:
        url = _TX_KLINE_URL + f"{_tx_code(code)},day,,,{days},qfq"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
        data = json.loads(raw)
        items = data.get("data", {}).get(_tx_code(code), {}).get("qfqday", [])
        if not items:
            return None
        rows = []
        for item in items:
            rows.append({"date": item[0], "open": float(item[1]),
                         "close": float(item[2]), "high": float(item[3]),
                         "low": float(item[4]), "volume": float(item[5]) * 100})
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        return df
    except Exception:
        return None


def fetch_history(code, years=5, mkt="A"):
    """拉取历史行情(后复权), 带缓存; EM失败时回退腾讯"""
    k = f"{code}_hist{years}"
    cached = load("hist", k)
    if cached is not None:
        if "date" in cached.columns:
            cached["date"] = pd.to_datetime(cached["date"])
            cached = cached.set_index("date").sort_index()
        return cached

    end = datetime.date.today()
    start = end - datetime.timedelta(days=years * 365)
    df = None
    if mkt == "A":
        try:
            df = retry(lambda: ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"), adjust="qfq"))
        except Exception:
            pass
        if df is None or df.empty:
            tx = _fetch_tx_history(code, max(years * 365, 500))
            if tx is not None:
                mask = (tx["date"] >= pd.Timestamp(start)) & (tx["date"] <= pd.Timestamp(end))
                df = tx[mask] if mask.any() else tx
    else:
        try:
            df = retry(lambda: ak.stock_hk_hist(
                symbol=code, period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"), adjust="qfq"))
        except Exception:
            return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

    # 兼容EM(日期/收盘)和腾讯(date/close)列名
    if "date" not in df.columns and "日期" in df.columns:
        df = df.rename(columns={"日期": "date", "收盘": "close"})
    elif "close" not in df.columns and "收盘" in df.columns:
        df = df.rename(columns={"收盘": "close"})
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
    elif not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    save("hist", k, df.reset_index())
    return df


def fetch_histories(codes, years=5, mkt="A"):
    """并行批量拉取历史行情"""
    r = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        fut = {pool.submit(fetch_history, c, years, mkt): c for c in codes}
        for f in as_completed(fut):
            try:
                c = fut[f]
                df = f.result(timeout=_REQ_TIMEOUT)
                if not df.empty:
                    r[c] = df
            except Exception:
                pass
    return r


def backtest_portfolio(codes, weights, years=5, cost=0.001, risk_free=0.02):
    """
    回测组合

    参数:
        codes: list of str
        weights: dict {code: weight_pct}
        years: 回测年数
        cost: 单边交易成本
        risk_free: 无风险利率

    返回 dict 或 None
    """
    all_data = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        fut = {pool.submit(fetch_history, c, years): c for c in codes}
        for f in as_completed(fut, timeout=_REQ_TIMEOUT):
            try:
                c = fut[f]
                df = f.result()
                if not df.empty and "close" in df.columns:
                    all_data[c] = df["close"]
            except Exception:
                pass

    if not all_data:
        return None

    prices = pd.DataFrame(all_data)
    if not isinstance(prices.index, pd.DatetimeIndex):
        prices.index = pd.to_datetime(prices.index)
    prices = prices.dropna(how="all").ffill()
    returns = prices.pct_change().dropna()

    if returns.empty:
        return None

    total_w = sum(weights.get(c, 0) for c in returns.columns)
    if total_w <= 0:
        return None
    w = {c: weights.get(c, 0) / total_w for c in returns.columns}

    port_ret = sum(returns[c] * w[c] for c in returns.columns)

    # 净值曲线
    nav = (1 + port_ret).cumprod()

    # 月度收益
    monthly_ret = port_ret.resample("ME").apply(lambda x: (1 + x).prod() - 1)
    monthly_ret = monthly_ret - cost * 2  # 双边成本

    # 指标
    total_return = nav.iloc[-1] / nav.iloc[0] - 1
    # 年化按实际数据跨度，避免传入 years 大于实际数据时高估收益
    span_days = max(int((nav.index[-1] - nav.index[0]).days), 1)
    years_actual = span_days / 365.25
    annual_return = (1 + total_return) ** (1 / years_actual) - 1 if years_actual > 0 else 0
    annual_vol = port_ret.std() * np.sqrt(252)
    sharpe = (annual_return - risk_free) / annual_vol if annual_vol > 0 else 0

    # 最大回撤
    cummax = nav.cummax()
    drawdown = (nav - cummax) / cummax
    max_dd = drawdown.min()

    # 月度胜率
    win_rate = (monthly_ret > 0).sum() / len(monthly_ret) if len(monthly_ret) > 0 else 0

    # 年化换手率 (简化: 按月调仓)
    annual_rebalances = 12
    turnover = annual_rebalances * 2  # 每月双边换手次数
    annual_cost = cost * turnover  # 单位与 cost 一致（小数）

    # 扣费后年化
    annual_return_net = annual_return - annual_cost

    # 回撤归因
    dd_start = drawdown.idxmin()
    dd_period = returns.loc[:dd_start].tail(60) if dd_start in returns.index else returns.tail(60)
    dd_attr = {}
    for c in dd_period.columns:
        if w.get(c, 0) > 0:
            dd_attr[c] = round(dd_period[c].sum() * w.get(c, 0) * 100, 2)

    return {
        "nav": nav,
        "monthly_returns": monthly_ret,
        "metrics": {
            "total_return": round(total_return * 100, 2),
            "annual_return": round(annual_return * 100, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown": round(max_dd * 100, 2),
            "annual_vol": round(annual_vol * 100, 2),
            "win_rate": round(win_rate * 100, 1),
            "turnover": round(turnover, 1),
            "annual_cost": round(annual_cost * 100, 2),
            "annual_return_net": round(annual_return_net * 100, 2),
        },
        "drawdown_attr": dd_attr,
    }


def backtest_scenarios(port_a, port_hk, fins, years=5):
    """
    三场景回测:
      bull: 只保留李氏评分 >= 70 的持仓
      base: 当前持仓不变
      bear: 剔除李氏评分 < 50 的持仓

    注意: lishing_scores 需要从外部传入
    这里简化: 用 roe > 10 作为近似高评分判断
    """
    base_weights = {}
    for c, cfg in port_a.items():
        base_weights[c] = cfg.get("ratio", 0) or 0

    # 乐观: 高ROE加权
    bull_weights = {}
    for c, cfg in port_a.items():
        fi = fins.get(c, {})
        roe = fi.get("roe", 0) or 0
        if roe > 10:
            bull_weights[c] = (cfg.get("ratio", 0) or 0) * 1.2
        elif roe > 5:
            bull_weights[c] = cfg.get("ratio", 0) or 0

    # 悲观: 剔除低ROE
    bear_weights = {}
    for c, cfg in port_a.items():
        fi = fins.get(c, {})
        roe = fi.get("roe", 0) or 0
        if roe > 5:
            bear_weights[c] = cfg.get("ratio", 0) or 0

    codes = list(port_a.keys())
    results = {}
    for scenario, weights in [("bull", bull_weights), ("base", base_weights), ("bear", bear_weights)]:
        r = backtest_portfolio(codes, weights, years=years)
        if r:
            results[scenario] = r

    return results

"""
历史牛熊信号回测 — 验证买卖信号(建仓/加仓/减仓)在牛熊市中的真实性。

方法:
- 对 watchlist 每只标的拉取 5 年历史行情(后复权)
- 滚动计算每日信号: 价格分位(pctl)、MA20/MA60 均线、Kelly(用当前基本面近似)
- 用与生产一致的门槛 (BUY>=65, SELL<55) 判定 buy/hold/sell
- 统计信号后 5/10/20 个交易日涨幅, 计算胜率(ret>0 且超基准) 与平均收益
- 用市场基准指数(中证红利/恒生/标普) 判断牛熊分段, 分别统计

限制声明:
- 基本面(ROE/股息/估值) 使用当前值, 无法回溯历史, 属近似
- 价格分位用历史滚动窗口近似, 非真实历史分位数
- 未剔除退市/被 ST 股票, 存在幸存者偏差
- 交易成本: buy 信号按市场收取往返成本(A股≈0.10%, 港股≈0.70%, 美股≈0.05%), sell/hold 不计
- 重叠窗口: 同时提供按"信号事件簇"(连续同信号第一天)聚合的统计, 降低样本虚增
- sell 口径: 简化卖出信号(高估值+动量破位, sell_score>=55)与生产 evaluate_sell(持有强度,
  低分才卖)方向不同, 不能直接视为生产卖出信号; sell 胜率定义为"卖出后 20 日内下跌
  或跑输基准为胜"(与 buy/hold 相反)
- 结果仅用于评估信号逻辑的有效性, 不构成投资建议
"""
import argparse
import datetime
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/app")
from modules.backtest import fetch_history, fetch_histories
from modules.config import WATCH_LIST, MANUAL_DIV_YIELDS
from modules.data.cache import load as cache_load

BUY_TH = 65
SELL_TH = 55
HORIZONS = [5, 10, 20]

# 单次往返交易成本(百分比): A股=佣金万2.5+过户费+印花税0.05%单边卖出; 港股=佣金0.25%x2+印花税0.1%x2;
# 美股=免佣金, 仅少量规费+滑点。buy 信号计往返成本(信号日建仓, 观察期末平仓), sell/hold 不计。
TRADE_COST = {"A": 0.10, "HK": 0.70, "US": 0.05}

# 市场基准 (用于牛熊判定与超额收益)
_BENCH = {
    "A": {"code": "000922", "mkt": "A", "name": "中证红利"},
    "HK": {"code": "HSI", "mkt": "HK", "name": "恒生指数"},
    "US": {"code": "SPX", "mkt": "US", "name": "标普500"},
}


def _fetch_tx_index(code, days=1500):
    """腾讯K线指数(带 sh/sz 前缀)回退"""
    import urllib.request
    import json
    try:
        url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{days},"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
        d = json.loads(raw)
        data = d.get("data", {}).get(code, {})
        items = data.get("day", []) or data.get("qfqday", [])
        if not items:
            return None
        rows = [{"date": it[0], "close": float(it[2])} for it in items]
        import pandas as pd
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date").sort_index()["close"]
    except Exception:
        return None


def _fetch_tx_hk_stock(code, days=1500):
    """腾讯K线港股个股 (hk5位前缀), qfq。返回 (df) 或 None"""
    import urllib.request
    import json
    hk = f"hk{code}"
    url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={hk},day,,,{days},qfq"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
        d = json.loads(raw)
        data = d.get("data", {}).get(hk, {})
        items = data.get("qfqday", []) or data.get("day", [])
        if not items:
            return None
        rows = [{"date": it[0], "close": float(it[2])} for it in items]
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date").sort_index()
    except Exception:
        return None


def _fetch_fmp_us_stock(code, days=1500):
    """FMP 美股历史K线 (historical-price-eod/full)。带 30 天磁盘缓存。

    缓存 key: fmp_hist|{code}; 命中则直接返回, 避免重复消耗 FMP 配额。
    """
    import requests
    import os
    from modules.data.cache import load as c_load, save as c_save
    from modules.data.fmp import bump_fmp_usage

    sym = str(code).split(".")[0].upper()
    ck = f"fmp_hist|{sym}"
    cached = c_load("fmp_hist", ck, allow_expired=True)
    if cached is not None:
        if "date" in cached.columns:
            cached["date"] = pd.to_datetime(cached["date"])
            cached = cached.set_index("date").sort_index()
            if len(cached) > days:
                cached = cached.tail(days)
            return cached

    key = os.environ.get("FMP_API_KEY", "").strip()
    if not key:
        return None
    try:
        r = requests.get(
            "https://financialmodelingprep.com/stable/historical-price-eod/full",
            params={"apikey": key, "symbol": sym}, timeout=25)
        bump_fmp_usage(sym)
        if r.status_code != 200:
            return None
        j = r.json()
        if not isinstance(j, list) or not j:
            return None
        rows = [{"date": it["date"], "close": float(it["close"]),
                 "open": float(it.get("open") or 0),
                 "high": float(it.get("high") or 0),
                 "low": float(it.get("low") or 0),
                 "volume": float(it.get("volume") or 0)} for it in j]
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
        if len(df) > days:
            df = df.tail(days)
        try:
            c_save("fmp_hist", ck, df)
        except Exception as _e:
            print(f"[fmp] cache save failed {sym}: {_e}")
        return df.set_index("date")
    except Exception:
        return None


def fetch_bench(mkt):
    """拉取市场基准历史(腾讯指数K线)。返回 close series 或 None。"""
    try:
        if mkt == "A":
            return _fetch_tx_index("sh000922")  # 中证红利
        elif mkt == "HK":
            return _fetch_tx_index("hkHSI")  # 恒生指数
        else:
            df = _fetch_fmp_us_stock("SPY")  # 标普500 ETF
            return df["close"] if df is not None else None
    except Exception:
        return None
    return None


def kelly_approx(price, roe, div_yield, vol=0.2, debt=0):
    """Kelly 简化版 (与 kelly.py 一致的核心公式)。"""
    div = div_yield or 0
    p = 0.50 + 0.30 * min(div / 5.0, 1) + 0.20 * min(max(roe or 0, 0) / 15.0, 1)
    p = max(0.05, min(p, 0.95))
    b = 1.0  # 无 PB 历史时用保守赔率
    q = 1 - p
    k_raw = (b * p - q) / b if b > 0 else 0
    kelly = max(k_raw, 0) * 0.25  # quarter kelly
    return kelly


def build_signal_series(df, fin):
    """对单只标的的历史行情计算信号序列。"""
    close = df["close"]
    ret = close.pct_change()

    # 价格分位: 滚动 250 日
    rolling = close.rolling(250, min_periods=60)
    pctl = (close - rolling.min()) / (rolling.max() - rolling.min()) * 100
    pctl = pctl.clip(0, 100).fillna(50)

    ma20 = close.rolling(20, min_periods=5).mean()
    ma60 = close.rolling(60, min_periods=10).mean()

    vol = ret.rolling(250, min_periods=60).std() * np.sqrt(252)
    vol = vol.fillna(0.2).clip(0.05, 1.0)

    roe = fin.get("roe") or 0
    dy = (MANUAL_DIV_YIELDS.get(df.attrs.get("code", ""), {}) or {}).get("ttm") or 0

    kelly = kelly_approx(close, roe, dy, vol)

    out = pd.DataFrame({
        "close": close,
        "pctl": pctl,
        "ma20": ma20,
        "ma60": ma60,
        "vol": vol,
        "kelly": kelly,
    })
    out["price_above_ma20"] = out["close"] > out["ma20"]
    out["ma20_above_ma60"] = out["ma20"] > out["ma60"]
    return out


def classify(buy_score, sell_score):
    if buy_score >= BUY_TH:
        return "buy"
    if sell_score >= SELL_TH:
        return "sell"
    return "hold"


def regime_of(date, bench_close):
    """用基准 250 日均线判定牛/熊。"""
    s = bench_close
    if s is None or s.empty:
        return None
    past = s.loc[:date]
    if len(past) < 60:
        return None
    ma = past.tail(250).mean()
    if np.isnan(ma):
        return None
    return "bull" if past.iloc[-1] > ma else "bear"


def _forward_stats(sig, i, h, bench):
    """第 i 天信号后 h 个交易日的收益/超额收益(毛)。point-in-time, 不含未来信息。"""
    dates = sig.index
    j = i + h
    if j >= len(dates):
        return None
    price = sig["close"].iloc[i]
    future = sig["close"].iloc[j]
    if price <= 0 or future <= 0:
        return None
    ret = (future - price) / price * 100
    bench_ret = None
    if bench is not None:
        past = bench.loc[:dates[i]]
        if len(past) >= 1:
            b0 = past.iloc[-1]
            b1_series = bench.loc[dates[j]:dates[j]]
            if len(b1_series) >= 1:
                b1 = b1_series.iloc[0]
                if b0 and b1:
                    bench_ret = (b1 - b0) / b0 * 100
    excess = ret - bench_ret if bench_ret is not None else None
    return {"ret": ret, "bench_ret": bench_ret, "excess": excess}


def _make_rec(st, cost, is_sell=False):
    """把一次前向观察转成统计记录, 同时保留毛/净(扣成本)两套口径。"""
    ret = st["ret"]
    bench_ret = st["bench_ret"]
    excess = st["excess"]
    net_ret = ret - cost
    net_excess = (net_ret - bench_ret) if bench_ret is not None else None
    if is_sell:
        # 卖出信号: 卖出后下跌/跑输基准才算"卖对了"
        win = ret <= 0
        excess_win = excess is not None and excess <= 0
        net_win = net_ret <= 0
        net_excess_win = net_excess is not None and net_excess <= 0
    else:
        win = ret > 0
        excess_win = excess is not None and excess > 0
        net_win = net_ret > 0
        net_excess_win = net_excess is not None and net_excess > 0
    return {
        "total": 1,
        "up": 1 if ret > 0 else 0,
        "win": 1 if win else 0,
        "excess_win": 1 if excess_win else 0,
        "net_win": 1 if net_win else 0,
        "net_excess_win": 1 if net_excess_win else 0,
        "rets": [ret],
        "excess": [excess] if excess is not None else [],
        "net_rets": [net_ret],
        "net_excess": [net_excess] if net_excess is not None else [],
    }


def run_backtest(codes, years=5, min_hist=200):
    results = {
        "generated_at": datetime.datetime.now().isoformat(),
        "note": "历史牛熊信号回测",
        "by_signal": {},
        "by_regime": {},
        "by_horizon": {},
        "by_event": {},
        "by_regime_event": {},
        "per_stock": {},
        "cost": TRADE_COST,
        "sell_note": "sell 信号为简化近似(高估值+动量破位), 与生产 evaluate_sell 方向不同; "
                     "sell 胜率=卖出后下跌/跑输基准为胜",
        "bench": {},
    }

    # 市场分组
    groups = {}
    for c, meta in WATCH_LIST.items():
        m = meta.get("market", "A")
        groups.setdefault(m, []).append(c)
    # 用户指定 codes 时覆盖
    if codes:
        groups = {"A": [], "HK": [], "US": []}
        for c in codes:
            m = WATCH_LIST.get(c, {}).get("market", "A")
            groups.setdefault(m, []).append(c)

    # 拉基准
    benches = {}
    for m in groups:
        benches[m] = fetch_bench(m)

    # 基本面
    fins = {}
    for m, cs in groups.items():
        for c in cs:
            df = cache_load("fin", c)
            if df is not None and len(df):
                fins[c] = {k: v for k, v in df.iloc[0].to_dict().items() if k != "_ts"}

    for m, cs in groups.items():
        bench = benches.get(m)
        hists = fetch_histories(cs, years=years, mkt="H" if m == "HK" else m)
        # HK 用腾讯回退补齐 akshare 缺失
        for c in cs:
            if m == "HK" and (hists.get(c) is None or hists.get(c).empty):
                tx = _fetch_tx_hk_stock(c, days=years * 365)
                if tx is not None and len(tx) >= min_hist:
                    hists[c] = tx
        # US 用 FMP 补齐 akshare 缺失
        for c in cs:
            if m == "US" and (hists.get(c) is None or hists.get(c).empty):
                f = _fetch_fmp_us_stock(c, days=years * 365)
                if f is not None and len(f) >= min_hist:
                    hists[c] = f

        for c in cs:
            df = hists.get(c)
            if df is None or df.empty or len(df) < min_hist:
                continue
            df.attrs["code"] = c
            fin = fins.get(c, {})
            sig = build_signal_series(df, fin)
            if sig.empty:
                continue

            # 简化评分: 与生产 evaluate_buy 权重近似
            # 估值(35): 分位越低越便宜得分越高
            val_score = np.where(
                sig["pctl"] <= 30, 35,
                np.where(sig["pctl"] <= 50, 20,
                np.where(sig["pctl"] <= 70, 10, 0)))
            # 趋势(30): 价格在MA20上+15, MA20>MA60+15
            trend_score = (sig["price_above_ma20"].astype(int) * 15
                           + sig["ma20_above_ma60"].astype(int) * 15)
            # kelly(15): kelly>0 且方向 buy 得15
            kelly_score = np.where(sig["kelly"] > 0, 15, 5)
            # 股息率(15): 用 kelly 内股息贡献近似, 高股息>2%加分
            dy_score = np.where(sig["kelly"] > 5, 15, np.where(sig["kelly"] > 0, 10, 0))

            score = (val_score + trend_score + kelly_score + dy_score).astype(float)
            score = pd.Series(score, index=sig.index).clip(0, 100)
            # 卖出评分: 趋势走弱 + 高估值 + kelly 负
            sell_val = np.where(
                sig["pctl"] >= 80, 30,
                np.where(sig["pctl"] >= 60, 15, 0))
            sell_trend = ((~sig["price_above_ma20"]).astype(int) * 15
                          + (~sig["ma20_above_ma60"]).astype(int) * 15)
            sell_kelly = np.where(sig["kelly"] < 0, 20, 5)
            sell_score = pd.Series(sell_val + sell_trend + sell_kelly,
                                   index=sig.index).clip(0, 100)

            sig["buy_score"] = score
            sig["sell_score"] = sell_score
            sig["signal"] = [classify(b, s) for b, s in zip(score, sell_score)]

            # 信号事件簇: 连续相同信号取第一天为一次事件, 用于降低重叠窗口虚增的样本量
            sig_arr = sig["signal"].values
            events = []
            start = None
            prev = None
            for idx in range(len(sig_arr)):
                v = sig_arr[idx]
                if v != prev:
                    if start is not None:
                        events.append((start, prev))
                    start = idx
                    prev = v
            if start is not None:
                events.append((start, prev))

            per_stock = {"buys": 0, "sells": 0, "holds": 0, "horizon_stats": {}}
            per_stock_event = {"buys": 0, "sells": 0, "holds": 0, "horizon_stats": {}}
            dates = sig.index

            for i in range(len(dates)):
                date = dates[i]
                sig_type = sig["signal"].iloc[i]
                per_stock[f"{sig_type}s"] = per_stock.get(f"{sig_type}s", 0) + 1
                cost = TRADE_COST.get(m, 0.0) if sig_type == "buy" else 0.0
                regime = regime_of(date, bench)

                for h in HORIZONS:
                    st = _forward_stats(sig, i, h, bench)
                    if st is None:
                        continue
                    rec = _make_rec(st, cost, is_sell=(sig_type == "sell"))
                    _merge(results["by_signal"], sig_type, h, rec)
                    _merge(results["by_horizon"], f"{h}d", sig_type, rec)
                    if regime:
                        _merge(results["by_regime"], f"{regime}|{sig_type}", h, rec)
                    _merge(per_stock["horizon_stats"], sig_type, h, rec)

            # 事件簇级别统计
            for (i0, etype) in events:
                cost = TRADE_COST.get(m, 0.0) if etype == "buy" else 0.0
                regime = regime_of(dates[i0], bench)
                per_stock_event[f"{etype}s"] = per_stock_event.get(f"{etype}s", 0) + 1
                for h in HORIZONS:
                    st = _forward_stats(sig, i0, h, bench)
                    if st is None:
                        continue
                    rec = _make_rec(st, cost, is_sell=(etype == "sell"))
                    _merge(results["by_event"], etype, h, rec)
                    if regime:
                        _merge(results["by_regime_event"], f"{regime}|{etype}", h, rec)
                    _merge(per_stock_event["horizon_stats"], etype, h, rec)

            if per_stock.get("buys") or per_stock.get("sells"):
                results["per_stock"][c] = {
                    "name": WATCH_LIST.get(c, {}).get("name", c),
                    "market": m,
                    "n": len(sig),
                    "buys": per_stock.get("buys", 0),
                    "sells": per_stock.get("sells", 0),
                    "event_buys": per_stock_event.get("buys", 0),
                    "event_sells": per_stock_event.get("sells", 0),
                    "horizon": _summarize(per_stock.get("horizon_stats", {})),
                    "horizon_event": _summarize(per_stock_event.get("horizon_stats", {})),
                }

    # 汇总计算
    results["by_signal"] = _finalize(results["by_signal"])
    results["by_horizon"] = _finalize(results["by_horizon"])
    results["by_regime"] = _finalize(results["by_regime"])
    results["by_event"] = _finalize(results["by_event"])
    results["by_regime_event"] = _finalize(results["by_regime_event"])
    results["concentration"] = _concentration(results["per_stock"])
    for m, s in benches.items():
        if s is not None:
            results["bench"][m] = {
                "name": _BENCH[m]["name"],
                "n": len(s),
                "start": str(s.index[0].date()),
                "end": str(s.index[-1].date()),
            }
    return results


def _merge(container, key, h, rec):
    node = container.setdefault(key, {})
    hnode = node.setdefault(f"{h}d", {
        "total": 0, "up": 0, "win": 0, "excess_win": 0,
        "net_win": 0, "net_excess_win": 0,
        "rets": [], "excess": [], "net_rets": [], "net_excess": [],
    })
    hnode["total"] += rec["total"]
    hnode["up"] += rec["up"]
    hnode["win"] += rec["win"]
    hnode["excess_win"] += rec["excess_win"]
    hnode["net_win"] += rec["net_win"]
    hnode["net_excess_win"] += rec["net_excess_win"]
    hnode["rets"].extend(rec["rets"])
    hnode["excess"].extend(rec["excess"])
    hnode["net_rets"].extend(rec["net_rets"])
    hnode["net_excess"].extend(rec["net_excess"])


def _summarize(horizon_stats):
    out = {}
    for sig, hd in horizon_stats.items():
        out[sig] = {}
        for h, node in hd.items():
            n = node["total"]
            if n:
                out[sig][h] = {
                    "n": n,
                    "win_rate": round(node["win"] / n * 100, 1),
                    "excess_win_rate": round(node["excess_win"] / n * 100, 1) if n else 0,
                    "avg_ret": round(np.mean(node["rets"]), 2),
                    "avg_excess": round(np.mean(node["excess"]), 2) if node["excess"] else None,
                    "net_win_rate": round(node["net_win"] / n * 100, 1) if n else 0,
                    "avg_net_ret": round(float(np.mean(node["net_rets"])), 2),
                    "avg_net_excess": round(float(np.mean(node["net_excess"])), 2) if node["net_excess"] else None,
                }
    return out


def _finalize(container):
    out = {}
    for sig, hd in container.items():
        out[sig] = {}
        for h, node in hd.items():
            n = node["total"]
            if n:
                out[sig][h] = {
                    "n": n,
                    "win_rate": round(node["win"] / n * 100, 1),
                    "up_pct": round(node["up"] / n * 100, 1),
                    "excess_win_rate": round(node["excess_win"] / n * 100, 1),
                    "avg_ret": round(float(np.mean(node["rets"])), 2),
                    "avg_excess": round(float(np.mean(node["excess"])), 2) if node["excess"] else None,
                    "net_win_rate": round(node["net_win"] / n * 100, 1),
                    "avg_net_ret": round(float(np.mean(node["net_rets"])), 2),
                    "avg_net_excess": round(float(np.mean(node["net_excess"])), 2) if node["net_excess"] else None,
                }
    return out


def _concentration(per_stock):
    """个股分布/集中度: 买入事件由多少只股票贡献, 前3名占比, 净超额为正的股票。"""
    rows = []
    for c, st in per_stock.items():
        h = (st.get("horizon_event", {}).get("buy", {}) or {}).get("20d", {}) or {}
        hs = (st.get("horizon_event", {}).get("sell", {}) or {}).get("20d", {}) or {}
        rows.append({
            "code": c,
            "name": st["name"],
            "market": st["market"],
            "day_buys": st.get("buys", 0),
            "day_sells": st.get("sells", 0),
            "event_buys": st.get("event_buys", 0),
            "event_sells": st.get("event_sells", 0),
            "buy20_win": h.get("win_rate"),
            "buy20_net_win": h.get("net_win_rate"),
            "buy20_avg_excess": h.get("avg_excess"),
            "buy20_avg_net_excess": h.get("avg_net_excess"),
            "sell20_win": hs.get("win_rate"),
            "sell20_avg_excess": hs.get("avg_excess"),
        })
    buy_rows = sorted([r for r in rows if r["event_buys"] > 0],
                      key=lambda r: -r["event_buys"])
    total_buys = sum(r["event_buys"] for r in buy_rows)
    top3_buy_share = (round(sum(r["event_buys"] for r in buy_rows[:3]) / total_buys * 100, 1)
                      if total_buys else None)
    pos_gross = sorted(r["code"] for r in rows
                       if r.get("buy20_avg_excess") is not None and r["buy20_avg_excess"] > 0)
    pos_net = sorted(r["code"] for r in rows
                     if r.get("buy20_avg_net_excess") is not None and r["buy20_avg_net_excess"] > 0)
    return {
        "by_stock": rows,
        "n_stocks_with_buy_events": len(buy_rows),
        "top3_buy_share_pct": top3_buy_share,
        "stocks_pos_excess_gross20": pos_gross,
        "stocks_pos_excess_net20": pos_net,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--out", default="/app/data/reports/signal_regime_backtest.json")
    ap.add_argument("--codes", default="")
    args = ap.parse_args()

    codes = [c for c in args.codes.split(",") if c] if args.codes else None
    res = run_backtest(codes, years=args.years)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(json.dumps({
        "by_signal": res["by_signal"],
        "by_regime": res["by_regime"],
        "by_event": res["by_event"],
        "by_regime_event": res["by_regime_event"],
        "cost": res["cost"],
        "sell_note": res["sell_note"],
        "concentration": res["concentration"],
        "bench": res["bench"],
    }, ensure_ascii=False, indent=1))
    print("saved:", args.out)


if __name__ == "__main__":
    main()

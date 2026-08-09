"""
Paper Trader — 模拟盘验证引擎
=================================
用真实行情数据 (东方财富 AKShare) 驱动信号快照 + 虚拟组合执行，
验证系统推送的买卖信号是否真实可信。
import logging

logger = logging.getLogger(__name__)
核心流程:
  1. 每日快照: 对所有持仓生成信号评分快照 (buy/sell/hold + score)
  2. 虚拟执行: 按 Kelly 建议仓位 + 信号强弱 模拟下单
  3. 业绩追踪: 维护虚拟组合净值曲线, 计算夏普/回撤/胜率
  4. 信号验证: 回溯验证每条信号的准确性 (买后涨了吗? 卖后跌了吗?)

数据真实性:
  所有行情数据来自东方财富 (via AKShare stock_zh_a_hist / stock_hk_hist)，
  并显式标记 data_source, is_mock 字段, 禁止使用 mock 数据。
"""

import json
import os
import time
import datetime
from collections import defaultdict

import numpy as np
import pandas as pd

from modules.data.cache import load, save, retry
from modules.kelly import run as kelly_run
from modules.signals import evaluate_buy, evaluate_sell, evaluate_growth
from modules.config import PORT_A, PORT_HK, PORT_US, PORT_REIT, WATCH_LIST, MANUAL_US_GROWTH
from modules.lishing import calc as lishing_calc

PAPER_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "paper")
SNAPSHOT_FILE = os.path.join(PAPER_DIR, "signal_snapshots.json")
PORTFOLIO_FILE = os.path.join(PAPER_DIR, "paper_portfolio.json")
VALIDATION_FILE = os.path.join(PAPER_DIR, "signal_validation.json")
NAV_FILE = os.path.join(PAPER_DIR, "paper_nav.json")

VIRTUAL_CAPITAL = 1_000_000.0
MAX_POSITION_PCT = 30.0
SIGNAL_BUY_THRESHOLD = 65
SIGNAL_SELL_THRESHOLD = 55
SIGNAL_HOLD_THRESHOLD = 45

# A 股成交假设：T+1 开盘成交、涨停/跌停/停牌限制、费用
FEE_RATE = 0.001  # 单边费用（佣金+过户费等，可配置）
STAMP_TAX_RATE = 0.0005  # 卖出印花税
MIN_LOT = 100
A_LIMIT_DEFAULT = 0.10
A_LIMIT_ST = 0.05
A_LIMIT_CHINEXT = 0.20
A_LIMIT_BSE_REIT = 0.30


def _ensure_dir():
    os.makedirs(PAPER_DIR, exist_ok=True)


def _load_json(path):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_json(path, data):
    _ensure_dir()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _market_of(code):
    code_str = str(code)
    if code_str.isdigit():
        return "HK" if len(code_str) == 5 else "A"
    return "US"

_BENCH_CACHE = {"market": None, "date": None, "series": None}

def _bench_close(series, date_str):
    """取基准在指定日期的收盘点位，缺失时取最近一个不晚于该日的点。"""
    try:
        v = series.asof(pd.Timestamp(date_str))
    except Exception:
        return None
    if v is None or pd.isna(v) or v <= 0:
        return None
    return float(v)

def _fetch_benchmark_df(market="A"):
    """A 股基准：优先中证红利 000922，失败退沪深300 000300；当日缓存。"""
    today = datetime.date.today().isoformat()
    if _BENCH_CACHE.get("market") == market and _BENCH_CACHE.get("date") == today:
        return _BENCH_CACHE["series"]
    series = None
    if market == "A":
        import akshare as ak
        for symbol in ("sh000922", "sh000300"):
            try:
                df = ak.stock_zh_index_daily(symbol=symbol)
                if df is None or df.empty:
                    continue
                df["date"] = pd.to_datetime(df["date"])
                series = df.set_index("date")["close"].astype(float).sort_index()
                break
            except Exception:
                continue
    _BENCH_CACHE.update({"market": market, "date": today, "series": series})
    return series


def _fetch_real_price(code, mkt="A"):
    """从东方财富 (AKShare) 获取真实行情, 不使用缓存兜底以外的 mock 数据。"""
    from modules.data.cache import retry
    try:
        if mkt == "A":
            import akshare as ak
            df = retry(lambda: ak.stock_zh_a_spot_em())
            if df is not None and not df.empty:
                col_code = df.columns[0]
                col_price = df.columns[2]
                row = df[df[col_code].astype(str) == str(code)]
                if not row.empty:
                    price = float(row[col_price].iloc[0])
                    if price > 0:
                        return price, "eastmoney"
        elif mkt == "HK":
            import akshare as ak
            df = retry(lambda: ak.stock_hk_spot_em())
            if df is not None and not df.empty:
                col_code = df.columns[1]
                col_price = df.columns[3]
                row = df[df[col_code].astype(str) == str(code)]
                if not row.empty:
                    price = float(row[col_price].iloc[0])
                    if price > 0:
                        return price, "eastmoney_hk"
        elif mkt == "US":
            from modules.data.market_us import fetch_us_spot
            spot = fetch_us_spot([code])
            if spot is not None and not spot.empty:
                row = spot[spot["c"].astype(str) == str(code)]
                if not row.empty:
                    price = float(row["price"].iloc[0])
                    if price > 0:
                        return price, "eastmoney_us"
    except Exception as _e:
        logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    return None, "unavailable"


def _fetch_hist_real(code, years=1, mkt="A"):
    """从东方财富获取历史K线 (后复权), 用于信号验证。"""
    from modules.data.market import _fetch_hist_em_or_tx
    end = datetime.date.today()
    start = end - datetime.timedelta(days=years * 365)
    try:
        if mkt == "A":
            df = _fetch_hist_em_or_tx(
                code, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
            )
        elif mkt == "HK":
            import akshare as ak
            df = retry(lambda: ak.stock_hk_hist(
                symbol=code, period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"), adjust="qfq"
            ))
        elif mkt == "US":
            from modules.data.market_us import fetch_us_hist
            df = fetch_us_hist(
                code, start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d")
            )
        else:
            return None

        if df is not None and not df.empty:
            if "date" not in df.columns and "日期" in df.columns:
                df = df.rename(columns={"日期": "date", "收盘": "close"})
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date").sort_index()
            elif not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)
            if "close" in df.columns:
                return df["close"]
            close_col = [c for c in df.columns if "close" in c.lower() or "收盘" in str(c)]
            if close_col:
                return df[close_col[0]]
    except Exception as _e:
        logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    return None





def _us_signal_label(score):
    if score >= 80:
        return "strong_buy", "🟢 强烈买入"
    if score >= 70:
        return "buy", "🟢 可买入"
    if score >= 50:
        return "cautious", "🟡 观望"
    return "avoid", "⚪ 回避"


def _compute_us_signal(code, price, vol_info, pctls, us_extra):
    """美股走成长评分体系，不再用 A 股李氏评分占位。"""
    from modules.config import MANUAL_DIV_YIELDS
    spot = us_extra.get("spot") or {}
    div_info = us_extra.get("div") or {}
    pe_val = spot.get("pe")
    pb_val = spot.get("pb")
    div_ps = div_info.get("ttm") or div_info.get("lfy")
    if not div_ps:
        div_ps = (MANUAL_DIV_YIELDS.get(code, {}) or {}).get("ttm")
    div_yield = round(div_ps / price * 100, 2) if div_ps and price and price > 0 else None

    pctls = pctls or {}
    mg = MANUAL_US_GROWTH.get(code, {}) or {}
    pe_pctl = pctls.get("pe_pctl", mg.get("pe_pctl"))
    pb_pctl = pctls.get("pb_pctl", mg.get("pb_pctl"))
    price_above_ma20 = mg.get("price_above_ma20")
    ma20_above_ma60 = mg.get("ma20_above_ma60")
    price_above_ma60 = mg.get("price_above_ma60")
    ma60_above_ma120 = mg.get("ma60_above_ma120")

    vol = vol_info if vol_info else 0.2
    kelly = kelly_run(price=price, pb=pb_val, pe=pe_val, roe=None, eps=None,
                      debt=None, div=div_ps or 0, vol=vol)
    kf = kelly["kelly_vol_adj"]
    psig = kelly["p_signal"]

    growth = evaluate_growth(
        pe_pctl=pe_pctl, pb_pctl=pb_pctl,
        price_above_ma20=price_above_ma20,
        ma20_above_ma60=ma20_above_ma60,
        price_above_ma60=price_above_ma60,
        ma60_above_ma120=ma60_above_ma120,
        kelly_final=kf,
        vol_adj_factor=kelly["vol_adj_factor"],
        p_signal=psig,
    )
    score = growth["score"]
    buy_signal, buy_label = _us_signal_label(score)

    sell_eval = evaluate_sell(
        current_score=score,
        buy_score=None,
        high_250d=None,
        div_yield=div_yield,
        kelly_final=kf,
        p_signal=psig,
        price_above_ma20=price_above_ma20,
        ma20_above_ma60=ma20_above_ma60,
    )

    return {
        "code": code,
        "price": price,
        "buy_signal": buy_signal,
        "buy_score": score,
        "buy_label": buy_label,
        "sell_signal": sell_eval["signal"],
        "sell_score": sell_eval["score"],
        "sell_label": sell_eval["label"],
        "kelly_final": kf,
        "p_signal": psig,
        "div_yield": div_yield,
        "pe": pe_val,
        "pb": pb_val,
        "roe": None,
        "data_source": "eastmoney_us_via_akshare",
        "is_mock": False,
    }


def _compute_signal_for_stock(code, fin_info, price, vol_info, pctls=None, mkt="A", us_extra=None):
    """对单只股票计算买卖信号 (复用现有信号引擎)。"""
    from modules.config import MANUAL_DIV_YIELDS

    if mkt == "US":
        return _compute_us_signal(code, price, vol_info, pctls or {}, us_extra or {})

    pe = fin_info.get("eps") and price and price > 0 and fin_info["eps"] > 0
    pe_val = round(price / fin_info["eps"], 2) if pe else None
    pb_val = fin_info.get("bps") and price and price > 0 and fin_info["bps"] > 0
    pb_val = round(price / fin_info["bps"], 2) if pb_val else None
    roe = fin_info.get("roe")
    debt = fin_info.get("debt")

    manual_div = MANUAL_DIV_YIELDS.get(code, {}) or {}
    div_yield = manual_div.get("ttm") or manual_div.get("lfy")
    div_ps = round(div_yield / 100 * price, 4) if div_yield and price and price > 0 else None

    vol = vol_info if vol_info else 0.2

    pctls = pctls or {}
    pe_pctl = pctls.get("pe_pctl", 50)
    pb_pctl = pctls.get("pb_pctl", 50)

    kelly = kelly_run(
        price=price, pb=pb_val, pe=pe_val, roe=roe,
        eps=fin_info.get("eps"), debt=debt,
        div=div_ps if div_ps else 0, vol=vol
    )
    kf = kelly["kelly_vol_adj"]
    psig = kelly["p_signal"]

    from modules.data.market import fetch_sma
    ma20 = fetch_sma(code, 20, mkt=mkt)
    ma60 = fetch_sma(code, 60, mkt=mkt)
    price_above_ma20 = price > ma20 if (ma20 and price) else None
    ma20_above_ma60 = ma20 > ma60 if (ma20 and ma60) else None

    ls_row = {
        "code": code,
        "name": WATCH_LIST.get(code, {}).get("name", code),
        "pe": pe_val,
        "pb": pb_val,
        "roe": roe,
        "debt": debt,
        "dy": div_yield or 0,
        "div_years": fin_info.get("div_years", 0) or 0,
        "ocf_ratio": fin_info.get("ocf_ratio"),
        "mcap": None,
        "audit": fin_info.get("audit", True),
    }
    total_shares = fin_info.get("total_shares")
    if total_shares and price:
        ls_row["mcap"] = round(total_shares * price / 1e8, 2)
    ls = lishing_calc(ls_row, market=mkt)
    ls_score = ls["total"]

    buy_eval = evaluate_buy(
        lishing_score=ls_score,
        pe_pctl=pe_pctl, pb_pctl=pb_pctl,
        div_yield=div_yield,
        price_above_ma20=price_above_ma20,
        ma20_above_ma60=ma20_above_ma60,
        kelly_final=kf,
        p_signal=psig,
        vol_adj_factor=kelly["vol_adj_factor"],
    )

    sell_eval = evaluate_sell(
        current_score=ls_score,
        buy_score=None,
        high_250d=None,
        div_yield=div_yield,
        kelly_final=kf,
        p_signal=psig,
        price_above_ma20=price_above_ma20,
        ma20_above_ma60=ma20_above_ma60,
    )

    return {
        "code": code,
        "price": price,
        "buy_signal": buy_eval["signal"],
        "buy_score": buy_eval["score"],
        "buy_label": buy_eval["label"],
        "sell_signal": sell_eval["signal"],
        "sell_score": sell_eval["score"],
        "sell_label": sell_eval["label"],
        "kelly_final": kf,
        "p_signal": psig,
        "div_yield": div_yield,
        "pe": pe_val,
        "pb": pb_val,
        "roe": roe,
        "lishing_score": ls_score,
        "data_source": "eastmoney_via_akshare",
        "is_mock": False,
    }


def capture_daily_snapshot():
    """
    每日信号快照: 对所有持仓计算买入/卖出信号评分。
    使用东方财富真实数据, 不使用 mock 数据。
    """
    today = datetime.date.today().isoformat()
    snapshots = _load_json(SNAPSHOT_FILE)

    all_codes = list(PORT_A.keys()) + list(PORT_HK.keys()) + list(PORT_US.keys())
    results = []

    from modules.data.market import fetch_vols, fetch_price_percentiles
    from modules.data.market_us import fetch_us_spot
    from modules.data.fundamental import fetch_fins, fetch_fins_hk, fetch_divs_us

    logger.info(f"[PaperTrader] 拉取 {len(all_codes)} 只标的真实数据 (东方财富)...")

    ca = list(PORT_A.keys())
    chk = list(PORT_HK.keys())
    cus = list(PORT_US.keys())

    fins_a = fetch_fins(ca) if ca else {}
    fins_hk = fetch_fins_hk(chk) if chk else {}
    fins_us = {}

    vols_a = fetch_vols(ca, "A") if ca else {}
    vols_hk = fetch_vols(chk, "H") if chk else {}
    vols_us = fetch_vols(cus, "US") if cus else {}
    spot_us = fetch_us_spot(cus) if cus else None
    divs_us = fetch_divs_us(cus) if cus else {}
    spot_us_map = {}
    if spot_us is not None and not spot_us.empty:
        for _, r in spot_us.iterrows():
            spot_us_map[str(r.get("c", ""))] = r.to_dict()

    all_codes_with_mkt = []
    for c in ca:
        all_codes_with_mkt.append((c, "A", fins_a.get(c, {}), vols_a.get(c, 0.2)))
    for c in chk:
        all_codes_with_mkt.append((c, "HK", fins_hk.get(c, {}), vols_hk.get(c, 0.2)))
    for c in cus:
        all_codes_with_mkt.append((c, "US", {}, vols_us.get(c, 0.2)))

    codes_a = [c for c, m, _, _ in all_codes_with_mkt if m == "A"]
    codes_hk = [c for c, m, _, _ in all_codes_with_mkt if m == "HK"]
    pctls_a = fetch_price_percentiles(codes_a, 5, "A") if codes_a else {}
    pctls_hk = fetch_price_percentiles(codes_hk, 5, "HK") if codes_hk else {}
    codes_us = [c for c, m, _, _ in all_codes_with_mkt if m == "US"]
    pctls_us = fetch_price_percentiles(codes_us, 5, "US") if codes_us else {}

    for code, mkt, fin_info, vol in all_codes_with_mkt:
        price, src = _fetch_real_price(code, mkt=mkt)
        if price is None:
            logger.info(f"[PaperTrader] ⚠️ {code} 无法获取真实行情 (数据源: {src}), 跳过")
            results.append({
                "code": code,
                "date": today,
                "price": None,
                "error": f"无法获取真实行情 (数据源: {src})",
                "is_mock": False,
            })
            continue

        if mkt == "A":
            pctls = pctls_a.get(code, {})
        elif mkt == "HK":
            pctls = pctls_hk.get(code, {})
        else:
            pctls = pctls_us.get(code, {})
        us_extra = {"spot": spot_us_map.get(code, {}), "div": divs_us.get(code, {})}
        snap = _compute_signal_for_stock(code, fin_info or {}, price, vol, pctls, mkt=mkt, us_extra=us_extra)
        snap["date"] = today
        snap["data_source"] = src
        snap["is_mock"] = False
        results.append(snap)

    snapshots[today] = results
    _save_json(SNAPSHOT_FILE, snapshots)
    logger.info(f"[PaperTrader] ✅ 快照已保存: {today}, 共 {len(results)} 只标的")

    return results






from modules.paper_executor import _get_paper_portfolio, _fee_rate, _stamp_tax_rate, _a_limit_ratio, _a_limit_prices, _fetch_a_bar_for_date, _target_buy_shares, _create_pending_orders, _execute_pending_orders, _fetch_execution_bars
from modules.paper_executor import _get_paper_portfolio, _fee_rate, _stamp_tax_rate, _a_limit_ratio, _a_limit_prices, _fetch_a_bar_for_date, _target_buy_shares, _create_pending_orders, _execute_pending_orders, _fetch_execution_bars


from modules.paper_executor import _get_paper_portfolio, _fee_rate, _stamp_tax_rate, _a_limit_ratio, _a_limit_prices, _fetch_a_bar_for_date, _target_buy_shares, _create_pending_orders, _execute_pending_orders, _fetch_execution_bars


def update_paper_portfolio(snapshots=None, execution_date=None):
    """更新虚拟组合: 执行昨日挂单(T+1开盘) → 今日快照生成新挂单 → 净值更新。"""
    pf = _get_paper_portfolio()

    if snapshots is None:
        snapshots = capture_daily_snapshot()
    today = execution_date or datetime.date.today()
    if isinstance(today, str):
        today = datetime.date.fromisoformat(today)

    bars = _fetch_execution_bars(pf.get("pending_orders", []), today)
    pf = _execute_pending_orders(pf, bars, today)
    pf = _create_pending_orders(snapshots, pf, today.isoformat())

    total_value = pf["cash"]
    positions_value = 0
    for code, pos in pf["positions"].items():
        if pos.get("shares", 0) > 0:
            price = None
            for snap in snapshots:
                if snap.get("code") == code:
                    price = snap.get("price")
                    break
            if price and price > 0:
                pos_val = pos["shares"] * price
                positions_value += pos_val

    pf["total_value"] = pf["cash"] + positions_value
    pf["positions_value"] = positions_value
    pf["last_updated"] = datetime.datetime.now().isoformat()

    _save_json(PORTFOLIO_FILE, pf)

    _update_nav(pf)

    return pf


def _update_nav(pf):
    """更新净值曲线。"""
    nav_data = _load_json(NAV_FILE)
    today = datetime.date.today().isoformat()
    total = pf.get("total_value", VIRTUAL_CAPITAL)
    nav = total / VIRTUAL_CAPITAL

    if "dates" not in nav_data:
        nav_data["dates"] = []
        nav_data["navs"] = []
        nav_data["values"] = []

    if nav_data["dates"] and nav_data["dates"][-1] == today:
        nav_data["navs"][-1] = round(nav, 4)
        nav_data["values"][-1] = round(total, 2)
    else:
        nav_data["dates"].append(today)
        nav_data["navs"].append(round(nav, 4))
        nav_data["values"].append(round(total, 2))

    nav_data["capital"] = VIRTUAL_CAPITAL
    nav_data["last_updated"] = datetime.datetime.now().isoformat()

    if len(nav_data["navs"]) >= 2:
        navs = np.array(nav_data["navs"])
        peak = np.maximum.accumulate(navs)
        drawdown = (navs - peak) / peak
        nav_data["max_drawdown"] = round(float(np.min(drawdown)) * 100, 2)
        total_ret = navs[-1] - 1
        days = len(nav_data["dates"])
        if days > 1:
            years = days / 252
            nav_data["annual_return"] = round((1 + total_ret) ** (1 / years) - 1, 4) if years > 0 else 0
            rets = np.diff(navs) / navs[:-1]
            nav_data["annual_vol"] = round(float(np.std(rets) * np.sqrt(252)), 4) if len(rets) > 1 else 0
            if nav_data["annual_vol"] > 0:
                nav_data["sharpe"] = round((nav_data["annual_return"] - 0.025) / nav_data["annual_vol"], 2)
            else:
                nav_data["sharpe"] = 0

    _save_json(NAV_FILE, nav_data)


def validate_signals():
    """
    信号验证: 回溯历史信号, 检查其准确性。

    对每条历史信号快照, 检查后续 N 天的表现:
    - 买入信号: 之后 5/10/20 天是否上涨?
    - 卖出信号: 之后 5/10/20 天是否下跌?
    - 中性信号: 之后表现如何?
    """
    snapshots = _load_json(SNAPSHOT_FILE)
    if not snapshots:
        return {"error": "无信号快照数据, 请先运行每日快照"}

    validation = _load_json(VALIDATION_FILE)
    results = {
        "by_signal": defaultdict(lambda: {"total": 0, "up": 0, "down": 0, "flat": 0}),
        "by_horizon": {},
        "details": [],
    }

    horizons = [5, 10, 20]

    sorted_dates = sorted(snapshots.keys())

    # A 股信号用中证红利/沪深300做相对基准，避免只看绝对涨跌
    bench = _fetch_benchmark_df("A")

    for i, date in enumerate(sorted_dates):
        snaps = snapshots[date]
        future_start_idx = i + 1
        if future_start_idx >= len(sorted_dates):
            break

        for snap in snaps:
            code = snap.get("code")
            buy_score = snap.get("buy_score", 0)
            sell_score = snap.get("sell_score", 100)
            price = snap.get("price")
            if not code or price is None or price <= 0:
                continue

            mkt = _market_of(code)

            for horizon in horizons:
                if future_start_idx + horizon > len(sorted_dates):
                    continue

                future_prices = []
                for j in range(future_start_idx, min(future_start_idx + horizon, len(sorted_dates))):
                    future_snaps = snapshots[sorted_dates[j]]
                    for fs in future_snaps:
                        if fs.get("code") == code and fs.get("price") is not None:
                            future_prices.append((sorted_dates[j], fs["price"]))
                            break

                if len(future_prices) < 2:
                    continue

                final_price = future_prices[-1][1]
                ret = (final_price - price) / price * 100

                bench_ret = None
                if bench is not None:
                    start_bench = _bench_close(bench, date)
                    end_bench = _bench_close(bench, future_prices[-1][0])
                    if start_bench and end_bench:
                        bench_ret = (end_bench - start_bench) / start_bench * 100
                excess_ret = ret - bench_ret if bench_ret is not None else None

                if buy_score >= SIGNAL_BUY_THRESHOLD:
                    sig_type = "buy"
                elif sell_score < SIGNAL_SELL_THRESHOLD:
                    sig_type = "sell"
                else:
                    sig_type = "hold"

                results["by_signal"][sig_type]["total"] += 1
                if ret > 1:
                    results["by_signal"][sig_type]["up"] += 1
                elif ret < -1:
                    results["by_signal"][sig_type]["down"] += 1
                else:
                    results["by_signal"][sig_type]["flat"] += 1

                h_key = f"{horizon}d"
                if h_key not in results["by_horizon"]:
                    results["by_horizon"][h_key] = defaultdict(lambda: {"total": 0, "correct": 0, "avg_ret": [], "avg_excess_ret": []})

                results["by_horizon"][h_key][sig_type]["total"] += 1
                if sig_type == "buy" and ret > 0:
                    results["by_horizon"][h_key][sig_type]["correct"] += 1
                elif sig_type == "sell" and ret < 0:
                    results["by_horizon"][h_key][sig_type]["correct"] += 1
                results["by_horizon"][h_key][sig_type]["avg_ret"].append(ret)
                if excess_ret is not None:
                    results["by_horizon"][h_key][sig_type]["avg_excess_ret"].append(excess_ret)

                results["details"].append({
                    "signal_date": date,
                    "code": code,
                    "signal_type": sig_type,
                    "buy_score": buy_score,
                    "sell_score": sell_score,
                    "entry_price": price,
                    "exit_price": final_price,
                    "return_pct": round(ret, 2),
                    "horizon": horizon,
                    "correct": (sig_type == "buy" and ret > 0) or (sig_type == "sell" and ret < 0),
                    "benchmark_ret_pct": round(bench_ret, 2) if bench_ret is not None else None,
                    "excess_ret_pct": round(excess_ret, 2) if excess_ret is not None else None,
                    "correct_excess": (excess_ret is not None and excess_ret > 0),
                })

    summary = {
        "total_signals": 0,
        "buy_signals": {"total": 0, "win_rate": 0, "avg_ret": 0},
        "sell_signals": {"total": 0, "win_rate": 0, "avg_ret": 0},
        "hold_signals": {"total": 0, "avg_ret": 0},
        "by_horizon": {},
    }

    for sig_type, data in results["by_signal"].items():
        summary[sig_type + "_signals"] = {
            "total": data["total"],
            "win_rate": round(data["up"] / data["total"] * 100, 1) if data["total"] > 0 else 0,
            "avg_up": 0,
            "avg_down": 0,
        }
        summary["total_signals"] += data["total"]

    for h_key, h_data in results["by_horizon"].items():
        summary["by_horizon"][h_key] = {}
        for sig_type, data in h_data.items():
            rets = data["avg_ret"]
            excess_rets = data.get("avg_excess_ret") or []
            avg_ret = sum(rets) / len(rets) if rets else 0
            avg_excess = sum(excess_rets) / len(excess_rets) if excess_rets else 0
            win_rate = data["correct"] / data["total"] * 100 if data["total"] > 0 else 0
            excess_win_rate = sum(1 for x in excess_rets if x > 0) / len(excess_rets) * 100 if excess_rets else 0
            summary["by_horizon"][h_key][sig_type] = {
                "total": data["total"],
                "win_rate": round(win_rate, 1),
                "avg_ret_pct": round(avg_ret, 2),
                "avg_excess_ret_pct": round(avg_excess, 2),
                "excess_win_rate": round(excess_win_rate, 1),
            }

    summary["validated_at"] = datetime.datetime.now().isoformat()
    summary["details"] = results["details"][-100:]

    _save_json(VALIDATION_FILE, summary)
    return summary


def get_paper_status():
    """获取模拟盘完整状态 (供 UI 展示)。"""
    pf = _get_paper_portfolio()
    nav_data = _load_json(NAV_FILE)
    validation = _load_json(VALIDATION_FILE)
    snapshots = _load_json(SNAPSHOT_FILE)

    today_snapshot = None
    if snapshots:
        latest_date = max(snapshots.keys())
        today_snapshot = snapshots[latest_date]

    positions = []
    for code, pos in pf.get("positions", {}).items():
        if pos.get("shares", 0) > 0:
            name = WATCH_LIST.get(code, {}).get("name", code)
            price = 0
            if today_snapshot:
                for s in today_snapshot:
                    if s.get("code") == code:
                        price = s.get("price", 0) or 0
                        break
            mv = pos["shares"] * price
            cost_mv = pos["shares"] * pos.get("avg_cost", price or 0)
            pnl = mv - cost_mv
            pnl_pct = pnl / cost_mv * 100 if cost_mv > 0 else 0
            positions.append({
                "code": code, "name": name,
                "shares": pos["shares"],
                "avg_cost": pos.get("avg_cost", 0),
                "price": price,
                "market_value": round(mv, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
            })

    positions.sort(key=lambda x: x["market_value"], reverse=True)

    return {
        "portfolio": pf,
        "nav": nav_data,
        "validation": validation,
        "positions": positions,
        "today_snapshot": today_snapshot,
        "data_source_note": "所有数据来自东方财富 (AKShare), is_mock=False 表示真实数据",
        "pending_orders": pf.get("pending_orders", []),
        "skipped_orders": pf.get("skipped_orders", []),
    }


def reset_paper():
    """重置模拟盘。"""
    for path in [SNAPSHOT_FILE, PORTFOLIO_FILE, VALIDATION_FILE, NAV_FILE]:
        if os.path.exists(path):
            os.remove(path)
    return {"status": "reset", "message": "模拟盘已重置, 下次快照将从零开始"}


def run_full_paper_cycle():
    """完整模拟盘周期: 快照 → 交易 → 净值 → 验证。"""
    logger.info("=" * 60)
    logger.info("[PaperTrader] 开始模拟盘验证周期...")
    logger.info("=" * 60)

    snapshots = capture_daily_snapshot()
    pf = update_paper_portfolio(snapshots)
    validation = validate_signals()

    logger.info(f"\n📊 虚拟组合: 总资产 ¥{pf.get('total_value', 0):,.0f} | 现金 ¥{pf.get('cash', 0):,.0f}")
    logger.info(f"📈 虚拟持仓数: {sum(1 for p in pf.get('positions', {}).values() if p.get('shares', 0) > 0)}")
    if validation and "by_horizon" in validation:
        logger.info(f"\n🎯 信号验证结果:")
        for h, data in validation.get("by_horizon", {}).items():
            buy_data = data.get("buy", {})
            sell_data = data.get("sell", {})
            logger.info(f"  {h}: 买入胜率 {buy_data.get('win_rate', 0):.1f}% (n={buy_data.get('total', 0)})"
                  f" | 卖出胜率 {sell_data.get('win_rate', 0):.1f}% (n={sell_data.get('total', 0)})")

    return get_paper_status()


# ── 定时任务支持 ──

SCHEDULE_CONFIG_FILE = os.path.join(PAPER_DIR, "schedule_config.json")

DEFAULT_SCHEDULE = {
    "enabled": True,
    "run_time": "15:30",
    "retry_on_failure": True,
    "max_retries": 3,
    "retry_interval_minutes": 30,
    "last_run": None,
    "last_run_status": None,
}


def _load_schedule_config():
    if os.path.exists(SCHEDULE_CONFIG_FILE):
        try:
            with open(SCHEDULE_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                return {**DEFAULT_SCHEDULE, **cfg}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_SCHEDULE)


def _save_schedule_config(cfg):
    _ensure_dir()
    with open(SCHEDULE_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def is_trading_day(date=None):
    """简单判断是否交易日 (排除周末, 不含节假日)。"""
    if date is None:
        date = datetime.date.today()
    return date.weekday() < 5


# 跨进程互斥锁: 两个容器(portfolio/alert-worker)各自跑 alert_worker,
# 为避免同一周期被并发执行两次导致重复下单, 用锁文件保证单写者。
CYCLE_LOCK_FILE = os.path.join(PAPER_DIR, "cycle.lock")
CYCLE_LOCK_MAX_AGE_SECONDS = 2 * 60 * 60


def _acquire_cycle_lock():
    """尝试获取执行锁。成功返回锁文件路径, 失败(已被持有)返回 None。"""
    _ensure_dir()
    try:
        if os.path.exists(CYCLE_LOCK_FILE):
            try:
                age = time.time() - os.path.getmtime(CYCLE_LOCK_FILE)
            except OSError:
                age = 0
            if age < CYCLE_LOCK_MAX_AGE_SECONDS:
                return None
            # 锁过期(进程崩溃残留), 回收
            os.remove(CYCLE_LOCK_FILE)
        fd = os.open(CYCLE_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return CYCLE_LOCK_FILE
    except FileExistsError:
        return None
    except FileNotFoundError:
        os.makedirs(PAPER_DIR, exist_ok=True)
        return _acquire_cycle_lock()


def _release_cycle_lock(lock_path):
    if lock_path and os.path.exists(lock_path):
        try:
            os.remove(lock_path)
        except OSError:
            pass


def check_and_run_daily(force=False):
    """
    检查并运行每日模拟盘验证。
    - 如果今日已完成且非强制, 跳过
    - 如果是交易日且未运行, 执行完整周期
    - 支持失败重试
    """
    cfg = _load_schedule_config()
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now()

    if not cfg["enabled"]:
        return {"status": "skipped", "reason": "定时任务已禁用"}

    if not is_trading_day():
        return {"status": "skipped", "reason": "非交易日"}

    if not force and cfg.get("last_run") == today:
        return {"status": "skipped", "reason": f"今日已完成 ({cfg.get('last_run_status')})"}

    run_time = cfg.get("run_time", "15:30")
    try:
        h, m = map(int, run_time.split(":"))
        scheduled = now.replace(hour=h, minute=m, second=0, microsecond=0)
    except (ValueError, AttributeError):
        scheduled = now.replace(hour=15, minute=30, second=0, microsecond=0)

    if now < scheduled and not force:
        return {
            "status": "waiting",
            "reason": f"计划执行时间 {run_time}, 当前 {now.strftime('%H:%M')}",
            "scheduled": run_time,
        }

    lock = None
    if not force:
        lock = _acquire_cycle_lock()
        if not lock:
            return {"status": "skipped", "reason": "另一进程正在运行模拟盘, 本次跳过"}
    try:
        retries = cfg.get("max_retries", 3)
        for attempt in range(1, retries + 1):
            try:
                status = run_full_paper_cycle()
                cfg["last_run"] = today
                cfg["last_run_status"] = "success"
                cfg["last_run_time"] = now.isoformat()
                _save_schedule_config(cfg)
                return {"status": "success", "attempt": attempt, "summary": status}
            except Exception as e:
                logger.info(f"[PaperTrader] ❌ 第 {attempt} 次尝试失败: {e}")
                if attempt < retries and cfg.get("retry_on_failure", True):
                    import time
                    time.sleep(cfg.get("retry_interval_minutes", 30) * 60)
                else:
                    cfg["last_run"] = today
                    cfg["last_run_status"] = f"failed: {str(e)[:100]}"
                    _save_schedule_config(cfg)
                    return {"status": "failed", "attempt": attempt, "error": str(e)}
    finally:
        if lock:
            _release_cycle_lock(lock)

    return {"status": "failed", "error": "超过最大重试次数"}


def get_schedule_status():
    """获取定时任务状态 (供 UI 展示)。"""
    cfg = _load_schedule_config()
    today = datetime.date.today().isoformat()
    return {
        "enabled": cfg["enabled"],
        "run_time": cfg["run_time"],
        "is_trading_day": is_trading_day(),
        "today_done": cfg.get("last_run") == today,
        "last_run": cfg.get("last_run"),
        "last_run_status": cfg.get("last_run_status"),
        "last_run_time": cfg.get("last_run_time"),
        "force_available": True,
    }


def set_schedule_config(enabled=None, run_time=None):
    """更新定时任务配置。"""
    cfg = _load_schedule_config()
    if enabled is not None:
        cfg["enabled"] = enabled
    if run_time is not None:
        cfg["run_time"] = run_time
    _save_schedule_config(cfg)
    return cfg



"""Streamlit 数据获取缓存包装（住在数据缝，可被测试触达）。

原内联于 app.py 顶部，下沉到数据层以提升 locality：
缓存策略与数据获取同处一层，app 视图只关心取用。
"""
import logging

logger = logging.getLogger(__name__)

import datetime

import pandas as pd
import streamlit as st

from modules.data.fundamental import (
    fetch_divs,
    fetch_divs_hk,
    fetch_divs_us,
    fetch_fins,
    fetch_fins_hk,
    fetch_fins_us,
)
from modules.data.market import fetch_spot_a, fetch_spot_hk, fetch_vols


@st.cache_data(ttl=3600)
def fetch_divs_cached(codes_tuple):
    return fetch_divs(list(codes_tuple))


@st.cache_data(ttl=3600)
def fetch_divs_hk_cached(codes_tuple):
    return fetch_divs_hk(list(codes_tuple))


@st.cache_data(ttl=3600)
def fetch_hist_em_or_tx_cached(code, start, end):
    from modules.data.market import _fetch_hist_em_or_tx
    return _fetch_hist_em_or_tx(code, start, end)


@st.cache_data(ttl=86400)
def fetch_factor_history_cached(code, market):
    """获取因子分析用历史数据 (750天)"""
    import akshare as ak

    from modules.data.cache import retry
    end = datetime.date.today().strftime("%Y%m%d")
    start = (datetime.date.today() - datetime.timedelta(days=750)).strftime("%Y%m%d")
    if market == "US":
        try:
            from modules.data.market_us import fetch_us_hist
            df = fetch_us_hist(code, days=750)
            if df is not None:
                return df
        except Exception as _e:
            logger.warning(f"[{__name__}] %s", _e, exc_info=True)
        return None
    if market == "HK":
        try:
            d = retry(lambda: ak.stock_hk_hist(symbol=code, period="daily",
                        start_date=start, end_date=end, adjust="qfq"))
            if d is not None and not d.empty:
                d = d.rename(columns={"日期": "date", "开盘": "open",
                                       "收盘": "close", "最高": "high",
                                       "最低": "low", "成交量": "volume"})
                d["date"] = pd.to_datetime(d["date"])
                return d
        except Exception as _e:
            logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    from modules.data.market import _fetch_hist_em_or_tx
    return _fetch_hist_em_or_tx(code, start, end)


@st.cache_data(ttl=300)  # 5分钟缓存行情
def fetch_spot_a_cached(codes_tuple):
    return fetch_spot_a(list(codes_tuple))


@st.cache_data(ttl=300)
def fetch_spot_hk_cached(codes_tuple):
    return fetch_spot_hk(list(codes_tuple))


@st.cache_data(ttl=3600)  # 1小时缓存财务数据
def fetch_fins_cached(codes_tuple):
    return fetch_fins(list(codes_tuple))


@st.cache_data(ttl=3600)
def fetch_fins_hk_cached(codes_tuple):
    return fetch_fins_hk(list(codes_tuple))


@st.cache_data(ttl=3600)
def fetch_fins_us_cached(codes_tuple):
    return fetch_fins_us(list(codes_tuple))


@st.cache_data(ttl=300)  # 5分钟缓存波动率
def fetch_vols_cached(codes_tuple, market="A"):
    return fetch_vols(list(codes_tuple), market)


@st.cache_data(ttl=3600)
def fetch_divs_us_cached(codes_tuple):
    return fetch_divs_us(list(codes_tuple))


@st.cache_data(ttl=3600)
def fetch_market_prices_cached(codes_tuple):
    from modules.data.fund import fetch_market_prices
    return fetch_market_prices(list(codes_tuple))


@st.cache_data(ttl=3600)
def fetch_divs_reit_cached(codes_tuple):
    from modules.data.fundamental import fetch_divs_reit
    return fetch_divs_reit(list(codes_tuple))


@st.cache_data(ttl=300)  # 5分钟缓存快照数据
def load_snapshots_cached(days):
    from modules.exporter import load_snapshots
    return load_snapshots(days)


# ── 智能缓存TTL：交易时段5分钟，非交易时段30分钟 ──
def get_cache_ttl():
    try:
        from modules.scheduler import is_trading_hours
        return 300 if is_trading_hours() else 1800
    except Exception:
        return 600
"""全市场 A 股批量快照（红利候选扫描的廉价粗筛数据源）。

复用 akshare stock_zh_a_spot_em 一次拿到全部 A 股的
代码 / 名称 / 最新价 / 市盈率 / 市净率 / 总市值 / 成交额，缓存到本地（默认 24h），
避免每次扫描都打网络。不含股息率——股息率由后续 build_portfolio_df 对幸存者逐个拉取，
符合"分级、廉价粗筛"的设计。

注：北交所 / 老三板（代码前缀 8/4/92/83/43/87）流动性与交易规则不同，粗筛直接排除。
"""
import datetime
import logging
import os
import pickle

import pandas as pd

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_CACHE = os.path.join(_DATA_DIR, ".bulk_a_spot.pkl")
_MAX_AGE_HOURS = 24

# 北交所 / 老三板 代码前缀，流动性与交易规则不同，粗筛排除
_EXCLUDE_PREFIX = ("8", "4", "92", "83", "43", "87")


def _normalize(df):
    out = pd.DataFrame()
    out["code"] = df["代码"].astype(str).str.strip()
    out["name"] = df["名称"].astype(str).str.strip()
    out["price"] = pd.to_numeric(df["最新价"], errors="coerce")
    out["pe"] = pd.to_numeric(df["市盈率-动态"], errors="coerce")
    out["pb"] = pd.to_numeric(df["市净率"], errors="coerce")
    mcap = pd.to_numeric(df["总市值"], errors="coerce")
    out["mcap"] = mcap / 1e8  # 元 -> 亿
    out["amount"] = pd.to_numeric(df["成交额"], errors="coerce")
    out = out[~out["code"].str.startswith(_EXCLUDE_PREFIX)]
    out = out[~out["name"].str.contains("ST", case=False, na=False)]
    out = out[~out["name"].str.startswith("*", na=False)]
    return out.reset_index(drop=True)


def bulk_a_spot(force=False, max_age_hours=_MAX_AGE_HOURS):
    """返回全 A 批量快照 DataFrame（标准化列：code/name/price/pe/pb/mcap/amount）。"""
    if not force and os.path.exists(_CACHE):
        try:
            with open(_CACHE, "rb") as f:
                ts, df = pickle.load(f)
            age = (datetime.datetime.now() - ts).total_seconds() / 3600
            if age <= max_age_hours:
                logger.info("[market_universe] cache hit (%dh, %d rows)", age, len(df))
                return df
        except Exception as e:
            logger.warning("[market_universe] cache load failed: %s", e)
    import akshare as ak
    logger.info("[market_universe] fetching all A via akshare...")
    raw = ak.stock_zh_a_spot_em()
    df = _normalize(raw)
    try:
        with open(_CACHE, "wb") as f:
            pickle.dump((datetime.datetime.now(), df), f)
    except Exception as e:
        logger.warning("[market_universe] cache write failed: %s", e)
    logger.info("[market_universe] fetched %d rows", len(df))
    return df

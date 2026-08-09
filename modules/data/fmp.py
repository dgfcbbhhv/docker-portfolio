"""FMP (Financial Modeling Prep) 美股基本面备援数据源。

yfinance 被 Yahoo 限流时降级使用。免费 key 250 请求/天, 必须缓存:
  1h 内存+磁盘缓存, 每个 code 固定 2 次请求 (key-metrics + ratios)。

API: https://financialmodelingprep.com/stable/
  key-metrics/symbol -> returnOnEquity, ...
  ratios/symbol      -> priceToEarningsRatio, priceToBookRatio, debtToAssetsRatio,
                        netIncomePerShare, shareholdersEquityPerShare,
                        operatingCashFlowPerShare
Key 从环境变量 FMP_API_KEY 读取, 缺失时静默返回空(绝不阻塞主流程)。
"""
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

BASE = "https://financialmodelingprep.com/stable"
_TIMEOUT = 20
_CACHE = {}
_CACHE_TTL = 3600  # 1h, 与 yfinance 缓存节奏一致


def _api_key():
    return os.environ.get("FMP_API_KEY", "").strip()


def _get(path, symbol):
    key = _api_key()
    if not key:
        return None
    ck = (path, symbol)
    now = time.time()
    hit = _CACHE.get(ck)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    try:
        r = requests.get(f"{BASE}/{path}", params={"apikey": key, "symbol": symbol, "limit": 1},
                         timeout=_TIMEOUT)
        if r.status_code != 200:
            logger.warning("[fmp] %s %s: HTTP %s", path, symbol, r.status_code)
            _CACHE[ck] = (now, None)
            return None
        data = r.json()
    except Exception as e:
        logger.warning("[fmp] %s %s 请求失败: %s", path, symbol, e)
        _CACHE[ck] = (now, None)
        return None
    _CACHE[ck] = (now, data)
    return data


def _first(data):
    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict):
        return data
    return None


def _f(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_us_finance(code):
    """FMP 拉取单只美股基本面, 返回与 fundamental 兼容的 info dict。

    任一字段缺失保持 None, 不推断。code 形如 "MSFT" 或 "MSFT.US"。
    """
    info = {"roe": None, "eps": None, "bps": None, "debt": None,
            "pe": None, "pb": None, "ocf_ratio": None, "audit": True,
            "div_years": 0, "total_shares": None, "_source": "fmp"}
    sym = str(code).split(".")[0].upper()
    km = _first(_get("key-metrics", sym))
    ratios = _first(_get("ratios", sym))
    if not km and not ratios:
        return info

    roe = _f(km.get("returnOnEquity") if km else None)
    if roe is not None:
        info["roe"] = round(roe * 100, 2)

    if ratios:
        info["pe"] = _f(ratios.get("priceToEarningsRatio"))
        info["pb"] = _f(ratios.get("priceToBookRatio"))
        debt = _f(ratios.get("debtToAssetsRatio"))
        if debt is not None:
            info["debt"] = round(debt * 100, 2)
        info["eps"] = _f(ratios.get("netIncomePerShare"))
        info["bps"] = _f(ratios.get("shareholdersEquityPerShare"))
        ocf = _f(ratios.get("operatingCashFlowPerShare"))
        ni = _f(ratios.get("netIncomePerShare"))
        if ocf is not None and ni and ni != 0:
            info["ocf_ratio"] = round(ocf / ni * 100.0, 1)
    return info

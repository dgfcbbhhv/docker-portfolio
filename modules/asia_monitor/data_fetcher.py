# -*- coding: utf-8 -*-
"""
数据拉取模块
支持ETF历史数据、基本面数据获取，并集成缓存机制
使用akshare获取美股ETF数据
"""

import datetime
import pandas as pd
import numpy as np
import time
import random
from typing import Dict, Optional, Tuple
import logging

from .config import ASIA_MARKETS, DATA_CONFIG

logger = logging.getLogger(__name__)

# 全局Session复用
_session = None
_last_request_time = 0
_min_interval = 1.0  # 最小请求间隔(秒)


def _get_session():
    """获取或创建全局Session"""
    global _session
    if _session is None:
        import requests
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
    return _session


def _rate_limit_delay():
    """速率限制延迟"""
    global _last_request_time
    current_time = time.time()
    elapsed = current_time - _last_request_time
    if elapsed < _min_interval:
        sleep_time = _min_interval - elapsed + random.uniform(0.1, 0.3)
        time.sleep(sleep_time)
    _last_request_time = time.time()


def _fetch_with_retry(func, max_retries=3, base_delay=3.0):
    """带重试的数据获取"""
    for attempt in range(max_retries):
        try:
            _rate_limit_delay()
            return func()
        except Exception as e:
            error_msg = str(e)
            if "Too Many Requests" in error_msg or "Rate limited" in error_msg:
                delay = base_delay * (attempt + 1) + random.uniform(1, 2)
                logger.warning(f"速率限制，等待 {delay:.1f}s 后重试 ({attempt+1}/{max_retries})")
                time.sleep(delay)
            else:
                logger.error(f"获取数据失败: {e}")
                return None
    logger.error(f"重试 {max_retries} 次后仍然失败")
    return None


def _get_cache_path(code: str, data_type: str) -> str:
    """获取缓存文件路径"""
    return f"modules/asia_monitor/cache/{data_type}_{code}.pkl"


def _save_cache(df: pd.DataFrame, code: str, data_type: str) -> None:
    """保存数据到缓存"""
    import os
    cache_dir = "modules/asia_monitor/cache"
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = _get_cache_path(code, data_type)
    try:
        df.to_pickle(cache_path)
    except Exception as e:
        logger.warning(f"缓存保存失败: {e}")


def _load_cache(code: str, data_type: str, max_age_seconds: int = None) -> Optional[pd.DataFrame]:
    """从缓存加载数据"""
    import os
    import time
    
    if max_age_seconds is None:
        max_age_seconds = DATA_CONFIG["cache_ttl"]
    
    cache_path = _get_cache_path(code, data_type)
    
    if not os.path.exists(cache_path):
        return None
    
    # 检查缓存是否过期
    file_age = time.time() - os.path.getmtime(cache_path)
    if file_age > max_age_seconds:
        return None
    
    try:
        return pd.read_pickle(cache_path)
    except Exception as e:
        logger.warning(f"缓存加载失败: {e}")
        return None


def fetch_etf_history(code: str, period: str = None, use_cache: bool = True) -> Optional[pd.DataFrame]:
    """
    获取ETF历史数据（使用akshare）
    
    Args:
        code: 市场代码 (如 "JP", "KR")
        period: 数据周期，默认使用配置中的history_period
        use_cache: 是否使用缓存
    
    Returns:
        DataFrame with columns: [Open, High, Low, Close, Volume]
    """
    if period is None:
        period = DATA_CONFIG["history_period"]
    
    market_info = ASIA_MARKETS.get(code)
    if not market_info:
        logger.error(f"未知市场代码: {code}")
        return None
    
    etf_code = market_info["etf_code"]
    
    # 尝试从缓存加载
    if use_cache:
        cached = _load_cache(code, "etf_history")
        if cached is not None:
            return cached
    
    def _fetch():
        import akshare as ak
        # 使用akshare获取美股ETF数据
        df = ak.stock_us_daily(symbol=etf_code, adjust='qfq')
        if df is None or df.empty:
            raise Exception(f"ETF {etf_code} 无数据")
        return df
    
    hist = _fetch_with_retry(_fetch)
    if hist is None:
        return None
    
    # 标准化列名和格式
    hist = hist.rename(columns={
        "date": "Date",
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
        "volume": "Volume",
    })
    
    # 确保日期列是datetime类型
    if "Date" in hist.columns:
        hist["Date"] = pd.to_datetime(hist["Date"])
        hist = hist.set_index("Date")
    
    # 保存缓存
    if use_cache:
        _save_cache(hist, code, "etf_history")
    
    return hist


def fetch_index_history(code: str, period: str = None, use_cache: bool = True) -> Optional[pd.DataFrame]:
    """
    获取指数历史数据
    
    Args:
        code: 市场代码
        period: 数据周期
        use_cache: 是否使用缓存
    
    Returns:
        DataFrame with columns: [Open, High, Low, Close, Volume]
    """
    if period is None:
        period = DATA_CONFIG["history_period"]
    
    market_info = ASIA_MARKETS.get(code)
    if not market_info:
        logger.error(f"未知市场代码: {code}")
        return None
    
    index_code = market_info["index_code"]
    
    # 尝试从缓存加载
    if use_cache:
        cached = _load_cache(code, "index_history")
        if cached is not None:
            return cached
    
    try:
        ticker = yf.Ticker(index_code)
        hist = ticker.history(period=period)
        
        if hist.empty:
            logger.warning(f"指数 {index_code} 无数据")
            return None
        
        # 保存缓存
        if use_cache:
            _save_cache(hist, code, "index_history")
        
        return hist
        
    except Exception as e:
        logger.error(f"获取指数 {index_code} 数据失败: {e}")
        return None


def fetch_etf_info(code: str, use_cache: bool = True) -> Optional[Dict]:
    """
    获取ETF基本面数据（使用默认估值数据）
    
    Args:
        code: 市场代码
        use_cache: 是否使用缓存
    
    Returns:
        Dict with keys: [pe_ratio, dividend_yield, ...]
    """
    market_info = ASIA_MARKETS.get(code)
    if not market_info:
        logger.error(f"未知市场代码: {code}")
        return None
    
    etf_code = market_info["etf_code"]
    
    # 尝试从缓存加载
    if use_cache:
        cached = _load_cache(code, "etf_info")
        if cached is not None:
            return cached.to_dict() if isinstance(cached, pd.Series) else cached
    
    # 使用合理的默认估值数据
    # 这些是基于市场平均估值的近似值
    default估值 = {
        "EWJ": {"pe_ratio": 15.5, "dividend_yield": 0.022},   # 日本
        "EWY": {"pe_ratio": 12.8, "dividend_yield": 0.018},   # 韩国
        "EWS": {"pe_ratio": 13.2, "dividend_yield": 0.035},   # 新加坡
        "INDA": {"pe_ratio": 22.5, "dividend_yield": 0.012},  # 印度
        "EWT": {"pe_ratio": 14.8, "dividend_yield": 0.028},   # 台湾
        "EWH": {"pe_ratio": 10.5, "dividend_yield": 0.032},   # 香港
        "MCHI": {"pe_ratio": 12.2, "dividend_yield": 0.018},  # 中国
        "EIDO": {"pe_ratio": 14.2, "dividend_yield": 0.025},  # 印尼
        "EWM": {"pe_ratio": 13.5, "dividend_yield": 0.030},   # 马来西亚
        "THD": {"pe_ratio": 15.8, "dividend_yield": 0.022},   # 泰国
        "EPHE": {"pe_ratio": 12.5, "dividend_yield": 0.028},  # 菲律宾
        "VNM": {"pe_ratio": 13.8, "dividend_yield": 0.015},   # 越南
    }
    
    defaults = default估值.get(etf_code, {"pe_ratio": 15.0, "dividend_yield": 0.020})
    
    result = {
        "pe_ratio": defaults["pe_ratio"],
        "forward_pe": None,
        "dividend_yield": defaults["dividend_yield"],
        "price_to_book": None,
        "market_cap": None,
        "currency": "USD",
        "short_name": market_info["name_en"],
        "long_name": f"{market_info['name_en']} ETF",
    }
    
    # 保存缓存
    if use_cache:
        _save_cache(pd.Series(result), code, "etf_info")
    
    return result


def fetch_market_data(code: str, use_cache: bool = True) -> Tuple[Optional[pd.DataFrame], Optional[Dict]]:
    """
    获取完整的市场数据（历史数据 + 基本面）
    
    Args:
        code: 市场代码
        use_cache: 是否使用缓存
    
    Returns:
        Tuple of (history_df, info_dict)
    """
    hist = fetch_etf_history(code, use_cache=use_cache)
    info = fetch_etf_info(code, use_cache=use_cache)
    
    return hist, info


def fetch_all_markets(use_cache: bool = True) -> Dict[str, Dict]:
    """
    获取所有12个市场的数据（串行请求以避免速率限制）
    
    Args:
        use_cache: 是否使用缓存
    
    Returns:
        Dict mapping market code to {history, info}
    """
    results = {}
    
    # 串行获取数据，避免速率限制
    for code in ASIA_MARKETS.keys():
        try:
            hist, info = fetch_market_data(code, use_cache=use_cache)
            results[code] = {
                "history": hist,
                "info": info,
            }
            logger.info(f"成功获取 {code} 市场数据")
        except Exception as e:
            logger.error(f"获取市场 {code} 数据失败: {e}")
            results[code] = {
                "history": None,
                "info": None,
            }
    
    return results


def clear_cache(code: str = None) -> None:
    """
    清除缓存
    
    Args:
        code: 指定市场代码，如果为None则清除所有缓存
    """
    import os
    import glob
    
    cache_dir = "modules/asia_monitor/cache"
    if not os.path.exists(cache_dir):
        return
    
    if code:
        pattern = os.path.join(cache_dir, f"*_{code}.pkl")
    else:
        pattern = os.path.join(cache_dir, "*.pkl")
    
    for file_path in glob.glob(pattern):
        try:
            os.remove(file_path)
        except Exception as e:
            logger.warning(f"删除缓存文件失败: {e}")


def get_data_status() -> Dict[str, Dict]:
    """
    获取所有市场的数据状态
    
    Returns:
        Dict mapping market code to {has_data, last_update, record_count}
    """
    import os
    
    status = {}
    cache_dir = "modules/asia_monitor/cache"
    
    for code in ASIA_MARKETS.keys():
        cache_path = os.path.join(cache_dir, f"etf_history_{code}.pkl")
        
        if os.path.exists(cache_path):
            import time
            file_age = time.time() - os.path.getmtime(cache_path)
            
            try:
                df = pd.read_pickle(cache_path)
                record_count = len(df)
            except:
                record_count = 0
            
            status[code] = {
                "has_data": True,
                "last_update_hours": round(file_age / 3600, 1),
                "record_count": record_count,
                "is_stale": file_age > DATA_CONFIG["cache_ttl"],
            }
        else:
            status[code] = {
                "has_data": False,
                "last_update_hours": None,
                "record_count": 0,
                "is_stale": True,
            }
    
    return status

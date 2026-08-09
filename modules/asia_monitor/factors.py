# -*- coding: utf-8 -*-
"""
动量/估值/背离因子计算模块
实现动低背离(Momentum-Valuation Divergence)因子计算
"""

import pandas as pd
import numpy as np
from typing import Dict, Optional, Tuple
import logging

from .config import MOMENTUM_WEIGHTS, VALUATION_WEIGHTS

logger = logging.getLogger(__name__)


def percentile_rank(value: float, series: pd.Series) -> float:
    """
    计算当前值在历史序列中的百分位排名
    
    Args:
        value: 当前值
        series: 历史序列
    
    Returns:
        百分位排名 [0, 100]
    """
    if series.empty or series.isna().all():
        return 50.0
    
    valid_series = series.dropna()
    if valid_series.empty:
        return 50.0
    
    # 计算排名百分位
    rank = (valid_series < value).sum() / len(valid_series) * 100
    return min(max(rank, 0), 100)


def calc_momentum_percentile(hist: pd.DataFrame, lookback: int = 252) -> Dict:
    """
    计算动量分位 [0, 100]
    
    动量分位 = 0.4 × rank(20日收益率) + 0.3 × rank(60日收益率) + 0.3 × rank(距高点位置)
    
    Args:
        hist: 历史数据DataFrame，包含Close和High列
        lookback: 回溯天数
    
    Returns:
        Dict包含: momentum_score, ret_20d, ret_60d, pct_from_high, details
    """
    if hist is None or len(hist) < lookback:
        return {
            "momentum_score": 50.0,
            "ret_20d": 0.0,
            "ret_60d": 0.0,
            "pct_from_high": 0.0,
            "details": {"rank_ret20": 50.0, "rank_ret60": 50.0, "rank_high": 50.0},
            "insufficient_data": True,
        }
    
    try:
        close = hist["Close"]
        high = hist["High"]
        
        # 计算20日收益率
        ret_20d_series = close.pct_change(20) * 100
        ret_20d = ret_20d_series.iloc[-1] if not ret_20d_series.empty else 0.0
        
        # 计算60日收益率
        ret_60d_series = close.pct_change(60) * 100
        ret_60d = ret_60d_series.iloc[-1] if not ret_60d_series.empty else 0.0
        
        # 计算距52周高点位置
        rolling_high = high.rolling(window=252, min_periods=1).max()
        pct_from_high = (close.iloc[-1] / rolling_high.iloc[-1] - 1) * 100
        
        # 计算各子因子的历史排名
        rank_ret20 = percentile_rank(ret_20d, ret_20d_series)
        rank_ret60 = percentile_rank(ret_60d, ret_60d_series)
        rank_high = percentile_rank(pct_from_high, 
                                    (close / rolling_high - 1) * 100)
        
        # 加权计算动量分位
        momentum_score = (
            MOMENTUM_WEIGHTS["ret_20d"] * rank_ret20 +
            MOMENTUM_WEIGHTS["ret_60d"] * rank_ret60 +
            MOMENTUM_WEIGHTS["pct_from_high"] * rank_high
        )
        
        return {
            "momentum_score": round(momentum_score, 2),
            "ret_20d": round(ret_20d, 2),
            "ret_60d": round(ret_60d, 2),
            "pct_from_high": round(pct_from_high, 2),
            "details": {
                "rank_ret20": round(rank_ret20, 2),
                "rank_ret60": round(rank_ret60, 2),
                "rank_high": round(rank_high, 2),
            },
            "insufficient_data": False,
        }
        
    except Exception as e:
        logger.error(f"计算动量分位失败: {e}")
        return {
            "momentum_score": 50.0,
            "ret_20d": 0.0,
            "ret_60d": 0.0,
            "pct_from_high": 0.0,
            "details": {"rank_ret20": 50.0, "rank_ret60": 50.0, "rank_high": 50.0},
            "insufficient_data": True,
        }


def calc_valuation_percentile(hist: pd.DataFrame, info: Dict) -> Dict:
    """
    计算估值分位 [0, 100]
    
    估值分位 = 0.5 × rank(PE) + 0.3 × rank(股息率) + 0.2 × rank(价格百分位)
    
    注意：PE越高代表越贵，股息率越高代表越便宜
    所以PE分位直接使用，股息率分位取反
    
    Args:
        hist: 历史数据DataFrame，包含Close列
        info: ETF基本面信息
    
    Returns:
        Dict包含: valuation_score, pe_ratio, dividend_yield, price_percentile, details
    """
    if hist is None or len(hist) < 60:
        return {
            "valuation_score": 50.0,
            "pe_ratio": None,
            "dividend_yield": None,
            "price_percentile": 50.0,
            "details": {"rank_pe": 50.0, "rank_dy": 50.0, "rank_price": 50.0},
            "insufficient_data": True,
        }
    
    try:
        close = hist["Close"]
        current_price = close.iloc[-1]
        
        # 获取基本面数据
        pe_ratio = info.get("pe_ratio") if info else None
        dividend_yield = info.get("dividend_yield") if info else None
        
        # 计算价格历史百分位
        price_percentile = percentile_rank(current_price, close)
        
        # 处理PE数据
        if pe_ratio is not None and pe_ratio > 0:
            # PE在合理范围内（0-100）
            pe_clipped = min(max(pe_ratio, 0), 100)
            pe_percentile = pe_clipped  # PE直接使用，越高越贵
        else:
            pe_percentile = 50.0
        
        # 处理股息率数据
        if dividend_yield is not None and dividend_yield > 0:
            # 股息率在合理范围内（0-0.2，即0-20%）
            dy_clipped = min(max(dividend_yield, 0), 0.2)
            # 股息率越高越便宜，所以取反
            dividend_percentile = (1 - dy_clipped / 0.2) * 100
        else:
            dividend_percentile = 50.0
        
        # 计算各子因子的排名
        rank_pe = pe_percentile
        rank_dy = dividend_percentile
        rank_price = price_percentile
        
        # 加权计算估值分位
        valuation_score = (
            VALUATION_WEIGHTS["pe"] * rank_pe +
            VALUATION_WEIGHTS["dividend_yield"] * rank_dy +
            VALUATION_WEIGHTS["price_percentile"] * rank_price
        )
        
        return {
            "valuation_score": round(valuation_score, 2),
            "pe_ratio": pe_ratio,
            "dividend_yield": dividend_yield,
            "price_percentile": round(price_percentile, 2),
            "details": {
                "rank_pe": round(rank_pe, 2),
                "rank_dy": round(rank_dy, 2),
                "rank_price": round(rank_price, 2),
            },
            "insufficient_data": False,
        }
        
    except Exception as e:
        logger.error(f"计算估值分位失败: {e}")
        return {
            "valuation_score": 50.0,
            "pe_ratio": None,
            "dividend_yield": None,
            "price_percentile": 50.0,
            "details": {"rank_pe": 50.0, "rank_dy": 50.0, "rank_price": 50.0},
            "insufficient_data": True,
        }


def calc_divergence(momentum_score: float, valuation_score: float) -> Dict:
    """
    计算背离度
    
    背离度 = 动量分位 - (100 - 估值分位)
    多头背离为正（动量强但估值低）
    空头背离为负（动量弱但估值高）
    
    Args:
        momentum_score: 动量分位 [0, 100]
        valuation_score: 估值分位 [0, 100]
    
    Returns:
        Dict包含: divergence_score, divergence_type, description
    """
    # 背离度 = 动量分位 - 估值分位（调整后）
    # 动量分位高 + 估值分位低 = 正背离（多头机会）
    # 动量分位低 + 估值分位高 = 负背离（空头警示）
    adjusted_valuation = 100 - valuation_score
    divergence = momentum_score - adjusted_valuation
    
    # 限制在 [-100, 100] 范围内
    divergence = max(min(divergence, 100), -100)
    
    # 判断背离类型
    if divergence > 20:
        divergence_type = "bullish_divergence"
        description = "多头背离：动量强于估值，价格优势明显"
    elif divergence < -20:
        divergence_type = "bearish_divergence"
        description = "空头背离：动量弱于估值，估值偏高"
    else:
        divergence_type = "neutral"
        description = "动量与估值基本匹配"
    
    return {
        "divergence_score": round(divergence, 2),
        "divergence_type": divergence_type,
        "description": description,
    }


def calc_trend_strength(hist: pd.DataFrame) -> Dict:
    """
    计算趋势强度（均线多头排列得分）
    
    计算MA5, MA10, MA20, MA60的多头排列程度
    
    Args:
        hist: 历史数据DataFrame，包含Close列
    
    Returns:
        Dict包含: trend_score, ma_values, alignment
    """
    if hist is None or len(hist) < 60:
        return {
            "trend_score": 50.0,
            "ma_values": {},
            "alignment": "unknown",
        }
    
    try:
        close = hist["Close"]
        
        # 计算移动平均线
        ma5 = close.rolling(window=5).mean().iloc[-1]
        ma10 = close.rolling(window=10).mean().iloc[-1]
        ma20 = close.rolling(window=20).mean().iloc[-1]
        ma60 = close.rolling(window=60).mean().iloc[-1]
        current_price = close.iloc[-1]
        
        ma_values = {
            "MA5": round(ma5, 2),
            "MA10": round(ma10, 2),
            "MA20": round(ma20, 2),
            "MA60": round(ma60, 2),
        }
        
        # 计算多头排列得分
        # 完美多头排列: price > MA5 > MA10 > MA20 > MA60
        score = 0
        total_checks = 6
        
        if current_price > ma5:
            score += 1
        if current_price > ma10:
            score += 1
        if current_price > ma20:
            score += 1
        if ma5 > ma10:
            score += 1
        if ma10 > ma20:
            score += 1
        if ma20 > ma60:
            score += 1
        
        trend_score = (score / total_checks) * 100
        
        # 判断排列类型
        if score == total_checks:
            alignment = "perfect_bullish"
        elif score >= 4:
            alignment = "bullish"
        elif score <= 2:
            alignment = "bearish"
        else:
            alignment = "mixed"
        
        return {
            "trend_score": round(trend_score, 2),
            "ma_values": ma_values,
            "alignment": alignment,
        }
        
    except Exception as e:
        logger.error(f"计算趋势强度失败: {e}")
        return {
            "trend_score": 50.0,
            "ma_values": {},
            "alignment": "unknown",
        }


def calc_all_factors(hist: pd.DataFrame, info: Dict) -> Dict:
    """
    计算所有因子
    
    Args:
        hist: 历史数据DataFrame
        info: ETF基本面信息
    
    Returns:
        Dict包含所有因子计算结果
    """
    momentum = calc_momentum_percentile(hist)
    valuation = calc_valuation_percentile(hist, info)
    divergence = calc_divergence(
        momentum["momentum_score"],
        valuation["valuation_score"]
    )
    trend = calc_trend_strength(hist)
    
    return {
        "momentum": momentum,
        "valuation": valuation,
        "divergence": divergence,
        "trend": trend,
    }

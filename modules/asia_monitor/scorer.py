# -*- coding: utf-8 -*-
"""
多空评分算法模块
实现最终多空分数计算 [-100, +100]
"""

import pandas as pd
import numpy as np
from typing import Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

from .config import ASIA_MARKETS, SCORE_WEIGHTS, get_score_label
from .data_fetcher import fetch_market_data, fetch_all_markets
from .factors import calc_all_factors

logger = logging.getLogger(__name__)


def calculate_bull_bear_score(factors: Dict) -> Tuple[float, Dict]:
    """
    计算单个市场的多空分数
    
    最终 = 0.3 × 动量分位 + 0.3 × (100-估值分位) + 0.2 × 背离度 + 0.2 × 趋势强度
    
    Args:
        factors: calc_all_factors返回的因子字典
    
    Returns:
        Tuple of (score, details_dict)
    """
    momentum_score = factors["momentum"]["momentum_score"]
    valuation_score = factors["valuation"]["valuation_score"]
    divergence_score = factors["divergence"]["divergence_score"]
    trend_score = factors["trend"]["trend_score"]
    
    # 计算最终分数
    # 动量分位：越高越好（+0.3权重）
    # 估值分位：越低越好，所以用100减去（+0.3权重）
    # 背离度：正数表示多头机会（+0.2权重）
    # 趋势强度：越高越好（+0.2权重）
    
    final_score = (
        SCORE_WEIGHTS["momentum"] * momentum_score +
        SCORE_WEIGHTS["valuation"] * (100 - valuation_score) +
        SCORE_WEIGHTS["divergence"] * divergence_score +
        SCORE_WEIGHTS["trend"] * trend_score
    )
    
    # 限制在 [-100, 100] 范围内
    final_score = max(min(final_score, 100), -100)
    
    # 获取标签和颜色
    label, color = get_score_label(final_score)
    
    # 生成分析结论
    conclusion = _generate_conclusion(
        final_score, momentum_score, valuation_score, 
        divergence_score, trend_score
    )
    
    details = {
        "final_score": round(final_score, 2),
        "label": label,
        "color": color,
        "momentum_score": momentum_score,
        "valuation_score": valuation_score,
        "divergence_score": divergence_score,
        "trend_score": trend_score,
        "conclusion": conclusion,
        "components": {
            "momentum_weighted": round(SCORE_WEIGHTS["momentum"] * momentum_score, 2),
            "valuation_weighted": round(SCORE_WEIGHTS["valuation"] * (100 - valuation_score), 2),
            "divergence_weighted": round(SCORE_WEIGHTS["divergence"] * divergence_score, 2),
            "trend_weighted": round(SCORE_WEIGHTS["trend"] * trend_score, 2),
        },
    }
    
    return final_score, details


def _generate_conclusion(
    final_score: float,
    momentum: float,
    valuation: float,
    divergence: float,
    trend: float
) -> str:
    """
    生成分析结论
    
    Args:
        final_score: 最终分数
        momentum: 动量分位
        valuation: 估值分位
        divergence: 背离度
        trend: 趋势强度
    
    Returns:
        结论字符串
    """
    if final_score >= 60:
        if momentum >= 70 and valuation <= 40:
            return "动量强劲且估值合理，非泡沫状态，多头可追"
        elif momentum >= 70 and valuation > 60:
            return "动量强劲但估值偏高，可能处于牛市末端，谨慎追高"
        else:
            return "整体偏多，建议持有"
    elif final_score >= 20:
        if divergence > 20:
            return "动量温和领先，估值合理，继续持有"
        else:
            return "温和偏多，可适量参与"
    elif final_score >= -20:
        return "多空均衡，建议观望"
    elif final_score >= -60:
        if divergence < -20:
            return "动量弱且估值偏高，谨慎"
        else:
            return "温和偏空，减仓或观望"
    else:
        if momentum < 30 and valuation > 60:
            return "动量极弱且估值贵，可能是空头陷阱，观望"
        elif momentum < 30:
            return "动量极弱，空头延续，等待反转信号"
        else:
            return "强空头，建议回避"


def calculate_market_score(code: str, use_cache: bool = True) -> Optional[Dict]:
    """
    计算单个市场的完整评分
    
    Args:
        code: 市场代码
        use_cache: 是否使用缓存
    
    Returns:
        包含所有信息的字典
    """
    market_info = ASIA_MARKETS.get(code)
    if not market_info:
        logger.error(f"未知市场代码: {code}")
        return None
    
    # 获取数据
    hist, info = fetch_market_data(code, use_cache=use_cache)
    
    if hist is None or hist.empty:
        logger.warning(f"市场 {code} 无历史数据")
        return {
            "code": code,
            "market_info": market_info,
            "has_data": False,
            "factors": None,
            "score": None,
            "details": None,
        }
    
    # 计算因子
    factors = calc_all_factors(hist, info)
    
    # 计算分数
    score, details = calculate_bull_bear_score(factors)
    
    return {
        "code": code,
        "market_info": market_info,
        "has_data": True,
        "factors": factors,
        "score": score,
        "details": details,
    }


def calculate_all_scores(use_cache: bool = True, max_workers: int = 6) -> Dict[str, Dict]:
    """
    计算所有12个市场的评分（串行计算以避免速率限制）
    
    Args:
        use_cache: 是否使用缓存
        max_workers: 保留参数，不再使用
    
    Returns:
        Dict mapping market code to score results
    """
    results = {}
    
    # 串行计算，避免速率限制
    for code in ASIA_MARKETS.keys():
        try:
            result = calculate_market_score(code, use_cache=use_cache)
            results[code] = result
            logger.info(f"成功计算 {code} 市场评分")
        except Exception as e:
            logger.error(f"计算市场 {code} 评分失败: {e}")
            results[code] = {
                "code": code,
                "market_info": ASIA_MARKETS.get(code),
                "has_data": False,
                "factors": None,
                "score": None,
                "details": None,
            }
    
    return results


def get_score_summary(scores: Dict[str, Dict]) -> Dict:
    """
    生成所有市场的评分摘要
    
    Args:
        scores: calculate_all_scores返回的结果
    
    Returns:
        摘要信息
    """
    valid_scores = [
        (code, result["score"]) 
        for code, result in scores.items() 
        if result["has_data"] and result["score"] is not None
    ]
    
    if not valid_scores:
        return {
            "total_markets": len(scores),
            "valid_markets": 0,
            "bull_count": 0,
            "bear_count": 0,
            "neutral_count": 0,
            "avg_score": 0,
            "strongest_bull": None,
            "strongest_bear": None,
        }
    
    bull_count = sum(1 for _, score in valid_scores if score > 20)
    bear_count = sum(1 for _, score in valid_scores if score < -20)
    neutral_count = len(valid_scores) - bull_count - bear_count
    
    avg_score = sum(score for _, score in valid_scores) / len(valid_scores)
    
    # 找最强多头和最强空头
    sorted_scores = sorted(valid_scores, key=lambda x: x[1], reverse=True)
    strongest_bull = sorted_scores[0] if sorted_scores else None
    strongest_bear = sorted_scores[-1] if sorted_scores else None
    
    return {
        "total_markets": len(scores),
        "valid_markets": len(valid_scores),
        "bull_count": bull_count,
        "bear_count": bear_count,
        "neutral_count": neutral_count,
        "avg_score": round(avg_score, 2),
        "strongest_bull": {
            "code": strongest_bull[0],
            "score": strongest_bull[1],
            "name": ASIA_MARKETS[strongest_bull[0]]["name"],
            "flag": ASIA_MARKETS[strongest_bull[0]]["flag"],
        } if strongest_bull else None,
        "strongest_bear": {
            "code": strongest_bear[0],
            "score": strongest_bear[1],
            "name": ASIA_MARKETS[strongest_bear[0]]["name"],
            "flag": ASIA_MARKETS[strongest_bear[0]]["flag"],
        } if strongest_bear else None,
    }


def get_market_ranking(scores: Dict[str, Dict]) -> list:
    """
    按分数排序获取市场排名
    
    Args:
        scores: calculate_all_scores返回的结果
    
    Returns:
        排序后的列表
    """
    ranking = []
    
    for code, result in scores.items():
        if result["has_data"] and result["score"] is not None:
            ranking.append({
                "code": code,
                "name": result["market_info"]["name"],
                "name_en": result["market_info"]["name_en"],
                "flag": result["market_info"]["flag"],
                "etf_code": result["market_info"]["etf_code"],
                "score": result["score"],
                "label": result["details"]["label"],
                "color": result["details"]["color"],
                "momentum": result["factors"]["momentum"]["momentum_score"],
                "valuation": result["factors"]["valuation"]["valuation_score"],
                "divergence": result["factors"]["divergence"]["divergence_score"],
                "trend": result["factors"]["trend"]["trend_score"],
            })
    
    # 按分数降序排序
    ranking.sort(key=lambda x: x["score"], reverse=True)
    
    return ranking

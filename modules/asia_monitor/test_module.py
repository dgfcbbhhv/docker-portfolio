# -*- coding: utf-8 -*-
"""
亚洲股市AI泡沫多空监测器模块测试
在项目根目录运行: python3 -m modules.asia_monitor.test_module
"""

import sys
import os

# 确保项目根目录在路径中
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from modules.asia_monitor.config import ASIA_MARKETS, get_score_label, get_market_list
from modules.asia_monitor.data_fetcher import fetch_etf_history, fetch_etf_info
from modules.asia_monitor.factors import calc_momentum_percentile, calc_valuation_percentile, calc_divergence
from modules.asia_monitor.scorer import calculate_market_score, calculate_all_scores

def test_config():
    print("=== 测试配置模块 ===")
    print(f"市场数量: {len(ASIA_MARKETS)}")
    print(f"市场列表: {get_market_list()}")
    print(f"分数标签测试: 75分 -> {get_score_label(75)}")
    print(f"分数标签测试: -30分 -> {get_score_label(-30)}")
    print()

def test_data_fetcher():
    print("=== 测试数据获取模块 ===")
    code = "JP"
    print(f"获取 {code} 市场数据...")
    hist = fetch_etf_history(code, period="1y", use_cache=False)
    if hist is not None:
        print(f"  历史数据: {len(hist)} 条记录")
        print(f"  最新价格: {hist['Close'].iloc[-1]:.2f}")
    else:
        print("  历史数据获取失败")
    info = fetch_etf_info(code, use_cache=False)
    if info is not None:
        print(f"  基本面数据: PE={info.get('pe_ratio')}, 股息率={info.get('dividend_yield')}")
    else:
        print("  基本面数据获取失败")
    print()

def test_factors():
    print("=== 测试因子计算模块 ===")
    code = "JP"
    hist = fetch_etf_history(code, period="2y", use_cache=False)
    info = fetch_etf_info(code, use_cache=False)
    if hist is not None:
        momentum = calc_momentum_percentile(hist)
        print(f"动量分位: {momentum['momentum_score']:.2f}")
        print(f"  20日收益率: {momentum['ret_20d']:.2f}%")
        print(f"  60日收益率: {momentum['ret_60d']:.2f}%")
        print(f"  距高点位置: {momentum['pct_from_high']:.2f}%")
        if info:
            valuation = calc_valuation_percentile(hist, info)
            print(f"估值分位: {valuation['valuation_score']:.2f}")
            divergence = calc_divergence(momentum['momentum_score'], valuation['valuation_score'])
            print(f"背离度: {divergence['divergence_score']:.2f}")
            print(f"背离类型: {divergence['divergence_type']}")
    print()

def test_scorer():
    print("=== 测试评分模块 ===")
    code = "JP"
    print(f"计算 {code} 市场评分...")
    result = calculate_market_score(code, use_cache=False)
    if result and result['has_data']:
        print(f"  多空分数: {result['score']:.2f}")
        print(f"  标签: {result['details']['label']}")
        print(f"  结论: {result['details']['conclusion']}")
    else:
        print("  评分计算失败")
    print()

def main():
    print("亚洲股市AI泡沫多空监测器模块测试")
    print("=" * 50)
    try:
        test_config()
        test_data_fetcher()
        test_factors()
        test_scorer()
        print("=" * 50)
        print("所有测试完成！")
    except Exception as e:
        print(f"测试过程中出错: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()

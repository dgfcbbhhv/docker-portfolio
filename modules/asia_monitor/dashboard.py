# -*- coding: utf-8 -*-
"""
ECharts可视化仪表盘模块
实现三Tab可视化界面：多空全景、背离散点图、单市场详情
"""

import streamlit as st
import pandas as pd
import numpy as np
from typing import Dict, List, Optional
import json

from .config import ASIA_MARKETS, SCORE_RANGES, get_score_label
from .scorer import (
    calculate_all_scores, get_score_summary, 
    get_market_ranking, calculate_market_score
)
from .data_fetcher import get_data_status, clear_cache
from .factors import calc_all_factors


def render_dashboard():
    """
    渲染主仪表盘
    """
    st.subheader("🌏 亚洲股市 AI 泡沫多空监测器")
    st.caption("基于动量-估值背离因子，实时监测12个亚洲市场多空状态")
    
    # 侧边栏控制
    with st.sidebar:
        st.markdown("### ⚙️ 控制面板")
        
        if st.button("🔄 刷新数据", use_container_width=True):
            with st.spinner("正在获取最新数据..."):
                st.session_state["asia_scores"] = calculate_all_scores(use_cache=False)
                st.session_state["asia_refreshed"] = True
            st.success("数据已刷新")
            st.rerun()
        
        if st.button("🗑️ 清除缓存", use_container_width=True):
            clear_cache()
            st.success("缓存已清除")
        
        st.divider()
        
        # 数据状态
        st.markdown("### 📊 数据状态")
        data_status = get_data_status()
        for code, status in data_status.items():
            market = ASIA_MARKETS[code]
            if status["has_data"]:
                icon = "✅" if not status["is_stale"] else "⚠️"
                st.text(f"{icon} {market['flag']} {market['name']}")
            else:
                st.text(f"❌ {market['flag']} {market['name']}")
    
    # 计算或加载数据
    if "asia_scores" not in st.session_state:
        with st.spinner("正在计算12个市场评分（首次加载约需30秒）..."):
            st.session_state["asia_scores"] = calculate_all_scores()
    
    scores = st.session_state["asia_scores"]
    summary = get_score_summary(scores)
    ranking = get_market_ranking(scores)
    
    # Tab界面
    tab1, tab2, tab3 = st.tabs([
        "📊 多空全景", 
        "📈 背离散点图", 
        "🔍 单市场详情"
    ])
    
    with tab1:
        _render_overview_tab(summary, ranking, scores)
    
    with tab2:
        _render_scatter_tab(ranking)
    
    with tab3:
        _render_detail_tab(scores)


def _render_overview_tab(summary: Dict, ranking: List[Dict], scores: Dict):
    """
    渲染多空全景Tab
    """
    st.markdown("#### 亚洲多空比例仪表盘")
    
    # 多空比例仪表盘
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric(
            label="🟢 多头市场",
            value=summary["bull_count"],
            delta=f"{summary['bull_count']/summary['valid_markets']*100:.0f}%" if summary["valid_markets"] > 0 else "0%"
        )
    with col2:
        st.metric(
            label="🟡 中性市场",
            value=summary["neutral_count"],
            delta=f"{summary['neutral_count']/summary['valid_markets']*100:.0f}%" if summary["valid_markets"] > 0 else "0%"
        )
    with col3:
        st.metric(
            label="🔴 空头市场",
            value=summary["bear_count"],
            delta=f"{summary['bear_count']/summary['valid_markets']*100:.0f}%" if summary["valid_markets"] > 0 else "0%"
        )
    
    # 平均分和最强/最弱
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric(
            label="📈 亚洲平均分",
            value=f"{summary['avg_score']:.1f}",
            delta=get_score_label(summary["avg_score"])[0]
        )
    with col2:
        if summary["strongest_bull"]:
            bull = summary["strongest_bull"]
            st.metric(
                label="最强多头",
                value=f"{bull['flag']} {bull['name']}",
                delta=f"+{bull['score']:.1f}"
            )
    with col3:
        if summary["strongest_bear"]:
            bear = summary["strongest_bear"]
            st.metric(
                label="最强空头",
                value=f"{bear['flag']} {bear['name']}",
                delta=f"{bear['score']:.1f}"
            )
    
    st.divider()
    
    # 多空热力图
    st.markdown("#### 多空热力图 (12个市场)")
    
    # 使用Streamlit的progress bar创建热力图效果
    for item in ranking:
        code = item["code"]
        market = ASIA_MARKETS[code]
        score = item["score"]
        label = item["label"]
        color = item["color"]
        
        # 将分数映射到0-100范围用于进度条
        progress_value = (score + 100) / 200  # [-100, 100] -> [0, 1]
        
        col1, col2, col3, col4 = st.columns([2, 4, 1, 1])
        with col1:
            st.text(f"{market['flag']} {market['name']}")
        with col2:
            st.progress(progress_value)
        with col3:
            st.text(f"{score:+.1f}")
        with col4:
            st.text(label)
    
    st.divider()
    
    # 市场排名表格
    st.markdown("#### 详细排名")
    
    ranking_df = pd.DataFrame(ranking)
    if not ranking_df.empty:
        ranking_df = ranking_df[["flag", "name", "etf_code", "score", "label", "momentum", "valuation", "divergence", "trend"]]
        ranking_df.columns = ["市场", "名称", "ETF", "多空分", "状态", "动量分位", "估值分位", "背离度", "趋势强度"]
        
        st.dataframe(
            ranking_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "多空分": st.column_config.NumberColumn(
                    "多空分",
                    format="%.1f",
                ),
                "动量分位": st.column_config.NumberColumn(
                    "动量分位",
                    format="%.1f",
                ),
                "估值分位": st.column_config.NumberColumn(
                    "估值分位",
                    format="%.1f",
                ),
                "背离度": st.column_config.NumberColumn(
                    "背离度",
                    format="%.1f",
                ),
                "趋势强度": st.column_config.NumberColumn(
                    "趋势强度",
                    format="%.1f",
                ),
            }
        )


def _render_scatter_tab(ranking: List[Dict]):
    """
    渲染背离散点图Tab
    """
    st.markdown("#### 动量-估值背离散点图")
    st.caption("X轴: 估值分位(越高越贵) | Y轴: 动量分位(越高越强)")
    st.caption("对角线上方 = 多头背离(动量强于估值) | 对角线下方 = 空头背离(动量弱于估值)")
    
    # 准备数据
    scatter_data = []
    for item in ranking:
        scatter_data.append({
            "name": f"{item['flag']} {item['name']}",
            "code": item["code"],
            "x": item["valuation"],  # 估值分位
            "y": item["momentum"],   # 动量分位
            "score": item["score"],
            "label": item["label"],
        })
    
    # 创建ECharts散点图配置
    chart_option = {
        "tooltip": {
            "trigger": "item",
            "formatter": """
                function(params) {
                    return params.data.name + '<br/>' +
                           '动量分位: ' + params.data.y.toFixed(1) + '%<br/>' +
                           '估值分位: ' + params.data.x.toFixed(1) + '%<br/>' +
                           '多空分: ' + params.data.score.toFixed(1);
                }
            """,
        },
        "xAxis": {
            "name": "估值分位 (%)",
            "nameLocation": "middle",
            "nameGap": 30,
            "min": 0,
            "max": 100,
        },
        "yAxis": {
            "name": "动量分位 (%)",
            "nameLocation": "middle",
            "nameGap": 40,
            "min": 0,
            "max": 100,
        },
        "series": [{
            "type": "scatter",
            "symbolSize": lambda data: max(20, abs(data[2]) / 2),
            "data": [
                {
                    "name": item["name"],
                    "value": [item["x"], item["y"], item["score"]],
                    "code": item["code"],
                    "score": item["score"],
                    "label": item["label"],
                }
                for item in scatter_data
            ],
            "label": {
                "show": True,
                "position": "right",
                "formatter": lambda params: params.data["name"],
            },
            "itemStyle": {
                "color": lambda params: (
                    "#00ff88" if params.data["score"] > 20 else
                    "#ff4444" if params.data["score"] < -20 else
                    "#ffff88"
                ),
            },
        }],
        "markLine": {
            "data": [
                {"xAxis": 50, "lineStyle": {"type": "dashed", "color": "#666"}},
                {"yAxis": 50, "lineStyle": {"type": "dashed", "color": "#666"}},
            ],
        },
    }
    
    # 使用Streamlit的ECharts组件
    try:
        from streamlit_echarts import st_echarts
        st_echarts(chart_option, height="500px")
    except ImportError:
        # 如果没有streamlit_echarts，使用简单的表格显示
        st.warning("streamlit_echarts未安装，使用表格显示")
        st.dataframe(pd.DataFrame(scatter_data))
    
    # 四象限说明
    st.divider()
    st.markdown("#### 四象限解读")
    
    col1, col2 = st.columns(2)
    with col1:
        st.info("**右上象限 (动量高+估值高)**\n\n可能处于牛市末端，动量强但估值贵")
        st.success("**左上象限 (动量高+估值低)**\n\n多头背离：价格强但便宜，是买入机会")
    with col2:
        st.warning("**右下象限 (动量低+估值高)**\n\n底部背离：价格弱但已贵，可能接近底部")
        st.error("**左下象限 (动量低+估值低)**\n\n空头延续：价格弱且便宜，还没到反转时机")


def _render_detail_tab(scores: Dict):
    """
    渲染单市场详情Tab
    """
    st.markdown("#### 单市场详情")
    
    # 市场选择
    market_options = {f"{v['flag']} {v['name']} ({v['etf_code']})": k for k, v in ASIA_MARKETS.items()}
    selected_label = st.selectbox("选择市场", options=list(market_options.keys()))
    selected_code = market_options[selected_label]
    
    # 获取选中市场的数据
    result = scores.get(selected_code)
    
    if result is None or not result["has_data"]:
        st.warning(f"市场 {ASIA_MARKETS[selected_code]['name']} 暂无数据")
        return
    
    market = result["market_info"]
    factors = result["factors"]
    details = result["details"]
    
    # 市场概览
    st.markdown(f"### {market['flag']} {market['name']} ({market['etf_code']})")
    
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric(
            label="多空分数",
            value=f"{result['score']:+.1f}",
            delta=details["label"]
        )
    with col2:
        st.metric(
            label="动量分位",
            value=f"{factors['momentum']['momentum_score']:.1f}%",
            delta=f"20日: {factors['momentum']['ret_20d']:+.1f}%"
        )
    with col3:
        st.metric(
            label="估值分位",
            value=f"{factors['valuation']['valuation_score']:.1f}%",
            delta=f"PE: {factors['valuation']['pe_ratio']:.1f}" if factors['valuation']['pe_ratio'] else "PE: N/A"
        )
    with col4:
        st.metric(
            label="背离度",
            value=f"{factors['divergence']['divergence_score']:+.1f}",
            delta=factors['divergence']['divergence_type']
        )
    
    st.divider()
    
    # 分数分解
    st.markdown("#### 分数分解")
    
    components = details["components"]
    
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**加权分数组成：**")
        st.text(f"动量贡献: {components['momentum_weighted']:+.1f}")
        st.text(f"估值贡献: {components['valuation_weighted']:+.1f}")
        st.text(f"背离贡献: {components['divergence_weighted']:+.1f}")
        st.text(f"趋势贡献: {components['trend_weighted']:+.1f}")
    
    with col2:
        st.markdown("**详细指标：**")
        st.text(f"20日收益率: {factors['momentum']['ret_20d']:+.1f}%")
        st.text(f"60日收益率: {factors['momentum']['ret_60d']:+.1f}%")
        st.text(f"距52周高点: {factors['momentum']['pct_from_high']:+.1f}%")
        st.text(f"趋势强度: {factors['trend']['trend_score']:.1f}%")
    
    st.divider()
    
    # 均线状态
    st.markdown("#### 均线多头排列")
    
    trend = factors["trend"]
    if trend["ma_values"]:
        ma_cols = st.columns(4)
        for i, (ma_name, ma_value) in enumerate(trend["ma_values"].items()):
            with ma_cols[i]:
                st.metric(label=ma_name, value=f"{ma_value:.2f}")
        
        st.text(f"排列状态: {trend['alignment']}")
    
    st.divider()
    
    # 分析结论
    st.markdown("#### 💡 分析结论")
    st.info(details["conclusion"])
    
    # 分数分布说明
    st.divider()
    st.markdown("#### 📊 分数区间说明")
    
    for key, range_info in SCORE_RANGES.items():
        st.text(f"{range_info['min']:3d} ~ {range_info['max']:3d}: {range_info['label']}")

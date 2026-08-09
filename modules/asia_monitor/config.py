# -*- coding: utf-8 -*-
"""
12个亚洲市场配置
包含指数代码、ETF代码、投资货币、数据覆盖度
"""

ASIA_MARKETS = {
    "JP": {
        "name": "日本",
        "name_en": "Japan",
        "flag": "🇯🇵",
        "index_code": "^N225",
        "etf_code": "EWJ",
        "currency": "USD",
        "coverage": 5,
        "description": "日经225指数",
    },
    "KR": {
        "name": "韩国",
        "name_en": "South Korea",
        "flag": "🇰🇷",
        "index_code": "^KS11",
        "etf_code": "EWY",
        "currency": "USD",
        "coverage": 5,
        "description": "韩国综合指数",
    },
    "SG": {
        "name": "新加坡",
        "name_en": "Singapore",
        "flag": "🇸🇬",
        "index_code": "^STI",
        "etf_code": "EWS",
        "currency": "USD",
        "coverage": 5,
        "description": "海峡时报指数",
    },
    "IN": {
        "name": "印度",
        "name_en": "India",
        "flag": "🇮🇳",
        "index_code": "^NSEI",
        "etf_code": "INDA",
        "currency": "USD",
        "coverage": 5,
        "description": "印度Nifty50指数",
    },
    "TW": {
        "name": "台湾",
        "name_en": "Taiwan",
        "flag": "🇹🇼",
        "index_code": "^TWII",
        "etf_code": "EWT",
        "currency": "USD",
        "coverage": 5,
        "description": "台湾加权指数",
    },
    "HK": {
        "name": "香港",
        "name_en": "Hong Kong",
        "flag": "🇭🇰",
        "index_code": "^HSI",
        "etf_code": "EWH",
        "currency": "USD",
        "coverage": 5,
        "description": "恒生指数",
    },
    "CN": {
        "name": "中国",
        "name_en": "China",
        "flag": "🇨🇳",
        "index_code": "000300.SS",
        "etf_code": "MCHI",
        "currency": "USD",
        "coverage": 5,
        "description": "沪深300指数",
    },
    "ID": {
        "name": "印尼",
        "name_en": "Indonesia",
        "flag": "🇮🇩",
        "index_code": "^JKSE",
        "etf_code": "EIDO",
        "currency": "USD",
        "coverage": 4,
        "description": "雅加达综合指数",
    },
    "MY": {
        "name": "马来西亚",
        "name_en": "Malaysia",
        "flag": "🇲🇾",
        "index_code": "^KLSE",
        "etf_code": "EWM",
        "currency": "USD",
        "coverage": 4,
        "description": "马来西亚吉隆坡综合指数",
    },
    "TH": {
        "name": "泰国",
        "name_en": "Thailand",
        "flag": "🇹🇭",
        "index_code": "^SET",
        "etf_code": "THD",
        "currency": "USD",
        "coverage": 4,
        "description": "泰国SET指数",
    },
    "PH": {
        "name": "菲律宾",
        "name_en": "Philippines",
        "flag": "🇵🇭",
        "index_code": "^PSI",
        "etf_code": "EPHE",
        "currency": "USD",
        "coverage": 3,
        "description": "菲律宾PSEi指数",
    },
    "VN": {
        "name": "越南",
        "name_en": "Vietnam",
        "flag": "🇻🇳",
        "index_code": "^VNINDEX",
        "etf_code": "VNM",
        "currency": "USD",
        "coverage": 4,
        "description": "越南胡志明指数",
    },
}

# 多空评分权重配置
SCORE_WEIGHTS = {
    "momentum": 0.30,      # 动量分位权重
    "valuation": 0.30,     # 估值分位权重 (100-valuation)
    "divergence": 0.20,    # 背离度权重
    "trend": 0.20,         # 趋势强度权重
}

# 动量子因子权重
MOMENTUM_WEIGHTS = {
    "ret_20d": 0.40,       # 20日收益率权重
    "ret_60d": 0.30,       # 60日收益率权重
    "pct_from_high": 0.30, # 距52周高点位置权重
}

# 估值子因子权重
VALUATION_WEIGHTS = {
    "pe": 0.50,            # PE权重
    "dividend_yield": 0.30, # 股息率权重
    "price_percentile": 0.20, # 价格历史百分位权重
}

# 多空评分区间定义
SCORE_RANGES = {
    "strong_bull": {"min": 60, "max": 100, "label": "强多头", "color": "#00ff88"},
    "mild_bull": {"min": 20, "max": 60, "label": "温和多头", "color": "#88ff88"},
    "neutral": {"min": -20, "max": 20, "label": "中性", "color": "#ffff88"},
    "mild_bear": {"min": -60, "max": -20, "label": "温和空头", "color": "#ff8888"},
    "strong_bear": {"min": -100, "max": -60, "label": "强空头", "color": "#ff4444"},
}

# 数据获取配置
DATA_CONFIG = {
    "history_period": "2y",      # 历史数据周期
    "cache_ttl": 604800,         # 缓存过期时间(秒) - 7天
    "momentum_lookback": 252,    # 动量计算回溯天数(1年)
    "valuation_lookback": 1095,  # 估值计算回溯天数(3年)
}

def get_score_label(score):
    """根据分数获取多空标签和颜色"""
    for key, range_info in SCORE_RANGES.items():
        if range_info["min"] <= score <= range_info["max"]:
            return range_info["label"], range_info["color"]
    return "未知", "#888888"

def get_market_list():
    """获取所有市场代码列表"""
    return list(ASIA_MARKETS.keys())

def get_market_info(code):
    """获取指定市场的配置信息"""
    return ASIA_MARKETS.get(code)

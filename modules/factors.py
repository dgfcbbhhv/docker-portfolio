"""Alpha因子计算模块
参考 EasyXT 的101因子平台，实现10个核心Alpha因子：
- 动量类：alpha001, alpha012, alpha041
- 反转类：alpha003, alpha023, alpha054
- 波动率类：alpha010, alpha030, alpha078
- 质量类：alpha101
"""
import numpy as np
import pandas as pd

class FactorCalculator:
    def __init__(self, df):
        self.df = df.copy().sort_values("date").reset_index(drop=True)
        self.close = self.df["close"].values
        self.open = self.df["open"].values if "open" in self.df.columns else self.close
        self.high = self.df["high"].values if "high" in self.df.columns else self.close
        self.low = self.df["low"].values if "low" in self.df.columns else self.close
        self.volume = self.df["volume"].values if "volume" in self.df.columns else np.ones_like(self.close)
        
        self.n = len(self.close)
    
    def ts_sum(self, x, period):
        result = np.zeros_like(x)
        for i in range(self.n):
            result[i] = np.mean(x[max(0, i - period + 1):i + 1])
        return result
    
    def ts_std(self, x, period):
        result = np.zeros_like(x)
        for i in range(self.n):
            window = x[max(0, i - period + 1):i + 1]
            result[i] = np.std(window) if len(window) >= 2 else 0.0
        return result
    
    def ts_max(self, x, period):
        result = np.zeros_like(x)
        for i in range(self.n):
            result[i] = np.max(x[max(0, i - period + 1):i + 1])
        return result
    
    def ts_min(self, x, period):
        result = np.zeros_like(x)
        for i in range(self.n):
            result[i] = np.min(x[max(0, i - period + 1):i + 1])
        return result
    
    def delay(self, x, n):
        result = np.zeros_like(x)
        result[n:] = x[:-n]
        return result
    
    def rank(self, x):
        return np.argsort(np.argsort(x)) / (len(x) - 1)
    
    def sign(self, x):
        return np.sign(x)
    
    def alpha001(self):
        return (self.close - self.delay(self.close, 1)) / self.delay(self.close, 1)
    
    def alpha003(self):
        return -1 * self.sign(self.close - self.delay(self.close, 1)) * (self.high - self.low)
    
    def alpha010(self):
        return self.ts_std(self.close, 20)
    
    def alpha012(self):
        return (self.close - self.delay(self.close, 20)) / self.delay(self.close, 20)
    
    def alpha023(self):
        return -1 * (self.close - self.delay(self.close, 5)) / self.ts_std(self.close, 5)
    
    def alpha030(self):
        return (self.high - self.close) / (self.high - self.low + 1e-8)
    
    def alpha041(self):
        return self.sign(self.close - self.delay(self.close, 1)) * self.volume
    
    def alpha054(self):
        return -1 * self.sign(self.close - self.delay(self.close, 1)) * (self.close - self.open)
    
    def alpha078(self):
        return self.ts_std(self.close, 60) - self.ts_std(self.close, 20)
    
    def alpha101(self):
        return self.delay(self.close, 1) - self.delay(self.close, 2)
    
    def calculate_all(self):
        factors = {}
        factors["alpha001"] = self.alpha001()
        factors["alpha003"] = self.alpha003()
        factors["alpha010"] = self.alpha010()
        factors["alpha012"] = self.alpha012()
        factors["alpha023"] = self.alpha023()
        factors["alpha030"] = self.alpha030()
        factors["alpha041"] = self.alpha041()
        factors["alpha054"] = self.alpha054()
        factors["alpha078"] = self.alpha078()
        factors["alpha101"] = self.alpha101()
        
        return factors
    
    def calculate_factor(self, factor_name):
        methods = {
            "alpha001": self.alpha001,
            "alpha003": self.alpha003,
            "alpha010": self.alpha010,
            "alpha012": self.alpha012,
            "alpha023": self.alpha023,
            "alpha030": self.alpha030,
            "alpha041": self.alpha041,
            "alpha054": self.alpha054,
            "alpha078": self.alpha078,
            "alpha101": self.alpha101,
        }
        
        if factor_name in methods:
            return methods[factor_name]()
        return None

FACTOR_DESCRIPTIONS = {
    "alpha001": {"name": "日收益率", "type": "动量", "description": "当日收盘价相对前一日的收益率"},
    "alpha003": {"name": "反转幅度", "type": "反转", "description": "-1 × 价格方向 × (最高价-最低价)"},
    "alpha010": {"name": "20日波动率", "type": "波动率", "description": "最近20日收盘价的标准差"},
    "alpha012": {"name": "20日动量", "type": "动量", "description": "20日累计收益率"},
    "alpha023": {"name": "5日反转", "type": "反转", "description": "-1 × 5日收益率 / 5日波动率"},
    "alpha030": {"name": "收盘位置", "type": "技术面", "description": "(最高价-收盘价)/(最高价-最低价)"},
    "alpha041": {"name": "量价协同", "type": "量价", "description": "价格方向 × 成交量"},
    "alpha054": {"name": "开盘反转", "type": "反转", "description": "-1 × 价格方向 × (收盘价-开盘价)"},
    "alpha078": {"name": "波动率变化", "type": "波动率", "description": "60日波动率 - 20日波动率"},
    "alpha101": {"name": "短期趋势", "type": "趋势", "description": "前一日收盘价变化"},
}

def calculate_factors_for_stock(df, factor_names=None):
    if df is None or df.empty:
        return None
    
    calculator = FactorCalculator(df)
    
    if factor_names:
        results = {}
        for name in factor_names:
            results[name] = calculator.calculate_factor(name)
        return results
    else:
        return calculator.calculate_all()

def get_factor_descriptions():
    return FACTOR_DESCRIPTIONS

def get_factor_types():
    types = set()
    for f in FACTOR_DESCRIPTIONS.values():
        types.add(f["type"])
    return sorted(list(types))

def filter_factors_by_type(factor_type):
    result = {}
    for name, desc in FACTOR_DESCRIPTIONS.items():
        if desc["type"] == factor_type:
            result[name] = desc
    return result

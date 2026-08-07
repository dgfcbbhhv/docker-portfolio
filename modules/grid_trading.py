"""网格交易策略模块
参考 EasyXT 的网格交易策略：
- 固定网格：价格间隔固定，简单直观
- ATR动态网格：根据ATR自动调整网格间距
"""
import numpy as np
import pandas as pd
from datetime import datetime

# A股交易成本：买入佣金0.025%，卖出佣金0.025%+印花税0.05%
BUY_FEE_RATE = 0.00025
SELL_FEE_RATE = 0.00075

class FixedGridStrategy:
    def __init__(self, lower_bound, upper_bound, grid_count, initial_capital=100000, trade_unit=100):
        self.lower_bound = lower_bound
        self.upper_bound = upper_bound
        self.grid_count = grid_count
        self.initial_capital = initial_capital
        self.trade_unit = trade_unit
        
        self.grid_width = (upper_bound - lower_bound) / grid_count
        self.grids = [lower_bound + i * self.grid_width for i in range(grid_count + 1)]
        
        self.current_capital = initial_capital
        self.position = 0
        self.avg_cost = 0.0
        self.trades = []
    
    def get_grid_level(self, price):
        if price <= self.lower_bound:
            return 0
        if price >= self.upper_bound:
            return self.grid_count
        return int((price - self.lower_bound) / self.grid_width)
    
    def calculate_signal(self, price):
        level = self.get_grid_level(price)
        signals = []
        
        if level <= 2 and self.current_capital > 0:
            signals.append({"action": "buy", "level": level, "reason": f"价格接近下轨(level={level})"})
        
        if level >= self.grid_count - 2 and self.position > 0:
            signals.append({"action": "sell", "level": level, "reason": f"价格接近上轨(level={level})"})
        
        return signals
    
    def execute_trade(self, price, action, volume=None):
        if action == "buy":
            if not volume:
                volume = self.trade_unit
            cost = price * volume
            fee = max(cost * BUY_FEE_RATE, 5.0)  # 佣金最低5元
            total_cost = cost + fee
            if self.current_capital >= total_cost:
                if self.position > 0:
                    self.avg_cost = (self.avg_cost * self.position + price * volume) / (self.position + volume)
                else:
                    self.avg_cost = price
                self.position += volume
                self.current_capital -= total_cost
                self.trades.append({
                    "date": datetime.now(),
                    "action": "buy",
                    "price": price,
                    "volume": volume,
                    "cost": cost,
                    "fee": round(fee, 2),
                    "position": self.position,
                    "capital": self.current_capital,
                    "avg_cost": self.avg_cost
                })
                return True, f"买入 {volume} 股，价格 {price:.2f}，手续费 {fee:.2f}"
            return False, "资金不足"

        elif action == "sell":
            if not volume:
                volume = self.trade_unit
            if self.position >= volume:
                revenue = price * volume
                fee = max(revenue * SELL_FEE_RATE, 5.0)  # 佣金+印花税，最低5元
                net_revenue = revenue - fee
                self.position -= volume
                self.current_capital += net_revenue
                self.trades.append({
                    "date": datetime.now(),
                    "action": "sell",
                    "price": price,
                    "volume": volume,
                    "revenue": revenue,
                    "fee": round(fee, 2),
                    "net_revenue": round(net_revenue, 2),
                    "position": self.position,
                    "capital": self.current_capital,
                    "avg_cost": self.avg_cost if self.position > 0 else 0.0
                })
                return True, f"卖出 {volume} 股，价格 {price:.2f}，手续费 {fee:.2f}"
            return False, "持仓不足"

        return False, "无效操作"
    
    def get_status(self, current_price):
        level = self.get_grid_level(current_price)
        total_assets = self.current_capital + self.position * current_price
        profit = total_assets - self.initial_capital
        profit_pct = (profit / self.initial_capital) * 100
        
        return {
            "current_price": current_price,
            "grid_level": level,
            "position": self.position,
            "avg_cost": self.avg_cost,
            "capital": self.current_capital,
            "total_assets": total_assets,
            "profit": profit,
            "profit_pct": profit_pct,
            "grid_width": self.grid_width,
            "grids": self.grids
        }

class ATRGridStrategy:
    def __init__(self, atr_period=14, grid_multiple=2.0, initial_capital=100000, trade_unit=100):
        self.atr_period = atr_period
        self.grid_multiple = grid_multiple
        self.initial_capital = initial_capital
        self.trade_unit = trade_unit
        
        self.current_capital = initial_capital
        self.position = 0
        self.avg_cost = 0.0
        self.trades = []
        self.atr = None
        self.grid_width = None
        self.center_price = None
    
    def calculate_atr(self, df):
        if len(df) < self.atr_period + 1:
            return None
        
        high = df["high"].values
        low = df["low"].values
        close = df["close"].values
        
        tr1 = high[1:] - low[1:]
        tr2 = np.abs(high[1:] - close[:-1])
        tr3 = np.abs(low[1:] - close[:-1])
        
        tr = np.maximum(np.maximum(tr1, tr2), tr3)
        atr = np.mean(tr[-self.atr_period:])
        
        return atr
    
    def update_grid(self, df, current_price):
        self.atr = self.calculate_atr(df)
        
        if self.atr is None or self.atr <= 0:
            self.grid_width = current_price * 0.02
        else:
            self.grid_width = self.atr * self.grid_multiple
        
        if self.center_price is None:
            self.center_price = current_price
        
        self.grid_width = max(self.grid_width, current_price * 0.005)
    
    def calculate_signal(self, price):
        signals = []
        
        if self.grid_width is None or self.center_price is None:
            return signals
        
        distance_from_center = price - self.center_price
        grid_distance = distance_from_center / self.grid_width
        
        if grid_distance < -2.0 and self.current_capital > 0:
            signals.append({"action": "buy", "distance": grid_distance, "reason": f"低于中心价 {abs(grid_distance):.1f} 个网格"})
        
        if grid_distance > 2.0 and self.position > 0:
            signals.append({"action": "sell", "distance": grid_distance, "reason": f"高于中心价 {grid_distance:.1f} 个网格"})
        
        if grid_distance < -3.0 and self.current_capital > 0:
            signals.append({"action": "buy", "distance": grid_distance, "reason": f"大幅低于中心价，加倍买入"})
        
        if grid_distance > 3.0 and self.position > 0:
            signals.append({"action": "sell", "distance": grid_distance, "reason": f"大幅高于中心价，加倍卖出"})
        
        return signals
    
    def execute_trade(self, price, action, volume=None):
        if action == "buy":
            if not volume:
                volume = self.trade_unit
            cost = price * volume
            fee = max(cost * BUY_FEE_RATE, 5.0)  # 佣金最低5元
            total_cost = cost + fee
            if self.current_capital >= total_cost:
                if self.position > 0:
                    self.avg_cost = (self.avg_cost * self.position + price * volume) / (self.position + volume)
                else:
                    self.avg_cost = price
                self.position += volume
                self.current_capital -= total_cost
                self.trades.append({
                    "date": datetime.now(),
                    "action": "buy",
                    "price": price,
                    "volume": volume,
                    "cost": cost,
                    "fee": round(fee, 2),
                    "position": self.position,
                    "capital": self.current_capital,
                    "avg_cost": self.avg_cost
                })
                return True, f"买入 {volume} 股，价格 {price:.2f}，手续费 {fee:.2f}"
            return False, "资金不足"

        elif action == "sell":
            if not volume:
                volume = self.trade_unit
            if self.position >= volume:
                revenue = price * volume
                fee = max(revenue * SELL_FEE_RATE, 5.0)  # 佣金+印花税，最低5元
                net_revenue = revenue - fee
                self.position -= volume
                self.current_capital += net_revenue
                self.trades.append({
                    "date": datetime.now(),
                    "action": "sell",
                    "price": price,
                    "volume": volume,
                    "revenue": revenue,
                    "fee": round(fee, 2),
                    "net_revenue": round(net_revenue, 2),
                    "position": self.position,
                    "capital": self.current_capital,
                    "avg_cost": self.avg_cost if self.position > 0 else 0.0
                })
                return True, f"卖出 {volume} 股，价格 {price:.2f}，手续费 {fee:.2f}"
            return False, "持仓不足"

        return False, "无效操作"
    
    def get_status(self, current_price):
        total_assets = self.current_capital + self.position * current_price
        profit = total_assets - self.initial_capital
        profit_pct = (profit / self.initial_capital) * 100
        
        distance_from_center = 0.0
        if self.center_price is not None:
            distance_from_center = (current_price - self.center_price) / self.center_price * 100
        
        return {
            "current_price": current_price,
            "atr": self.atr,
            "grid_width": self.grid_width,
            "center_price": self.center_price,
            "distance_from_center_pct": distance_from_center,
            "position": self.position,
            "avg_cost": self.avg_cost,
            "capital": self.current_capital,
            "total_assets": total_assets,
            "profit": profit,
            "profit_pct": profit_pct
        }

def backtest_fixed_grid(df, lower_bound, upper_bound, grid_count, initial_capital=100000, trade_unit=100):
    strategy = FixedGridStrategy(lower_bound, upper_bound, grid_count, initial_capital, trade_unit)
    
    results = []
    for idx, row in df.iterrows():
        price = row["close"]
        signals = strategy.calculate_signal(price)
        
        for signal in signals:
            success, msg = strategy.execute_trade(price, signal["action"], trade_unit)
        
        status = strategy.get_status(price)
        results.append({
            "date": row["date"],
            "price": price,
            "position": status["position"],
            "avg_cost": status["avg_cost"],
            "capital": status["capital"],
            "total_assets": status["total_assets"],
            "profit": status["profit"],
            "profit_pct": status["profit_pct"],
            "grid_level": status["grid_level"]
        })
    
    return pd.DataFrame(results)

def backtest_atr_grid(df, atr_period=14, grid_multiple=2.0, initial_capital=100000, trade_unit=100):
    strategy = ATRGridStrategy(atr_period, grid_multiple, initial_capital, trade_unit)
    
    results = []
    for idx, row in df.iterrows():
        price = row["close"]
        
        sub_df = df.iloc[max(0, idx - atr_period * 2):idx + 1]
        strategy.update_grid(sub_df, price)
        
        signals = strategy.calculate_signal(price)
        
        for signal in signals:
            success, msg = strategy.execute_trade(price, signal["action"], trade_unit)
        
        status = strategy.get_status(price)
        results.append({
            "date": row["date"],
            "price": price,
            "position": status["position"],
            "avg_cost": status["avg_cost"],
            "capital": status["capital"],
            "total_assets": status["total_assets"],
            "profit": status["profit"],
            "profit_pct": status["profit_pct"],
            "atr": status["atr"],
            "grid_width": status["grid_width"],
            "distance_from_center_pct": status["distance_from_center_pct"]
        })
    
    return pd.DataFrame(results)


def kelly_calibrate_grid(strategy, kelly_pct, total_capital):
    allocated = total_capital * kelly_pct / 100.0
    strategy.initial_capital = allocated
    strategy.current_capital = allocated
    return allocated

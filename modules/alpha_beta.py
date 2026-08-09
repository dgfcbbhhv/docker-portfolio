"""Alpha/Beta 收益归因拆解引擎

参考 frank-quant 视频③ 的方法论：
- 不依赖外部基准指数
- 用个股对组合平均（除自身外）回归，分离 Alpha（选股能力）和 Beta（市场敞口）
- 输出归因报告，回答"你的收益到底靠选股还是靠贝塔挣钱"
"""
import numpy as np
import pandas as pd
from scipy import stats

def decompose_stock(returns_df, code):
    r_i = returns_df[code].dropna()
    others = [c for c in returns_df.columns if c != code]
    if not others:
        return None
    r_mkt = returns_df[others].mean(axis=1)
    common = r_i.index.intersection(r_mkt.index)
    if len(common) < 60:
        return None
    x = r_mkt.loc[common].values
    y = r_i.loc[common].values
    slope, intercept, r_value, p_value, std_err = stats.linregress(x, y)
    residuals = y - (intercept + slope * x)
    res_vol = float(np.std(residuals, ddof=1))
    return {
        "code": code,
        "alpha_daily": round(float(intercept), 6),
        "alpha_annualized": round(float(intercept) * 252 * 100, 2),
        "beta": round(float(slope), 4),
        "r_squared": round(float(r_value ** 2), 4),
        "vol_idiosyncratic": round(float(res_vol) * np.sqrt(252) * 100, 2),
    }

def decompose_portfolio(returns_df, weights):
    codes = [c for c in weights if c in returns_df.columns]
    if len(codes) < 2:
        return None
    total_w = sum(weights.get(c, 0) for c in codes)
    if total_w > 0:
        normal_w = {c: weights.get(c, 0) / total_w for c in codes}
    else:
        normal_w = {c: 1.0 / len(codes) for c in codes}
    stocks = []
    total_alpha = 0.0
    total_beta = 0.0
    for code in codes:
        res = decompose_stock(returns_df, code)
        if res is None:
            continue
        w = normal_w[code]
        alpha_contrib = res["alpha_annualized"] / 100.0 * w
        total_alpha += alpha_contrib
        total_beta += (returns_df[code].mean() * 252 - res["alpha_annualized"] / 100.0) * w
        res["weight_pct"] = round(w * 100, 1)
        res["alpha_contribution_annual_pct"] = round(alpha_contrib * 100, 2)
        stocks.append(res)
    port_ret = sum(returns_df[c].mean() * 252 * normal_w[c] for c in codes)
    if port_ret > 1e-12:
        alpha_ratio = round(total_alpha / port_ret * 100, 1)
        beta_ratio = round(100 - alpha_ratio, 1)
    elif port_ret < -1e-12:
        denom = abs(total_alpha) + abs(total_beta)
        if denom > 1e-12:
            alpha_ratio = round(abs(total_alpha) / denom * 100, 1)
            beta_ratio = round(100 - alpha_ratio, 1)
        else:
            alpha_ratio = beta_ratio = 0
    else:
        alpha_ratio = beta_ratio = 0
    return {
        "portfolio_annualized_ret": round(float(port_ret) * 100, 2),
        "alpha_contribution_pct": round(float(total_alpha) * 100, 2),
        "alpha_ratio_pct": alpha_ratio,
        "beta_ratio_pct": beta_ratio,
        "stocks": sorted(stocks, key=lambda x: x["alpha_annualized"], reverse=True),
    }
"""黑天鹅压力测试

场景:
1. 单日暴跌: -10% / -20%
2. 单行业全灭: 银行 / 地产 / 能源
3. 相关性断裂: 0.7 → 0.95
4. Monte Carlo 极端模拟: 1000次
"""
import numpy as np
np.random.seed(42)  # deterministic results
import pandas as pd

from modules.risk import single_day_loss, industry_collapse_loss


INDUSTRY_MAP = {
    "银行": ["601398", "600036", "601288", "601988", "601939"],
    "能源": ["601088", "600938"],
    "电信": ["600941"],
    "电力": ["600900"],
}


def single_day(codes, weights, drops=None):
    """单日暴跌场景。委托 modules.risk.single_day_loss，保持输出形状不变。"""
    if drops is None:
        drops = [-0.10, -0.20]
    rows = [{"weight": weights.get(c, 0)} for c in codes]
    results = []
    for d in drops:
        impact = single_day_loss(rows, d * 100)
        results.append({
            "scenario": f"单日暴跌 {d*100:.0f}%",
            "impact": round(impact, 2),
        })
    return results


def industry_collapse(weights, industry_map=None):
    """单行业全灭场景。委托 modules.risk.industry_collapse_loss，
    逐行业组显式指定 industry 键，保持原"整组取负"语义。"""
    if industry_map is None:
        industry_map = INDUSTRY_MAP
    results = []
    for ind, affected in industry_map.items():
        rows = [
            {"weight": weights.get(c, 0), "industry": ind}
            for c in affected if c in weights
        ]
        impact = industry_collapse_loss(rows) if rows else 0.0
        results.append({
            "scenario": f"{ind}行业全灭",
            "impact": round(impact, 2),
        })
    return results


def correlation_break(codes, weights, returns, new_corr=0.95):
    """相关性断裂: 所有股票相关性升至 new_corr"""
    if returns.empty:
        return {"scenario": "相关性断裂", "error": "无数据"}

    S = returns.cov() * 252
    vols = np.sqrt(np.diag(S))
    n = len(codes)

    # 构造新协方差矩阵
    new_S = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            if i == j:
                new_S[i, j] = vols[i] ** 2
            else:
                new_S[i, j] = new_corr * vols[i] * vols[j]

    w = np.array([weights.get(c, 0) / 100 for c in codes])
    orig_vol = np.sqrt(np.dot(w.T, np.dot(S.values, w))) if hasattr(S, 'values') else np.sqrt(np.dot(w.T, np.dot(S, w)))
    new_vol = np.sqrt(np.dot(w.T, np.dot(new_S, w)))

    return {
        "scenario": f"相关性断裂 (→{new_corr})",
        "original_vol": round(orig_vol * 100, 2),
        "stressed_vol": round(new_vol * 100, 2),
        "increase": round((new_vol - orig_vol) * 100, 2),
    }


def monte_carlo_stress(returns, weights, n_sims=1000, horizon_days=20):
    """Monte Carlo 极端场景模拟"""
    if returns.empty:
        return {"error": "无数据"}

    codes = returns.columns.tolist()
    mu = returns.mean().values
    S = returns.cov().values * 9  # 波动率放大3倍
    w = np.array([weights.get(c, 0) / 100 for c in codes])

    worst = []
    for _ in range(n_sims):
        sim = np.random.multivariate_normal(mu * 0.5, S, horizon_days)
        sim = sim - np.abs(np.random.normal(0, 0.01, sim.shape))
        cum_ret = np.prod(1 + sim @ w) - 1
        worst.append(cum_ret)

    worst = np.array(worst)
    return {
        "scenario": f"Monte Carlo ({n_sims}次, {horizon_days}天)",
        "1pct": round(np.percentile(worst, 1) * 100, 2),
        "5pct": round(np.percentile(worst, 5) * 100, 2),
        "10pct": round(np.percentile(worst, 10) * 100, 2),
        "mean": round(worst.mean() * 100, 2),
        "median": round(np.median(worst) * 100, 2),
    }

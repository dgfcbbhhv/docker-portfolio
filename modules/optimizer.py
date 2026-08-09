"""组合优化 + Monte Carlo 置信区间
import logging

logger = logging.getLogger(__name__)
三种目标: 最大夏普 / 最小波动率 / 风险平价
约束: 单只上限30%, 最少持有3只

假设:
- 基于历史协方差矩阵, 极端行情下可能失效
- 收益率假设为正态分布, 实际存在肥尾
"""
import numpy as np
np.random.seed(42)  # deterministic results
import pandas as pd
from scipy.optimize import minimize


def calc_cov(returns):
    """年化协方差矩阵"""
    return returns.cov() * 252


def port_ret(w, mu):
    return np.dot(w, mu)


def port_vol(w, S):
    return np.sqrt(np.dot(w.T, np.dot(S, w)))


def max_sharpe(mu, S, rf=0.02):
    n = len(mu)
    result = minimize(
        lambda w: -(port_ret(w, mu) - rf) / max(port_vol(w, S), 1e-8),
        x0=np.ones(n) / n,
        method="SLSQP",
        bounds=[(0, 0.3)] * n,
        constraints={"type": "eq", "fun": lambda w: np.sum(w) - 1},
    )
    return result.x


def min_vol(mu, S):
    n = len(mu)
    result = minimize(
        lambda w: port_vol(w, S),
        x0=np.ones(n) / n,
        method="SLSQP",
        bounds=[(0, 0.3)] * n,
        constraints={"type": "eq", "fun": lambda w: np.sum(w) - 1},
    )
    return result.x


def risk_parity(S):
    n = S.shape[0]
    target = 1.0 / n

    def rc(w):
        vol = port_vol(w, S)
        if vol <= 0:
            return 1e10
        marginal = np.dot(S, w)
        contrib = w * marginal / vol
        return np.sum((contrib - target) ** 2)

    result = minimize(
        rc,
        x0=np.ones(n) / n,
        method="SLSQP",
        bounds=[(0, 0.3)] * n,
        constraints={"type": "eq", "fun": lambda w: np.sum(w) - 1},
    )
    return result.x


def monte_carlo_confidence(mu, S, codes, method="max_sharpe", rf=0.02, n_sims=1000):
    """Monte Carlo 模拟生成权重分布, 用于置信区间"""
    n = len(codes)
    mc_results = []

    for _ in range(n_sims):
        noise = np.random.normal(0, 0.02, n)
        sim_mu = mu + noise
        sim_S = S + np.random.normal(0, 0.001, (n, n))
        sim_S = (sim_S + sim_S.T) / 2

        try:
            if method == "max_sharpe":
                w = max_sharpe(sim_mu, sim_S, rf)
            elif method == "min_vol":
                w = min_vol(sim_mu, sim_S)
            else:
                w = risk_parity(sim_S)
            mc_results.append(w)
        except Exception:
            continue

    if not mc_results:
        return {}

    mc = np.array(mc_results)
    ci = {}
    for i, c in enumerate(codes):
        if mc.shape[1] > i:
            vals = mc[:, i] * 100
            ci[c] = (round(np.percentile(vals, 2.5), 1), round(np.percentile(vals, 97.5), 1))
    return ci


def optimize(returns, codes, method="max_sharpe", rf=0.02, n_sims=1000, factor_ir=None):
    """
    主优化函数

    返回 dict:
        optimal_weights: dict {code: weight_pct}
        efficient_frontier: list of (vol%, ret%) tuples
        confidence: dict {code: (low_95, high_95)}
    """
    if returns.empty or len(codes) < 2:
        return {"optimal_weights": {}, "efficient_frontier": [], "confidence": {}}

    mu = returns.mean().values * 252
    S = calc_cov(returns)

    # 因子动量倾斜
    if factor_ir is not None:
        ir_values = np.array([factor_ir.get(c, 0.0) for c in codes])
        ir_tilt = 1.0 + np.clip(ir_values, -0.5, 0.5) * 0.15
        if len(ir_tilt) == len(mu):
            mu = mu * ir_tilt
            mu_mean = (returns.mean().values * 252).mean()
            if mu_mean > 1e-10:
                mu = mu * mu_mean / mu.mean()

    # 最优权重

    if method == "max_sharpe":
        w = max_sharpe(mu, S, rf)
    elif method == "min_vol":
        w = min_vol(mu, S)
    else:
        w = risk_parity(S)

    optimal = {codes[i]: round(w[i] * 100, 1) for i in range(len(codes)) if w[i] > 0.001}

    # 有效前沿
    frontier = []
    for target_ret in np.linspace(mu.min(), mu.max(), 20):
        n = len(codes)
        result = minimize(
            lambda w: port_vol(w, S),
            x0=np.ones(n) / n,
            method="SLSQP",
            bounds=[(0, 0.3)] * n,
            constraints=[
                {"type": "eq", "fun": lambda w: np.sum(w) - 1},
                {"type": "eq", "fun": lambda w: port_ret(w, mu) - target_ret},
            ],
        )
        if result.success:
            vol = port_vol(result.x, S)
            frontier.append((round(vol * 100, 2), round(target_ret * 100, 2)))

    # Monte Carlo 置信区间
    confidence = monte_carlo_confidence(mu, S, codes, method, rf, n_sims)

    return {
        "optimal_weights": optimal,
        "efficient_frontier": frontier,
        "confidence": confidence,
    }

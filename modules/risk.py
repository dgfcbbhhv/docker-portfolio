"""组合风险基础计算（共享深模块）。

单日跌幅与行业全灭只依赖目标权重，属确定性计算；
蒙特卡洛回撤依赖真实波动率，港美股/REIT 占位波动率视为数据不足。

stress（黑天鹅压力）与 layer_risk（分层回撤验收）共用本模块的基础原语，
避免两套同名实现漂移。
"""
import numpy as np
np.random.seed(42)  # deterministic results

INDUSTRY = {
    "600900": "电力", "601398": "银行", "600036": "银行", "600941": "电信",
    "601088": "能源", "600938": "能源", "601318": "综合", "600519": "综合",
    "00002": "电力", "02638": "能源", "01398": "银行", "00003": "能源",
    "02388": "银行", "00005": "银行", "00700": "科技", "00006": "电力",
    "508098": "REIT", "508068": "REIT",
    "AAPL": "科技", "NVDA": "科技", "MSFT": "科技",
}

CORR_BASE = 0.75
HORIZON_DAYS = 252
N_SIMS = 1000
PLACEHOLDER_VOL = 20.0
PLACEHOLDER_LAYERS = ("HK", "US", "REIT")


def industry_of(code):
    return INDUSTRY.get(str(code), "其他")


def single_day_loss(rows, drop_pct=-10.0):
    """单日跌幅损失（按目标权重，百分比）。rows: [{weight}, ...]"""
    if not rows:
        return 0.0
    return round(sum(r["weight"] * drop_pct / 100.0 for r in rows), 2)


def industry_collapse_loss(rows):
    """最坏单行业全灭损失（权重最大行业取负）。rows: [{weight, industry}, ...]"""
    if not rows:
        return 0.0
    by_industry = {}
    for r in rows:
        ind = r.get("industry") or industry_of(r.get("code"))
        by_industry[ind] = by_industry.get(ind, 0.0) + r["weight"]
    return round(-max(by_industry.values()), 2) if by_industry else 0.0


def vol_is_usable(vol, layer):
    """占位波动率（如 20%）不是真实数据，不得用于蒙特卡洛回撤。"""
    if vol <= 0:
        return False
    if layer in PLACEHOLDER_LAYERS and abs(vol - PLACEHOLDER_VOL) < 0.01:
        return False
    return True


def monte_carlo_p95_drawdown(rows, n_sims=N_SIMS, horizon=HORIZON_DAYS,
                             corr=CORR_BASE, seed=42):
    """蒙特卡洛回撤深度（p95）。任一持仓波动率不可用则返回 None。

    rows: [{weight, vol, _layer}, ...]，weight/vol 单位为百分数。
    """
    if not rows or any(not vol_is_usable(r["vol"], r.get("_layer", "A"))
                       for r in rows):
        return None
    n = len(rows)
    weights = np.array([r["weight"] for r in rows]) / 100.0
    vols = np.array([r["vol"] for r in rows]) / 100.0
    cov = np.full((n, n), corr)
    np.fill_diagonal(cov, 1.0)
    cov = cov * np.outer(vols, vols)
    rng = np.random.default_rng(seed)
    daily = rng.multivariate_normal(np.zeros(n), cov, size=horizon)
    port = daily @ weights
    port = np.clip(port, -0.99, 1.0)
    cum = np.cumprod(1 + port, axis=0)
    peak = np.maximum.accumulate(cum, axis=0)
    drawdown = (cum - peak) / peak
    worst = np.min(drawdown, axis=0)
    return round(-np.percentile(worst, 5) * 100, 1)

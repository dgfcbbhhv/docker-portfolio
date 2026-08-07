"""因子智能选择模块
功能: 根据股票代码自动计算各因子有效性(IC/IR), 结合行业先验推荐 top-3

设计:
  Step 1 — 行业先验: 按股票行业预设因子偏好 (作为 IR 平局时的兜底)
  Step 2 — 时序 IC/IR: 对每个 alpha 计算滚动秩相关
  Step 3 — 综合排序: IR × 置信度权重, 输出推荐

置信度分层:
  high (Tier 1 核心红利)    → 因子推荐直接融入买入评分
  medium (Tier 2 扩展红利)  → 因子推荐仅供展示 + 可选融入
  low (Tier 3 科技观察)     → 因子分析照跑, 但标注低置信度标签
"""
import numpy as np
from scipy import stats
from .factors import (
    FactorCalculator, FACTOR_DESCRIPTIONS, get_factor_descriptions
)


# ── 行业→因子偏好映射 (先验权重) ──
# 当 IC/IR 接近时用此作为兜底
INDUSTRY_FACTOR_PRIOR = {
    "银行":  {"factors": ["alpha010", "alpha101", "alpha001"], "type": "质量"},
    "电力":  {"factors": ["alpha030", "alpha078", "alpha001"], "type": "波动率"},
    "能源":  {"factors": ["alpha012", "alpha023", "alpha030"], "type": "动量"},
    "电信":  {"factors": ["alpha001", "alpha030", "alpha010"], "type": "动量"},
    "综合":  {"factors": ["alpha001", "alpha030", "alpha010"], "type": "综合"},
    "REIT":  {"factors": ["alpha010", "alpha101", "alpha001"], "type": "质量"},
    "科技":  {"factors": ["alpha001", "alpha012", "alpha023", "alpha054"], "type": "动量"},
    "默认":  {"factors": ["alpha001", "alpha030", "alpha010"], "type": "混合"},
}

# ── 股票→行业映射 ──
STOCK_INDUSTRY = {
    # A 股核心
    "600900": "电力", "601398": "银行", "600036": "银行",
    "600941": "电信", "601088": "能源", "600938": "能源",
    "601318": "综合", "600519": "综合",
    # 四大行
    "601288": "银行", "601988": "银行", "601939": "银行",
    # 港股核心
    "00002": "电力", "02638": "能源", "01398": "银行",
    "00003": "能源", "02388": "银行", "00005": "银行",
    # REITs
    "508098": "REIT", "508068": "REIT",
    # 港股扩展
    "00700": "科技",
    # 美股
    "AAPL": "科技", "NVDA": "科技", "MSFT": "科技",
}

# ── 置信度分层 ──
STOCK_CONFIDENCE = {
    # Tier 1: 核心红利 (高置信度)
    "600900": "high", "601398": "high", "600036": "high",
    "600941": "high", "601088": "high", "600938": "high",
    "601318": "high", "600519": "high",
    "601288": "high", "601988": "high", "601939": "high",
    "00002": "high", "02638": "high", "01398": "high",
    "00003": "high", "02388": "high", "00005": "high",
    # Tier 2: 扩展红利 (中等置信度)
    "508098": "medium", "508068": "medium",
    "AAPL": "medium",
    # Tier 3: 科技观察 (低置信度)
    "00700": "low", "NVDA": "low", "MSFT": "low",
}

STOCK_TIER = {
    code: {"high": "core", "medium": "extended", "low": "tech"}[v]
    for code, v in STOCK_CONFIDENCE.items()
}


def get_confidence_score(confidence):
    """置信度→数值权重"""
    return {"high": 1.0, "medium": 0.7, "low": 0.4}.get(confidence, 0.3)


def get_industry(code):
    """获取股票行业, 未知则返回 '默认'"""
    return STOCK_INDUSTRY.get(code, "默认")


def compute_rolling_ic(factor_values, forward_returns, window=60):
    """计算滚动 Rank IC

    在每个时间步上计算过去 window 日内
    因子值与 N 日后收益率的秩相关

    Returns:
        (ic_series, ic_mean, ic_std, ir, positive_ratio)
    """
    n = min(len(factor_values), len(forward_returns))
    valid = ~(np.isnan(factor_values) | np.isnan(forward_returns))
    fv = factor_values[valid]
    fr = forward_returns[valid]

    if len(fv) < max(window, 40):
        return np.array([]), 0.0, 0.0, 0.0, 0.0

    window = min(window, len(fv) // 2)
    ics = []
    for i in range(window, len(fv)):
        f_slice = fv[i - window:i]
        r_slice = fr[i - window:i]
        # 检查数据质量
        if np.std(f_slice) < 1e-10 or np.std(r_slice) < 1e-10:
            continue
        try:
            ic, _ = stats.spearmanr(f_slice, r_slice)
            if not np.isnan(ic):
                ics.append(ic)
        except Exception:
            continue

    ic_arr = np.array(ics)
    if len(ic_arr) < 5:
        return ic_arr, 0.0, 0.0, 0.0, 0.0

    ic_mean = float(np.mean(ic_arr))
    ic_std = float(np.std(ic_arr))
    ir = float(ic_mean / ic_std) if ic_std > 1e-10 else 0.0
    pos_ratio = float(np.mean(np.abs(ic_arr) > 0.03))  # IC 绝对值>0.03 的比例
    return ic_arr, ic_mean, ic_std, ir, pos_ratio


def evaluate_factors_for_stock(df, forward_days=5):
    """对输入股票 DataFrame 计算全部因子 IC/IR

    Args:
        df: 含 date/close/open/high/low/volume 的 DataFrame
        forward_days: 远期收益窗口 (默认 5 个交易日)

    Returns:
        dict: {alpha_name: {ic_mean, ic_std, ir, positive_ratio, n_samples}, ...}
        或 None (数据不足)
    """
    if df is None or df.empty or len(df) < 60:
        return None

    close = np.asarray(df["close"].values, dtype=float)

    # 计算前向收益
    forward_returns = np.full(len(close), np.nan, dtype=float)
    for i in range(len(close) - forward_days):
        forward_returns[i] = (close[i + forward_days] / close[i]) - 1.0

    # 计算全部因子值
    calc = FactorCalculator(df)
    all_factors = calc.calculate_all()

    results = {}
    for fname, fvalues in all_factors.items():
        if fvalues is None:
            continue
        fv = np.asarray(fvalues, dtype=float)
        # 对齐因子值和收益
        min_len = min(len(fv), len(forward_returns))
        fv = fv[:min_len]
        fr = forward_returns[:min_len]

        _, ic_mean, ic_std, ir, pos_ratio = compute_rolling_ic(fv, fr)

        if ic_std > 1e-10 and len(fv) >= 40:
            results[fname] = {
                "ic_mean": round(ic_mean, 6),
                "ic_std": round(ic_std, 6),
                "ir": round(ir, 4),
                "positive_ratio": round(pos_ratio, 4),
                "n_samples": len(fv),
            }

    return results if results else None


def recommend_factors(code, hist_df, confidence=None):
    """核心入口: 对目标股票推荐最佳因子

    Args:
        code: 股票代码
        hist_df: 历史行情 DataFrame
        confidence: 可选, 覆盖 STOCK_CONFIDENCE 自动判断

    Returns:
        dict: {
            "code": str,
            "industry": str,
            "tier": str,
            "confidence": str,
            "recommended": [{
                "name": "alpha030",
                "type": "波动率",
                "ir": 0.82,
                "ic_mean": 0.05,
                "positive_ratio": 0.65,
            }, ...],
            "all_results": {...},  # 全部因子 IC 明细
            "confidence_score": float,
        }
        或 None (数据不足)
    """
    if hist_df is None or hist_df.empty:
        return None

    # 确定行业和置信度
    industry = get_industry(code)
    if confidence is None:
        confidence = STOCK_CONFIDENCE.get(code, "low")
    conf_score = get_confidence_score(confidence)
    tier = STOCK_TIER.get(code, "tech")

    # 计算全部因子 IC/IR
    ic_results = evaluate_factors_for_stock(hist_df)
    if ic_results is None or len(ic_results) < 2:
        return None

    # 获取行业先验
    industry_prior = INDUSTRY_FACTOR_PRIOR.get(industry, INDUSTRY_FACTOR_PRIOR["默认"])
    prior_factors = industry_prior["factors"]

    # 排序: 先按 IR 降序, 行业先验因子同等 IR 时优先
    def sort_key(item):
        name = item[0]
        data = item[1]
        ir = abs(data["ir"])
        prior_bonus = 0.2 if name in prior_factors else 0.0
        return -(ir * conf_score + prior_bonus)

    sorted_factors = sorted(ic_results.items(), key=sort_key)

    # 构建推荐 (top-3 或 IR>0.3)
    recommended = []
    for name, data in sorted_factors:
        desc = FACTOR_DESCRIPTIONS.get(name, {})
        recommended.append({
            "name": name,
            "type": desc.get("type", "未知"),
            "name_cn": desc.get("name", name),
            "ir": data["ir"],
            "ic_mean": data["ic_mean"],
            "positive_ratio": data["positive_ratio"],
            "n_samples": data["n_samples"],
        })
        if len(recommended) >= 3:
            break

    return {
        "code": code,
        "industry": industry,
        "tier": tier,
        "confidence": confidence,
        "confidence_score": round(conf_score, 2),
        "recommended": recommended,
        "all_results": ic_results,
    }


def get_factor_dynamic_bonus(code, hist_df):
    """计算因子动态加分: 返回 0-10 的分数, 用于 signals.py 融合

    Tier 1 (high):   推荐因子点亮可加 0-10 分
    Tier 2 (medium): 推荐因子点亮可加 0-5 分
    Tier 3 (low):    不加分

    加分依据: 推荐因子当下是否≥历史均值方向
    """
    result = recommend_factors(code, hist_df)
    if result is None:
        return 0, []

    if result["confidence"] == "low":
        return 0, result["recommended"]

    # 推荐因子的平均 IR 作为加分依据
    if not result["recommended"]:
        return 0, []

    avg_ir = sum(abs(f["ir"]) for f in result["recommended"]) / len(result["recommended"])

    if result["confidence"] == "high":
        bonus = min(10, int(avg_ir * 10))
    else:
        bonus = min(5, int(avg_ir * 5))

    return max(0, bonus), result["recommended"]

def get_kelly_multiplier(code, hist_df=None):
    """计算因子 IR 对应的 Kelly 仓位乘数

    用 learner 的持久化模型 (优先) 或实时计算 hist_df (后备)
    返回 0.5 ~ 1.5 的乘数

    Tier 1 (core):    1.0 + avg_IR * 0.5,  [0.7, 1.5]
    Tier 2 (extended): 1.0 + avg_IR * 0.3,  [0.7, 1.2]
    Tier 3 (tech):     0.5 (固定减半)
    """
    from .learner import get_learner
    learner = get_learner()
    result = learner.get_learned_recommendation(code, hist_df)
    if not result or not result.get("recommended"):
        return 1.0

    confidence = result.get("confidence", "low")
    recs = result["recommended"]
    avg_ir = sum(abs(r["ir"]) for r in recs) / len(recs) if recs else 0.0

    if confidence == "high":
        return max(0.7, min(1.5, 1.0 + avg_ir * 0.5))
    elif confidence == "medium":
        return max(0.7, min(1.2, 1.0 + avg_ir * 0.3))
    else:
        return 0.5


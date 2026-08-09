"""因子在线学习引擎: 训练 → 持久化 → 衰减更新 → 进化检测
import logging

logger = logging.getLogger(__name__)
训练:     全量历史数据计算 IC/IR, 持久化到 data/factor_models/{code}.json
自学习:   新数据到达时衰减加权更新 (新权重 0.3, 旧权重 0.7)
自进化:   滑动窗口追踪各因子 IC 趋势, 判断提升/衰退/稳定
"""
import datetime
import json
import os

import numpy as np
from scipy import stats

MODEL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "factor_models"
)


class FactorLearner:
    """因子在线学习引擎"""

    def __init__(self):
        os.makedirs(MODEL_DIR, exist_ok=True)
        self._cache = {}

    def _model_path(self, code):
        return os.path.join(MODEL_DIR, f"{code}.json")

    # ── 训练 ──

    def train(self, code, hist_df):
        """全量训练: 计算 IC/IR 并持久化"""
        from .factor_selector import evaluate_factors_for_stock
        ic_results = evaluate_factors_for_stock(hist_df)
        if not ic_results:
            return None
        model = {
            "code": code,
            "trained_at": datetime.datetime.now().isoformat(),
            "n_days": len(hist_df),
            "factors": ic_results,
        }
        with open(self._model_path(code), "w", encoding="utf-8") as f:
            json.dump(model, f, indent=2, ensure_ascii=False)
        self._cache[code] = model
        return model

    def load(self, code):
        """加载已训练模型"""
        if code in self._cache:
            return self._cache[code]
        path = self._model_path(code)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                model = json.load(f)
            self._cache[code] = model
            return model
        return None

    def is_trained(self, code):
        return self.load(code) is not None

    def get_training_info(self, code):
        """返回训练状态摘要"""
        model = self.load(code)
        if not model:
            return {"status": "untrained"}
        return {
            "status": "trained",
            "trained_at": model.get("trained_at", "unknown"),
            "n_days": model.get("n_days", 0),
            "n_factors": len(model.get("factors", {})),
        }

    # ── 自学习 ──

    def update(self, code, hist_df):
        """在线学习: 衰减加权增量更新

        新数据到达时按权重 0.3 合并到已有模型
        """
        from .factor_selector import evaluate_factors_for_stock
        existing = self.load(code)
        new_results = evaluate_factors_for_stock(hist_df)
        if not new_results:
            return existing

        if existing:
            model = existing
            decay = 0.3
            for fname, fdata in new_results.items():
                if fname in model["factors"]:
                    old = model["factors"][fname]
                    model["factors"][fname] = {
                        "ic_mean": round(old["ic_mean"] * (1 - decay) + fdata["ic_mean"] * decay, 6),
                        "ic_std": round(old["ic_std"] * (1 - decay) + fdata["ic_std"] * decay, 6),
                        "ir": round(old["ir"] * (1 - decay) + fdata["ir"] * decay, 4),
                        "positive_ratio": round(old["positive_ratio"] * (1 - decay)
                                                + fdata["positive_ratio"] * decay, 4),
                        "n_samples": old["n_samples"] + fdata["n_samples"],
                    }
                else:
                    model["factors"][fname] = fdata
            model["trained_at"] = datetime.datetime.now().isoformat()
            model["n_days"] = len(hist_df)
        else:
            model = {
                "code": code,
                "trained_at": datetime.datetime.now().isoformat(),
                "n_days": len(hist_df),
                "factors": new_results,
            }

        with open(self._model_path(code), "w", encoding="utf-8") as f:
            json.dump(model, f, indent=2, ensure_ascii=False)
        self._cache[code] = model
        return model

    def get_learned_recommendation(self, code, hist_df=None):
        """获取自学习后的因子推荐

        优先使用持久化模型, 若无则从 hist_df 实时计算
        """
        model = self.load(code)
        if model:
            factors = model.get("factors", {})
            if factors:
                # 按 IR 排序返回 top-3
                from .factor_selector import (
                    FACTOR_DESCRIPTIONS,
                    STOCK_CONFIDENCE,
                    STOCK_TIER,
                    get_industry,
                )
                sorted_f = sorted(factors.items(), key=lambda x: -abs(x[1].get("ir", 0)))
                recommended = []
                for name, data in sorted_f:
                    desc = FACTOR_DESCRIPTIONS.get(name, {})
                    recommended.append({
                        "name": name,
                        "name_cn": desc.get("name", name),
                        "type": desc.get("type", "未知"),
                        "ir": data["ir"],
                        "ic_mean": data["ic_mean"],
                        "positive_ratio": data["positive_ratio"],
                        "n_samples": data.get("n_samples", 0),
                    })
                    if len(recommended) >= 3:
                        break
                return {
                    "code": code,
                    "industry": get_industry(code),
                    "tier": STOCK_TIER.get(code, "tech"),
                    "confidence": STOCK_CONFIDENCE.get(code, "low"),
                    "confidence_score": {"high": 1.0, "medium": 0.7, "low": 0.4}.get(
                        STOCK_CONFIDENCE.get(code, "low"), 0.3),
                    "recommended": recommended,
                    "all_results": factors,
                    "trained_at": model.get("trained_at", ""),
                }
        # 后备: 实时计算
        if hist_df is not None:
            from .factor_selector import recommend_factors
            return recommend_factors(code, hist_df)
        return None

    # ── 自进化 ──

    def compute_evolution(self, code, hist_df):
        """计算因子有效性进化轨迹

        将历史数据切分为滑动窗口 (每 30 天一次),
        每个窗口计算单期 IC, 形成 IC 趋势序列

        Returns:
            {factor_name: {
                "history": [{"date": str, "ic": float}, ...],
                "mean_ic": float,
                "trend": "improving" | "declining" | "stable",
                "latest_ic": float
            }, ...}
            或 None (数据不足)
        """
        if hist_df is None or len(hist_df) < 120:
            return None

        from .factors import FactorCalculator
        close = np.asarray(hist_df["close"].values, dtype=float)
        dates = hist_df["date"].values

        calc = FactorCalculator(hist_df)
        all_factors = calc.calculate_all()

        window_size = 60
        step = 30
        evolution = {}

        for fname, fv_list in all_factors.items():
            if fv_list is None:
                continue
            fv = np.asarray(fv_list, dtype=float)
            factor_history = []

            for i in range(window_size, min(len(fv), len(close)), step):
                f_slice = fv[i - window_size:i]
                c_slice = close[i - window_size:i]
                # 前向收益 (5 日)
                fwd_returns = np.full(len(c_slice), np.nan)
                for j in range(len(c_slice) - 5):
                    fwd_returns[j] = (c_slice[j + 5] / c_slice[j]) - 1.0
                valid = ~(np.isnan(f_slice) | np.isnan(fwd_returns))
                f_clean = f_slice[valid]
                r_clean = fwd_returns[valid]
                if len(f_clean) < 30 or np.std(f_clean) < 1e-10:
                    continue
                try:
                    ic_val, _ = stats.spearmanr(f_clean, r_clean)
                    if not np.isnan(ic_val):
                        factor_history.append({
                            "date": str(dates[min(i, len(dates) - 1)]),
                            "ic": round(float(ic_val), 4),
                        })
                except Exception:
                    continue

            if len(factor_history) >= 3:
                ics = np.array([h["ic"] for h in factor_history])
                # 趋势判定: 比较前 1/3 均值 vs 后 1/3 均值
                n = len(ics)
                early = np.mean(ics[:n // 3]) if n >= 6 else ics[0]
                late = np.mean(ics[-n // 3:]) if n >= 6 else ics[-1]
                diff = late - early
                evolution[fname] = {
                    "history": factor_history,
                    "mean_ic": float(np.mean(ics)),
                    "trend": "improving" if diff > 0.05
                    else "declining" if diff < -0.05
                    else "stable",
                    "latest_ic": factor_history[-1]["ic"],
                    "trend_strength": round(float(diff), 4),
                }

        return evolution if evolution else None

    def get_evolution_summary(self, evolution):
        """生成进化摘要"""
        if not evolution:
            return None
        improving = [n for n, d in evolution.items() if d.get("trend") == "improving"]
        declining = [n for n, d in evolution.items() if d.get("trend") == "declining"]
        stable = [n for n, d in evolution.items() if d.get("trend") == "stable"]
        best = max(evolution.items(), key=lambda x: x[1]["mean_ic"]) if evolution else None
        return {
            "total": len(evolution),
            "improving": improving,
            "declining": declining,
            "stable": stable,
            "best_factor": best[0] if best else None,
        }


# 全局单例
_learner = None


def get_learner():
    global _learner
    if _learner is None:
        _learner = FactorLearner()
    return _learner

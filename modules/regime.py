"""市场诊断层 — 判断红利板块整体状态, 影响个股阈值"""
import logging
import numpy as np
import datetime

logger = logging.getLogger(__name__)


def fetch_macro_signals():
    """获取宏观信号: 中美利差 + VIX

    数据来源:
      - 国债收益率: akshare bond_zh_us_rate
      - VIX: yfinance (^VIX)

    Returns:
        dict: {
            "china_10y": float | None,
            "us_10y": float | None,
            "china_us_spread": float | None,
            "vix": float | None,
            "updated_at": str,
        }
        或 {"error": str} (全部失败时)
    """
    result = {"updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
    has_data = False

    # ── 中美国债收益率 ──
    try:
        import akshare as ak
        df = ak.bond_zh_us_rate()
        if df is not None and not df.empty:
            last = df.iloc[-1]
            result["china_10y"] = round(float(last.iloc[3]), 3)  # 中国10Y
            result["us_10y"] = round(float(last.iloc[8]), 3)     # 美国10Y
            result["china_us_spread"] = round(result["china_10y"] - result["us_10y"], 3)
            has_data = True
    except Exception as _e:
        logger.warning(f"[fetch_macro_signals] %s", _e, exc_info=True)

    # ── VIX ──
    try:
        import yfinance as yf
        vix = yf.Ticker("^VIX")
        vix_hist = vix.history(period="5d")
        if vix_hist is not None and not vix_hist.empty:
            result["vix"] = round(float(vix_hist["Close"].iloc[-1]), 1)
            has_data = True
    except Exception as _e:
        logger.warning(f"[fetch_macro_signals] %s", _e, exc_info=True)

    return result if has_data else {"error": "无法获取宏观数据"}


def detect_regime(pe_pctls, pb_pctls, macro_signals=None):
    """判断市场状态

    pe_pctls: dict {code: pe_percentile} — 组合内各股 PE 分位
    pb_pctls: dict {code: pb_percentile} — 组合内各股 PB 分位
    macro_signals: dict | None — 来自 fetch_macro_signals() 的宏观信号

    返回: {
        "regime": str,
        "label": str,
        "pe_median": float,
        "pb_median": float,
        "threshold_adjust": int,
        "risk_warning": str | None,
        "macro": dict | None,
    }
    """
    pe_vals = [v for v in pe_pctls.values() if v is not None]
    pb_vals = [v for v in pb_pctls.values() if v is not None]

    pe_median = float(np.median(pe_vals)) if pe_vals else 50.0
    pb_median = float(np.median(pb_vals)) if pb_vals else 50.0

    # 综合估值分位 (PE + PB 加权)
    composite = pe_median * 0.6 + pb_median * 0.4

    if composite < 30:
        return {
            "regime": "undervalued",
            "label": "🟢 低估区间",
            "pe_median": round(pe_median, 1),
            "pb_median": round(pb_median, 1),
            "threshold_adjust": -5,      # 买入阈值降低 5 分
            "risk_warning": None,
        }
    elif composite < 70:
        return {
            "regime": "neutral",
            "label": "🟡 中性区间",
            "pe_median": round(pe_median, 1),
            "pb_median": round(pb_median, 1),
            "threshold_adjust": 0,
            "risk_warning": None,
        }
    else:
        warnings = []
        if pe_median > 80:
            warnings.append("PE中位数超过80%, 板块可能过热")
        if pb_median > 80:
            warnings.append("PB中位数超过80%, 注意估值泡沫")

        return {
            "regime": "overvalued",
            "label": "🔴 高估区间",
            "pe_median": round(pe_median, 1),
            "pb_median": round(pb_median, 1),
            "threshold_adjust": +5,      # 买入阈值提高 5 分
            "risk_warning": "; ".join(warnings) if warnings else "整体估值偏高, 谨慎加仓",
        }



def get_factor_regime(codes):
    try:
        from .learner import get_learner
        learner = get_learner()
        type_irs = {}
        for c in codes:
            model = learner.load(c)
            if not model:
                continue
            factors = model.get("factors", {})
            for fname, fdata in factors.items():
                from .factors import FACTOR_DESCRIPTIONS
                desc = FACTOR_DESCRIPTIONS.get(fname, {})
                ftype = desc.get("type", "unknown")
                ir = fdata.get("ir", 0)
                if ftype not in type_irs:
                    type_irs[ftype] = []
                type_irs[ftype].append(ir)
        if not type_irs:
            return "neutral", {}
        type_avg = {k: (sum(v)/len(v), len(v)) for k, v in type_irs.items()}
        best_type = max(type_avg, key=lambda x: type_avg[x][0])
        worst_type = min(type_avg, key=lambda x: type_avg[x][0])
        details = {
            "factor_regime": best_type,
            "regime_score": round(type_avg[best_type][0], 4),
            "worst_type": worst_type,
            "worst_score": round(type_avg[worst_type][0], 4),
            "all_types": {k: round(v[0], 4) for k, v in type_avg.items()},
        }
        if type_avg[best_type][0] > 0.03:
            return "momentum_active", details
        elif type_avg[best_type][0] > 0.01:
            return "mixed", details
        else:
            return "factor_weak", details
    except Exception as e:
        return "neutral", {"error": str(e)}

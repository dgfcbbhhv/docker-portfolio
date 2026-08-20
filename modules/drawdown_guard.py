"""组合级回撤熔断 — 补上"单笔风险预算管不了组合受伤"的缺口。

思路
----
- 记录真实持仓每日总市值快照（data/portfolio_drawdown.json），无快照则当日初始化。
- 回撤 = (当前总市值 − 历史峰值) / 历史峰值 × 100。
- 回撤 ≥ 熔断阈值（各层 layers.max_drawdown 的最大值，默认 10%）→ 熔断：
  停止一切建仓/加仓建议，仅保留卖出/止损提示。
- 熔断与"健康度偏热"不同：偏热是情绪面暂停，回撤熔断是组合受伤的硬闸门。

接口
----
- record_snapshot(value, date=None): 记录当日总市值（幂等，同日覆盖）。
- current_drawdown(): 读取当前回撤与熔断状态（不写文件）。
- is_frozen(dd=None): 是否处于熔断。
"""
import datetime
import json
import os

from modules.paths import data_dir

DRAWDOWN_FILE = os.path.join(data_dir(), "portfolio_drawdown.json")


def _load():
    if not os.path.exists(DRAWDOWN_FILE):
        return {}
    try:
        with open(DRAWDOWN_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data):
    os.makedirs(os.path.dirname(DRAWDOWN_FILE), exist_ok=True)
    tmp = DRAWDOWN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DRAWDOWN_FILE)


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def threshold():
    """组合级熔断阈值 = 各层 max_drawdown 最大值（默认 10%）。"""
    from modules.config import LAYERS
    vals = []
    for cfg in (LAYERS or {}).values():
        if cfg.get("enabled", True):
            vals.append(_f(cfg.get("max_drawdown"), 10.0))
    return max(vals) if vals else 10.0


def record_snapshot(value, date=None):
    """记录当日总市值快照（幂等）。

    Returns: {"recorded": bool, "peak": float, "drawdown_pct": float,
              "frozen": bool, "threshold": float}
    """
    value = _f(value)
    if value <= 0:
        return {"recorded": False, "peak": None, "drawdown_pct": None,
                "frozen": False, "threshold": threshold()}
    date = date or datetime.date.today().isoformat()
    data = _load()
    dates = data.get("dates", [])
    values = data.get("values", [])
    peak = _f(data.get("peak", 0))
    if not dates or dates[-1] != date:
        dates.append(date)
        values.append(round(value, 2))
    else:
        values[-1] = round(value, 2)
    peak = max(peak, value)
    dd = (peak - value) / peak * 100.0 if peak > 0 else 0.0
    th = threshold()
    data.update({"dates": dates, "values": values, "peak": round(peak, 2),
                 "last_value": round(value, 2), "last_date": date,
                 "drawdown_pct": round(dd, 2), "threshold": th,
                 "last_updated": datetime.datetime.now().isoformat()})
    _save(data)
    return {"recorded": True, "peak": round(peak, 2), "drawdown_pct": round(dd, 2),
            "frozen": dd >= th, "threshold": th}


def current_drawdown():
    """读取当前回撤状态（不写文件）。"""
    data = _load()
    if not data or not data.get("values"):
        return {"has_history": False, "peak": None, "drawdown_pct": None,
                "frozen": False, "threshold": threshold(), "dates": [], "values": []}
    return {"has_history": True, "peak": _f(data.get("peak")),
            "drawdown_pct": _f(data.get("drawdown_pct")),
            "frozen": _f(data.get("drawdown_pct", 0)) >= _f(data.get("threshold", threshold())),
            "threshold": _f(data.get("threshold", threshold())),
            "dates": data.get("dates", []), "values": data.get("values", [])}


def is_frozen():
    return current_drawdown().get("frozen", False)


def reset():
    """清空回撤历史（如资产规模变更后）。"""
    if os.path.exists(DRAWDOWN_FILE):
        os.remove(DRAWDOWN_FILE)
    return True
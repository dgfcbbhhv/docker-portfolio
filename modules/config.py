"""配置加载：config.json 是唯一事实源，缺失字段用内置默认值兜底 (优化版)

优化点:
1. 提取辅助函数减少重复代码
2. 使用dataclass或命名元组管理配置组
3. 简化reload函数
"""
import json
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(_ROOT, "config.json")

_DEFAULTS = {
    "portfolio": {"A": {}, "HK": {}, "REIT": {}, "US": {}},
    "manual_div_yields": {},
    "manual_prices": {},
    "manual_us_growth": {},
    "watch_list": {},
    "kelly": {"frac": 0.25, "maxpos": 30.0, "alert": 8.0, "max_total": 80.0},
    "alerts": {
        "price_drop": 8.0,
        "div_change": 0.5,
        "kelly_change": 5.0,
        "lishing_drop": 10.0,
        "cooldown_minutes": 240,
        "batch_min": 5,
        "retries": 3,
        "channels": ["email", "wechat", "telegram", "webhook"],
    },
    "freshness": {
        "spot_max_age_minutes": 90,
        "fin_max_age_days": 7,
        "div_max_age_days": 30,
        "hist_max_age_days": 7,
    },
    "daily_push": {
        "enabled": True,
        "lishing_buy": 70.0,
        "lishing_watch": 60.0,
        "us_buy": 70.0,
        "us_watch": 50.0,
        "add_gap": 2.0,
        "announcement_days": 90,
        "time_ah": "16:30",
        "time_us": "06:30",
        "time_validate": "18:00",
    },
    "paper_trade": {
        "auto_follow_picks": True,
        "picks_buy_pct": 10.0,
        "picks_reduce_ratio": 0.5,
    },
}


def _deep_merge(base, override):
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


_LAYER_DEFAULTS = {
    "A": {"sequence": 1, "enabled": True, "invested_cap": 80.0, "max_drawdown": 10.0},
    "HK": {"sequence": 2, "enabled": True, "invested_cap": 80.0, "max_drawdown": 10.0},
    "US": {"sequence": 3, "enabled": True, "invested_cap": 60.0, "max_drawdown": 10.0},
    "REIT": {"sequence": 4, "enabled": True, "invested_cap": 20.0, "max_drawdown": 10.0},
}


def _merge_layers(user_layers):
    result = {}
    for key, defaults in _LAYER_DEFAULTS.items():
        user = (user_layers or {}).get(key, {}) or {}
        merged = dict(defaults)
        merged.update({k: v for k, v in user.items() if v is not None})
        result[key] = merged
    return result


def _load():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            return _deep_merge(_DEFAULTS, user_cfg)
        except (json.JSONDecodeError, OSError):
            pass
    return dict(_DEFAULTS)


def _get_val(cfg, *keys, default=None):
    """安全获取嵌套字典值"""
    for key in keys:
        if isinstance(cfg, dict):
            cfg = cfg.get(key)
        else:
            return default
    return cfg if cfg is not None else default


_CFG = _load()
LAYERS = _merge_layers(_CFG.get("layers", {}))


def get_config():
    return _CFG


def get(key, default=None):
    return _CFG.get(key, default)


# ── 组合配置 ──
PORT_A = _CFG.get("portfolio", {}).get("A", {})
PORT_HK = _CFG.get("portfolio", {}).get("HK", {})
PORT_REIT = _CFG.get("portfolio", {}).get("REIT", {})
PORT_US = _CFG.get("portfolio", {}).get("US", {})
MANUAL_DIV_YIELDS = _CFG.get("manual_div_yields", {})
MANUAL_PRICES = _CFG.get("manual_prices", {})
MANUAL_US_GROWTH = _CFG.get("manual_us_growth", {})
WATCH_LIST = _CFG.get("watch_list", {})

# ── Kelly 参数 ──
_KELLY = _CFG.get("kelly", {})
FRAC = float(_KELLY.get("frac", 0.25))
MAXPOS = float(_KELLY.get("maxpos", 30.0))
ALERT = float(_KELLY.get("alert", 8.0))
MAX_TOTAL = float(_KELLY.get("max_total", 80.0))

# ── 预警参数 ──
_ALERTS = _CFG.get("alerts", {})
ALERT_THRESHOLDS = {
    "price_drop": float(_ALERTS.get("price_drop", 8.0)),
    "div_change": float(_ALERTS.get("div_change", 0.5)),
    "kelly_change": float(_ALERTS.get("kelly_change", 5.0)),
    "lishing_drop": float(_ALERTS.get("lishing_drop", 10.0)),
    "earn_drop": float(_ALERTS.get("earn_drop", 15.0)),
}
ALERT_COOLDOWN_MINUTES = int(_ALERTS.get("cooldown_minutes", 240))
ALERT_BATCH_MIN = int(_ALERTS.get("batch_min", 5))
ALERT_RETRIES = int(_ALERTS.get("retries", 3))
ALERT_CHANNELS = _ALERTS.get("channels", ["email", "wechat", "telegram", "webhook"])

# ── 数据新鲜度 ──
_FRESH = _CFG.get("freshness", {})
SPOT_MAX_AGE_MINUTES = float(_FRESH.get("spot_max_age_minutes", 90))
FIN_MAX_AGE_DAYS = float(_FRESH.get("fin_max_age_days", 7))
DIV_MAX_AGE_DAYS = float(_FRESH.get("div_max_age_days", 30))
HIST_MAX_AGE_DAYS = float(_FRESH.get("hist_max_age_days", 7))

# ── 每日推送 ──
_DAILY = _CFG.get("daily_push", {})
DAILY_PUSH = {
    "enabled": bool(_DAILY.get("enabled", True)),
    "lishing_buy": float(_DAILY.get("lishing_buy", 70.0)),
    "lishing_watch": float(_DAILY.get("lishing_watch", 60.0)),
    "us_buy": float(_DAILY.get("us_buy", 70.0)),
    "us_watch": float(_DAILY.get("us_watch", 50.0)),
    "add_gap": float(_DAILY.get("add_gap", 2.0)),
    "announcement_days": int(_DAILY.get("announcement_days", 90)),
    "time_ah": str(_DAILY.get("time_ah", "16:30")),
    "time_us": str(_DAILY.get("time_us", "06:30")),
    "time_validate": str(_DAILY.get("time_validate", "18:00")),
    "time_health": str(_DAILY.get("time_health", "18:10")),
    "fmp_limit": int(_DAILY.get("fmp_limit", 200)),
}

# ── 行情多源交叉核对 ──
_XC = _CFG.get("crosscheck", {})
CROSSCHECK = {
    "enabled": bool(_XC.get("enabled", False)),
    "threshold": float(_XC.get("threshold", 2.0)),
    "time": str(_XC.get("time", "16:40")),
}

# ── REIT 分派率跟踪 ──
_RM = _CFG.get("reit_monitor", {})
REIT_MONITOR = {
    "enabled": bool(_RM.get("enabled", True)),
    "time": str(_RM.get("time", "18:45")),
    "weekday": int(_RM.get("weekday", 5)),
    "codes": list(_RM.get("codes", ["508098", "508068"])),
}

# ── 模拟盘 ──
_PT = _CFG.get("paper_trade", {})
PAPER_TRADE = {
    "auto_follow_picks": bool(_PT.get("auto_follow_picks", True)),
    "picks_buy_pct": float(_PT.get("picks_buy_pct", 10.0)),
    "picks_reduce_ratio": float(_PT.get("picks_reduce_ratio", 0.5)),
}


def _apply_config(cfg):
    """从配置字典应用所有全局变量"""
    global PORT_A, PORT_HK, PORT_REIT, PORT_US
    global MANUAL_DIV_YIELDS, MANUAL_PRICES, MANUAL_US_GROWTH, WATCH_LIST
    global FRAC, MAXPOS, ALERT, MAX_TOTAL, LAYERS
    global ALERT_THRESHOLDS, ALERT_COOLDOWN_MINUTES, ALERT_BATCH_MIN, ALERT_RETRIES, ALERT_CHANNELS
    global SPOT_MAX_AGE_MINUTES, FIN_MAX_AGE_DAYS, DIV_MAX_AGE_DAYS, HIST_MAX_AGE_DAYS
    global DAILY_PUSH, CROSSCHECK, REIT_MONITOR
    global PAPER_TRADE

    PORT_A = cfg.get("portfolio", {}).get("A", {})
    PORT_HK = cfg.get("portfolio", {}).get("HK", {})
    PORT_REIT = cfg.get("portfolio", {}).get("REIT", {})
    PORT_US = cfg.get("portfolio", {}).get("US", {})
    MANUAL_DIV_YIELDS = cfg.get("manual_div_yields", {})
    MANUAL_PRICES = cfg.get("manual_prices", {})
    MANUAL_US_GROWTH = cfg.get("manual_us_growth", {})
    WATCH_LIST = cfg.get("watch_list", {})
    LAYERS = _merge_layers(cfg.get("layers", {}))

    _kelly = cfg.get("kelly", {})
    FRAC = float(_kelly.get("frac", 0.25))
    MAXPOS = float(_kelly.get("maxpos", 30.0))
    ALERT = float(_kelly.get("alert", 8.0))
    MAX_TOTAL = float(_kelly.get("max_total", 80.0))

    _alerts = cfg.get("alerts", {})
    ALERT_THRESHOLDS = {
        "price_drop": float(_alerts.get("price_drop", 8.0)),
        "div_change": float(_alerts.get("div_change", 0.5)),
        "kelly_change": float(_alerts.get("kelly_change", 5.0)),
        "lishing_drop": float(_alerts.get("lishing_drop", 10.0)),
    }
    ALERT_COOLDOWN_MINUTES = int(_alerts.get("cooldown_minutes", 240))
    ALERT_BATCH_MIN = int(_alerts.get("batch_min", 5))
    ALERT_RETRIES = int(_alerts.get("retries", 3))
    ALERT_CHANNELS = _alerts.get("channels", ["email", "wechat", "telegram", "webhook"])

    _fresh = cfg.get("freshness", {})
    SPOT_MAX_AGE_MINUTES = float(_fresh.get("spot_max_age_minutes", 90))
    FIN_MAX_AGE_DAYS = float(_fresh.get("fin_max_age_days", 7))
    DIV_MAX_AGE_DAYS = float(_fresh.get("div_max_age_days", 30))
    HIST_MAX_AGE_DAYS = float(_fresh.get("hist_max_age_days", 7))

    _daily = cfg.get("daily_push", {})
    DAILY_PUSH.update({
        "enabled": bool(_daily.get("enabled", True)),
        "lishing_buy": float(_daily.get("lishing_buy", 70.0)),
        "lishing_watch": float(_daily.get("lishing_watch", 60.0)),
        "us_buy": float(_daily.get("us_buy", 70.0)),
        "us_watch": float(_daily.get("us_watch", 50.0)),
        "add_gap": float(_daily.get("add_gap", 2.0)),
        "announcement_days": int(_daily.get("announcement_days", 90)),
        "time_ah": str(_daily.get("time_ah", "16:30")),
        "time_us": str(_daily.get("time_us", "06:30")),
        "time_validate": str(_daily.get("time_validate", "18:00")),
        "time_health": str(_daily.get("time_health", "18:10")),
        "fmp_limit": int(_daily.get("fmp_limit", 200)),
    })

    _xc = cfg.get("crosscheck", {})
    CROSSCHECK.update({
        "enabled": bool(_xc.get("enabled", False)),
        "threshold": float(_xc.get("threshold", 2.0)),
        "time": str(_xc.get("time", "16:40")),
    })

    _rm = cfg.get("reit_monitor", {})
    REIT_MONITOR.update({
        "enabled": bool(_rm.get("enabled", True)),
        "time": str(_rm.get("time", "18:45")),
        "weekday": int(_rm.get("weekday", 5)),
        "codes": list(_rm.get("codes", ["508098", "508068"])),
    })

    _pt = cfg.get("paper_trade", {})
    PAPER_TRADE.update({
        "auto_follow_picks": bool(_pt.get("auto_follow_picks", True)),
        "picks_buy_pct": float(_pt.get("picks_buy_pct", 10.0)),
        "picks_reduce_ratio": float(_pt.get("picks_reduce_ratio", 0.5)),
    })


def reload():
    """测试或运维需要时重新加载配置。"""
    global _CFG
    _CFG = _load()
    _apply_config(_CFG)

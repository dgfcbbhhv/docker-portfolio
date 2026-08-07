"""数据新鲜度与健康状态：给 App 和后台预警提供统一的数据门禁。"""
import datetime
import json
import logging
import os

import pandas as pd

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_ROOT, "data")
STATUS_PATH = os.path.join(DATA_DIR, "data_status.json")

_CATEGORY_DIRS = ["spot", "fin", "div", "vol", "hist", "sma", "pctl", "listing"]


def _max_age_seconds(cat):
    d = os.path.join(DATA_DIR, cat)
    if not os.path.isdir(d):
        return None
    ages = []
    for f in os.listdir(d):
        if not f.endswith(".parquet"):
            continue
        p = os.path.join(d, f)
        try:
            ts = pd.read_parquet(p, columns=["_ts"])["_ts"].iloc[0]
            ages.append((datetime.datetime.now() - pd.to_datetime(ts)).total_seconds())
        except Exception:
            continue
    return max(ages) if ages else None


def collect_status():
    status = {
        "collected_at": datetime.datetime.now().isoformat(),
        "categories": {},
    }
    for cat in _CATEGORY_DIRS:
        age = _max_age_seconds(cat)
        status["categories"][cat] = {"max_age_seconds": age}
    return status


def save_status(status=None):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump(status or collect_status(), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("save status failed: %s", e)


def load_status():
    """读取上次持久化的数据状态，供 App / 后台预警 / 健康检查交叉验证。"""
    try:
        if os.path.exists(STATUS_PATH):
            with open(STATUS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning("load status failed: %s", e)
    return None


def freshness_issues(cfg=None):
    """返回 (issues, status)。issues 为空表示数据新鲜度达标。"""
    from modules.config import get_config
    freshness_cfg = cfg or get_config().get("freshness", {})
    status = collect_status()
    cats = status["categories"]
    issues = []

    spot = cats.get("spot", {}).get("max_age_seconds")
    if spot is not None:
        limit = float(freshness_cfg.get("spot_max_age_minutes", 90)) * 60
        if spot > limit:
            issues.append("行情缓存已 %.0f 分钟未更新（阈值 %.0f 分钟）" % (spot / 60, limit / 60))

    fin = cats.get("fin", {}).get("max_age_seconds")
    if fin is not None:
        limit = float(freshness_cfg.get("fin_max_age_days", 7)) * 86400
        if fin > limit:
            issues.append("财务缓存已 %.1f 天未更新（阈值 %.0f 天）" % (fin / 86400, limit / 86400))

    div = cats.get("div", {}).get("max_age_seconds")
    if div is not None:
        limit = float(freshness_cfg.get("div_max_age_days", 30)) * 86400
        if div > limit:
            issues.append("分红缓存已 %.1f 天未更新（阈值 %.0f 天）" % (div / 86400, limit / 86400))

    hist = cats.get("hist", {}).get("max_age_seconds")
    if hist is not None:
        limit = float(freshness_cfg.get("hist_max_age_days", 7)) * 86400
        if hist > limit:
            issues.append("历史行情缓存已 %.1f 天未更新（阈值 %.0f 天）" % (hist / 86400, limit / 86400))

    return issues, status


def flag_rows(df):
    """为持仓 DataFrame 增加/刷新 data_flags 列（手动数据与缺失标记）。"""
    if df is None or df.empty:
        return df

    def _flags(r):
        flags = []
        raw = str(r.get("data_flags", "") or "")
        if raw:
            flags.append(raw)
        if r.get("manual_dividend"):
            flags.append("manual_dividend")
        if r.get("manual_price"):
            flags.append("manual_price")
        return "; ".join(x for x in flags if x)

    df = df.copy()
    df["data_flags"] = df.apply(_flags, axis=1)
    return df


def summary_line(status):
    cats = status.get("categories", {})
    parts = []
    for cat in ("spot", "fin", "div", "hist"):
        age = cats.get(cat, {}).get("max_age_seconds")
        if age is None:
            parts.append("%s: 无缓存" % cat)
        elif cat == "spot":
            parts.append("行情 %.0f 分钟前" % (age / 60))
        elif cat == "fin":
            parts.append("财务 %.1f 天前" % (age / 86400))
        elif cat == "hist":
            parts.append("历史 %.1f 天前" % (age / 86400))
        else:
            parts.append("分红 %.1f 天前" % (age / 86400))
    return " | ".join(parts)

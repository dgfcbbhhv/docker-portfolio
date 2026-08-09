"""A 股个股公告抓取与缓存。

数据源: akshare stock_individual_notice_report(security=代码, symbol="全部")
输出: data/announcements/{code}.json, 按天过期。
每条记录字段: date/title/url(东财原文链接)。任一项缺失即为"数据不足"。

H 股 / 美股没有现成公告接口, 由 daily_picks 用分红记录 / 财报日期兜底。
"""
import datetime
import json
import logging
import os

logger = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "announcements")


def _path(code):
    return os.path.join(_DIR, f"{code}.json")


def _load_cache(code, max_age_days):
    p = _path(code)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        ts = d.get("cached_at")
        if not ts:
            return None
        cached = datetime.datetime.fromisoformat(ts)
        if datetime.datetime.now() - cached > datetime.timedelta(days=max_age_days):
            return None
        return d
    except Exception:
        return None


def _save_cache(code, records):
    os.makedirs(_DIR, exist_ok=True)
    d = {
        "cached_at": datetime.datetime.now().isoformat(),
        "records": records,
    }
    tmp = _path(code) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _path(code))


def fetch(code, begin_date, end_date, max_age_days=1):
    """返回公告记录列表; 缓存命中则直接返回, 过期则重新抓取。

    records: [{date, title, url}, ...], 按日期倒序(最新在前)。
    抓取失败返回 None(区别于"无公告"的空列表)。
    """
    cached = _load_cache(code, max_age_days)
    if cached is not None:
        return cached.get("records", [])

    import akshare as ak
    try:
        df = ak.stock_individual_notice_report(
            security=code, symbol="全部",
            begin_date=begin_date, end_date=end_date)
    except Exception as e:
        logger.warning("公告抓取失败 %s: %s", code, e)
        return None

    records = []
    for _, row in df.iterrows():
        date = row.get("公告日期")
        title = row.get("公告标题")
        url = row.get("公告链接")
        if date is not None:
            date = str(date)[:10]
        rec = {
            "date": date if date else "",
            "title": str(title) if title is not None else "",
            "url": str(url) if url is not None else "",
        }
        records.append(rec)

    # 按日期倒序
    records.sort(key=lambda r: r["date"], reverse=True)
    _save_cache(code, records)
    logger.info("公告缓存更新 %s: %d 条", code, len(records))
    return records


def latest(code, begin_date, end_date, max_age_days=1):
    """取最近一条公告(供文案展示); 无公告返回 None。"""
    records = fetch(code, begin_date, end_date, max_age_days=max_age_days)
    if not records:
        return None
    # 取第一条非空标题的公告
    for r in records:
        if r.get("title"):
            return r
    return None

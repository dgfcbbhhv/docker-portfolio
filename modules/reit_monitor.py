"""公募REIT 分派率跟踪 — 独立于 L氏信号体系 (REITs 无传统基本面, 不进每日快照)。

数据源 (免费公开, 每周一次, 不消耗 FMP 配额):
- 分红历史: 天天基金 fhsp 页面 (每10份派现金)
- 单位净值: 东财 F10 lsjz API
- 现价: 腾讯行情 sh508xxx

指标:
- 年化分派率 = 近12个月每份分红 / 现价
  (净值仅在季报发布, 免费接口无可靠数据, 溢价率暂不跟踪)
"""
import datetime
import io
import json
import logging
import os
import re

import pandas as pd
import requests

from modules.config import WATCH_LIST

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_FILE = os.path.join(_ROOT, "data", "reports", "reit_monitor.json")


def _get(url, referer="http://fundf10.eastmoney.com/"):
    r = requests.get(url, timeout=15, headers={
        "User-Agent": "Mozilla/5.0", "Referer": referer})
    r.raise_for_status()
    return r


def fetch_dividend_rows(code):
    """返回按除息日倒序的 [{date, per10}] 分红记录; 失败返回 []。"""
    try:
        html = _get(f"http://fundf10.eastmoney.com/fhsp_{code}.html").text
        for df in pd.read_html(io.StringIO(html)):
            cols = [str(c) for c in df.columns]
            if not any("每10份" in c for c in cols):
                continue
            rows = []
            for _, r in df.iterrows():
                m = re.search(r"派现金([\d.]+)", str(r.get("每10份分红", "")))
                if not m:
                    continue
                rows.append({
                    "date": str(r.get("除息日", "") or "").strip(),
                    "per10": float(m.group(1)),
                })
            return rows
    except Exception as e:
        logger.warning("[REIT] %s 分红抓取失败: %s", code, e)
    return []


def fetch_price(code):
    """现价 via 腾讯行情 (sh508xxx)。"""
    try:
        r = _get(f"http://qt.gtimg.cn/q=sh{code}", referer="https://gu.qq.com/")
        r.encoding = "gbk"
        m = re.search(r'"(.+)"', r.text)
        if m:
            parts = m.group(1).split("~")
            if len(parts) > 3 and parts[0] in ("200", "1"):
                return float(parts[3])
    except Exception as e:
        logger.warning("[REIT] %s 现价抓取失败: %s", code, e)
    return None


def _trailing_12m_per_unit(rows, today=None):
    """近12个月每份分红(元)。rows 按除息日倒序。"""
    today = today or datetime.date.today()
    cutoff = today - datetime.timedelta(days=365)
    total = 0.0
    for r in rows:
        try:
            d = datetime.date.fromisoformat(r["date"])
        except (ValueError, TypeError):
            continue
        if d >= cutoff:
            total += r["per10"] / 10.0
    return total


def summary(code):
    """单只 REIT 的分派率/溢价率汇总。"""
    name = (WATCH_LIST.get(code, {}) or {}).get("name", code)
    price = fetch_price(code)
    rows = fetch_dividend_rows(code)
    per_unit = _trailing_12m_per_unit(rows)
    return {
        "code": code,
        "name": name,
        "price": price,
        "trailing12m_div_per_unit": round(per_unit, 4),
        "annual_yield_pct": round(per_unit / price * 100, 2) if price and per_unit else None,
        "last_div_date": rows[0]["date"] if rows else None,
    }


def reit_report(codes=None):
    """生成 REIT 监控报告并落盘 data/reports/reit_monitor.json。"""
    codes = codes or ["508098", "508068"]
    report = {
        "generated_at": datetime.datetime.now().isoformat(),
        "items": [summary(c) for c in codes],
    }
    try:
        os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
        tmp = REPORT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        os.replace(tmp, REPORT_FILE)
    except Exception as e:
        logger.warning("[REIT] 报告写入失败: %s", e)
    return report


def format_report(report):
    """报告转推送文案。"""
    lines = []
    for it in report["items"]:
        lines.append(
            f'{it["name"]} {it["code"]}: 现价 {it["price"]} | '
            f'年化分派率 {it["annual_yield_pct"]}% | '
            f'近12月分红 {it["trailing12m_div_per_unit"]}/份 | '
            f'最近分红 {it["last_div_date"]}')
    return "\n".join(lines)

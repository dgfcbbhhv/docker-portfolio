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


def reit_signal(code, price=None):
    """REIT 完整信号组件: 现价/分派率/估值分位/均线/PE/Kelly/综合评分。

    REITs 无 ROE/EPS 等传统基本面, 综合评分 = 分派率(40) + 估值分位(30) + 趋势(30)。
    返回 dict 或 None (数据不足)。
    """
    try:
        from modules.data.market import fetch_price_percentile, fetch_sma
        from modules.kelly import run as kelly_run
        price = price or fetch_price(code)
        if not price or price <= 0:
            return None
        rows = fetch_dividend_rows(code)
        per_unit = _trailing_12m_per_unit(rows)
        dy = round(per_unit / price * 100, 2) if per_unit else None
        pctl = fetch_price_percentile(code, 5, "A")
        if pctl is None:
            pctl = 50.0
        ma20 = fetch_sma(code, 20, mkt="A")
        ma60 = fetch_sma(code, 60, mkt="A")
        p_ma20 = bool(ma20 and price > ma20)
        ma20_ma60 = bool(ma20 and ma60 and ma20 > ma60)
        p_ma60 = bool(ma60 and price > ma60)
        pe = round(100.0 / dy, 2) if dy else None
        kelly = kelly_run(price=price, pb=None, pe=pe, roe=None, eps=None,
                          debt=None, div=per_unit or 0.0, vol=0.2)
        kf = kelly["kelly_vol_adj"]
        psig = kelly["p_signal"]
        dy_s = (40 if dy >= 5 else 32 if dy >= 4 else 24 if dy >= 3
                else 12 if dy >= 2 else 0) if dy else 0
        val_s = 30 if pctl <= 30 else 20 if pctl <= 50 else 10 if pctl <= 70 else 0
        trend_s = (10 if p_ma20 else 0) + (10 if ma20_ma60 else 0) + (10 if p_ma60 else 0)
        ls_score = min(100.0, dy_s + val_s + trend_s)
        flags = []
        if (dy or 0) < 3:
            flags.append("分派率偏低")
        if pctl > 70:
            flags.append("估值分位偏高")
        return {
            "code": code,
            "price": price,
            "div_yield": dy,
            "pe": pe,
            "pb": None,
            "pe_pctl": pctl,
            "pb_pctl": pctl,
            "lishing_score": round(ls_score, 1),
            "lishing_flags": flags,
            "lishing_status": "warning" if flags else "normal",
            "price_above_ma20": p_ma20,
            "ma20_above_ma60": ma20_ma60,
            "kelly_final": kf,
            "p_signal": psig,
            "vol_adj_factor": kelly["vol_adj_factor"],
            "audit": True,
            "data_source": "tx+fundf10",
        }
    except Exception as e:
        logger.warning("[REIT] %s 信号计算失败: %s", code, e)
        return None


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

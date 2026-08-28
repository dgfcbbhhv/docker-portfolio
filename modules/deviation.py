"""异动偏离监控（借鉴交易所异动规则口径：3 / 10 / 30 日累计偏离值）。

与原有单日 price_drop 互补：单日跌幅只看一天，偏离值看多日累计，更能捕捉
趋势性异动（连续下跌 / 拉升）。使用本地缓存的日线（duckdb_cache）计算，
无数据则跳过（不强制拉取，避免盘中高频打扰）。
"""
import logging

from modules.config import DEVIATION
from modules.data.duckdb_cache import load_daily_quotes

logger = logging.getLogger(__name__)


def _returns_from_closes(closes):
    """closes: 按日期升序的收盘价序列。返回 {3:ret, 10:ret, 30:ret}（小数）。"""
    n = len(closes)
    out = {}
    for win in (3, 10, 30):
        if n > win:
            base = closes[-(win + 1)]
            if base and base > 0:
                out[win] = closes[-1] / base - 1
    return out


def check_deviation(codes, thresholds=None):
    """对给定代码列表计算 3 / 10 / 30 日累计偏离，超阈值返回 alerts（与 check_alerts 同结构）。"""
    thresholds = thresholds or {
        "d3": float(DEVIATION.get("d3", 20)),
        "d10": float(DEVIATION.get("d10", 50)),
        "d30": float(DEVIATION.get("d30", 100)),
    }
    alerts = []
    for code in codes:
        df = load_daily_quotes(code)
        if df is None or df.empty or "close" not in df.columns or len(df) < 5:
            continue
        closes = df["close"].dropna().tolist()
        rets = _returns_from_closes(closes)
        for win, thr in (("3", thresholds["d3"]), ("10", thresholds["d10"]), ("30", thresholds["d30"])):
            r = rets.get(int(win))
            if r is None:
                continue
            pct = r * 100
            if abs(pct) >= float(thr):
                direction = "上涨" if pct > 0 else "下跌"
                alerts.append({
                    "code": code, "name": code, "type": "异动偏离",
                    "msg": f"{code} {win}日累计{direction} {pct:.1f}%（阈值±{thr:.0f}%）",
                })
    logger.info("[deviation] checked %d codes, %d deviation alerts", len(codes), len(alerts))
    return alerts


def run_deviation_check(worker=None):
    from modules.config import WATCH_LIST, PORT_A, PORT_HK, PORT_US, PORT_REIT
    codes = set()
    for m in (PORT_A, PORT_HK, PORT_US, PORT_REIT):
        codes.update(m.keys())
    for m in WATCH_LIST.values():
        if isinstance(m, dict):
            codes.update(m.keys())
    codes = [c for c in codes if c]
    alerts = check_deviation(codes)
    if not alerts:
        return alerts
    if worker is not None:
        alerts = worker._dedup(alerts)
        if alerts:
            worker._send_alerts(alerts, {})
    return alerts

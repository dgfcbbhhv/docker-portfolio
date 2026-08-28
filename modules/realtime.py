"""实时行情接入（easy_tdx / 通达信协议直连，无需 API key）。

仅依赖 MacClient（CLI 同款 ``from_best_host`` 选最快服务器路径），提供：
  - get_quote(code)            实时报价（价/涨跌/换手/量比/PE/PB/市值/主力净流入）
  - get_capital_flow(code)     主力/散户/中单 净流入明细
  - get_indicators(code)       MACD / KDJ / RSI / BIAS（基于日线 QFQ）
  - get_realtime_profile(code) 上述三者合并，供 AI 复盘 / 候选扫描直接使用

设计为「尽力而为」：任何网络/源异常都返回带 error 字段的 dict，不抛出，
调用方据此降级（如回退到 akshare 快照），不会拖垮主流程。
"""
import logging
import threading

logger = logging.getLogger(__name__)

try:
    from easy_tdx.mac.client import MacClient
    from easy_tdx import Market, KlineCategory, Adjust, indicator
    _HAVE_ETDX = True
except Exception as _e:  # pragma: no cover - 未装 easy_tdx 时优雅降级
    logger.warning("[realtime] easy_tdx 不可用：%s（实时数据将全部降级）", _e)
    _HAVE_ETDX = False


_MARKET_MAP = {"SH": Market.SH, "SZ": Market.SZ, "BJ": Market.BJ} if _HAVE_ETDX else {}
_CODE_PREFIX_MARKET = (
    {"6": Market.SH, "9": Market.SH, "0": Market.SZ, "3": Market.SZ, "8": Market.BJ, "4": Market.BJ}
    if _HAVE_ETDX else {}
)

_lock = threading.Lock()
_client = None


def _get_client():
    global _client
    if not _HAVE_ETDX:
        return None
    with _lock:
        if _client is None:
            _client = MacClient.from_best_host()
            _client.connect()
        return _client


def _reconnect():
    global _client
    if not _HAVE_ETDX:
        return None
    with _lock:
        try:
            if _client is not None:
                _client.close()
        except Exception:
            pass
        _client = MacClient.from_best_host()
        _client.connect()
        return _client


def _market_of(code):
    code = str(code).strip()
    if code[:2].upper() in _MARKET_MAP:
        return _MARKET_MAP[code[:2].upper()], code[2:].strip()
    return _CODE_PREFIX_MARKET.get(code[0], Market.SZ), code


def _wan(v):
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return round(v / 1e4, 1)


def get_quote(code):
    if not _HAVE_ETDX:
        return {"code": code, "error": "easy_tdx 未安装"}
    try:
        mkt, c = _market_of(code)
        df = _get_client().get_stock_quotes([(mkt, c)])
        if df is None or len(df) == 0:
            return {"code": c, "error": "empty"}
        return _normalize_quote(df.iloc[0].to_dict())
    except Exception as e:
        logger.warning("[realtime] quote %s 失败: %s", code, e)
        try:
            _reconnect()
        except Exception:
            pass
        return {"code": code, "error": str(e)}


def _normalize_quote(r):
    price = float(r.get("close") or 0)
    pre = float(r.get("pre_close") or 0)
    pct = round((price - pre) / pre * 100, 2) if pre else None
    net_assets = float(r.get("net_assets") or 0)
    pb = round(price / net_assets, 2) if net_assets else None
    mcap = float(r.get("total_market_cap_ab") or 0)
    return {
        "code": str(r.get("code")),
        "name": r.get("name"),
        "price": price,
        "pre_close": pre,
        "open": float(r.get("open") or 0),
        "high": float(r.get("high") or 0),
        "low": float(r.get("low") or 0),
        "pct": pct,
        "amount": float(r.get("amount") or 0),
        "vol": float(r.get("vol") or 0),
        "vol_ratio": float(r.get("vol_ratio") or 0),
        "turnover": float(r.get("turnover") or 0),
        "pe_ttm": (float(r.get("pe_ttm") or 0) or None),
        "pe_dynamic": (float(r.get("pe_dynamic") or 0) or None),
        "pb": pb,
        "mcap_yi": (round(mcap / 1e8, 1) if mcap else None),
        "main_net": float(r.get("main_net_amount") or 0),
        "speed_pct": float(r.get("speed_pct") or 0),
    }


def get_capital_flow(code):
    if not _HAVE_ETDX:
        return {"error": "easy_tdx 未安装"}
    try:
        mkt, c = _market_of(code)
        df = _get_client().get_capital_flow(mkt, c)
        if df is None or len(df) == 0:
            return {"error": "empty"}
        r = df.iloc[-1].to_dict()
        return {
            "main_net": float(r.get("main_net") or 0),
            "main_in": float(r.get("main_in") or 0),
            "main_out": float(r.get("main_out") or 0),
            "small_net": float(r.get("small_net") or 0),
            "mid_net": float(r.get("mid_net") or 0),
            "large_net": float(r.get("large_net") or 0),
        }
    except Exception as e:
        logger.warning("[realtime] capital_flow %s 失败: %s", code, e)
        return {"error": str(e)}


def get_indicators(code, count=200):
    if not _HAVE_ETDX:
        return {"error": "easy_tdx 未安装"}
    try:
        mkt, c = _market_of(code)
        bars = _get_client().get_stock_kline(mkt, c, KlineCategory.DAY, 0, count, adjust=Adjust.QFQ)
        if bars is None or len(bars) < 30:
            return {"error": "no bars"}
        ind = indicator.compute_indicators(bars, ["MACD", "KDJ", "RSI", "BIAS"])
        last = ind.iloc[-1]
        return {
            "macd_dif": round(float(last.get("MACD_DIF") or 0), 3),
            "macd_dea": round(float(last.get("MACD_DEA") or 0), 3),
            "macd_hist": round(float(last.get("MACD_HIST") or 0), 3),
            "kdj_k": round(float(last.get("KDJ_K") or 0), 1),
            "kdj_d": round(float(last.get("KDJ_D") or 0), 1),
            "kdj_j": round(float(last.get("KDJ_J") or 0), 1),
            "rsi": round(float(last.get("RSI") or 0), 1),
            "bias": round(float(last.get("BIAS1") or 0), 2),
        }
    except Exception as e:
        logger.warning("[realtime] indicators %s 失败: %s", code, e)
        return {"error": str(e)}


def get_realtime_profile(code, with_indicators=True):
    prof = dict(get_quote(code))
    prof["capital"] = get_capital_flow(code)
    if with_indicators:
        prof["indicators"] = get_indicators(code)
    return prof


def main_net_wan(code=None, profile=None):
    """便捷：返回主力净流入（万元，带符号字符串）。"""
    if profile is None:
        if code is None:
            return None
        profile = get_quote(code)
    v = profile.get("main_net", 0) or 0
    return _wan(v)

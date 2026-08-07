"""长桥 OpenAPI 数据源（美股/港股行情备援）

凭据从环境变量读取：LONGBRIDGE_APP_KEY / LONGBRIDGE_APP_SECRET / LONGBRIDGE_ACCESS_TOKEN
SDK 未安装或凭据缺失/鉴权失败时全部降级返回空结果，绝不阻塞主流程。
"""
import os
import logging
import pandas as pd

logger = logging.getLogger(__name__)

_CONFIG = None
_LB = None


def _available():
    global _LB
    if _LB is None:
        try:
            import longbridge  # noqa: F401
            _LB = True
        except Exception:
            _LB = False
    return _LB


def _config():
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG
    if not _available():
        return None
    app_key = os.environ.get("LONGBRIDGE_APP_KEY", "").strip()
    app_secret = os.environ.get("LONGBRIDGE_APP_SECRET", "").strip()
    access_token = os.environ.get("LONGBRIDGE_ACCESS_TOKEN", "").strip()
    if not (app_key and app_secret and access_token):
        return None
    try:
        from longbridge.openapi import Config
        _CONFIG = Config.from_apikey(app_key, app_secret, access_token)
    except Exception as e:
        logger.warning("长桥配置失败: %s", e)
        _CONFIG = False
    return _CONFIG if _CONFIG else None


def _is_cn_mainland_code(code):
    c = str(code).upper()
    return (c.endswith(".HK") or c.endswith(".US") or c.endswith(".SH") or c.endswith(".SZ"))


def _to_lb_symbol(code):
    c = str(code).upper().strip()
    if c.endswith(".HK"):
        return c
    if c.endswith(".US"):
        return c
    if c.endswith((".SH", ".SZ", ".BJ")):
        return c
    if c.isdigit() and len(c) <= 5:
        return f"{c}.HK"
    if c.isdigit() and len(c) == 6:
        if c.startswith(("60", "68", "9")):
            return f"{c}.SH"
        if c.startswith(("00", "30", "20")):
            return f"{c}.SZ"
        return f"{c}.HK" if c.startswith("0") else c
    return f"{c}.US"


def fetch_spot(codes):
    """批量实时报价 → [{"c": code, "name": str, "price": float, "chg": float, "pe": float|None}]
    空/失败返回 []。"""
    cfg = _config()
    if not cfg:
        return []
    try:
        from longbridge.openapi import QuoteContext
        symbols = [_to_lb_symbol(c) for c in codes]
        ctx = QuoteContext(cfg)
        infos = ctx.static_info(symbols)
        quotes = ctx.quote(symbols)
        by_sym = {}
        for q in quotes or []:
            by_sym[q.symbol] = q
        name_by = {}
        for i in infos or []:
            name_by[i.symbol] = i.name_en or i.name_cn or i.symbol
        results = []
        for code, sym in zip(codes, symbols):
            q = by_sym.get(sym)
            if q is None:
                continue
            price = float(q.last_done) if q.last_done is not None else 0.0
            prev = float(q.prev_close) if q.prev_close is not None else 0.0
            chg = round((price - prev) / prev * 100, 2) if prev > 0 else 0.0
            pe = float(q.pe_ttm) if getattr(q, "pe_ttm", None) else None
            results.append({"c": code, "name": name_by.get(sym, code),
                            "price": price, "chg": chg, "pe": pe, "pb": None})
        return results
    except Exception as e:
        logger.warning("长桥实时报价失败: %s", e)
        return []


def fetch_hist(code, start=None, end=None, days=750):
    """日K线 → DataFrame[date,open,high,low,close,volume] 或 None。
    timestamp 为 UTC，转成 %Y-%m-%d 日期字符串。"""
    cfg = _config()
    if not cfg:
        return None
    try:
        import datetime as _dt
        from longbridge.openapi import QuoteContext, Period, AdjustType
        if start is None:
            end_d = _dt.date.today()
            start_d = end_d - _dt.timedelta(days=days)
            start = start_d.strftime("%Y-%m-%d")
        if end is None:
            end = _dt.date.today().strftime("%Y-%m-%d")
        sym = _to_lb_symbol(code)
        ctx = QuoteContext(cfg)
        klines = ctx.candlesticks(sym, Period.Day, count=days + 10,
                                  adjust_type=AdjustType.ForwardAdjust)
        if not klines:
            return None
        rows = []
        for k in klines:
            d = _dt.datetime.fromisoformat(str(k.timestamp)[:19]).date()
            dstr = d.strftime("%Y-%m-%d")
            if start <= dstr <= end:
                rows.append({"date": dstr, "open": float(k.open),
                             "high": float(k.high), "low": float(k.low),
                             "close": float(k.close), "volume": float(k.volume or 0)})
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df = df.sort_values("date").reset_index(drop=True)
        return df
    except Exception as e:
        logger.warning("长桥 %s 历史数据失败: %s", code, e)
        return None

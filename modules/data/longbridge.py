"""长桥 OpenAPI 数据源（A股/港股/美股行情主源备援）

凭据从环境变量读取：LONGBRIDGE_APP_KEY / LONGBRIDGE_APP_SECRET / LONGBRIDGE_ACCESS_TOKEN
Access Token（旧版 API Key 认证）默认 90 天过期；本模块首次调用前自动执行一次
token 保鲜：距上次刷新 ≥60 天即调用 refresh_access_token 换取新 token，
写回 .env 与 os.environ，使凭证全自动续期、无需人工干预。
SDK 未安装/凭据缺失/鉴权失败时全部降级返回空结果，绝不阻塞主流程。
"""
import datetime
import logging
import os

import pandas as pd

logger = logging.getLogger(__name__)

_CONFIG = None
_LB = None
_REFRESH_GUARD = False


def _available():
    global _LB
    if _LB is None:
        try:
            import longbridge  # noqa: F401
            _LB = True
        except Exception:
            _LB = False
    return _LB


def _env_file():
    for p in ("/app/.env", os.path.join(os.path.dirname(__file__), "..", "..", ".env")):
        if os.path.exists(p):
            return os.path.abspath(p)
    return None


def _refresh_if_needed():
    """每 ~60 天自动换取一个新 access token 并把其余额持久化到 .env。

    幂等且 fail-safe：刷新失败时保留旧 token，绝不抛出。
    """
    global _REFRESH_GUARD
    if _REFRESH_GUARD:
        return
    _REFRESH_GUARD = True
    try:
        from longbridge.openapi import Config
    except Exception:
        return
    app_key = os.environ.get("LONGBRIDGE_APP_KEY", "").strip()
    app_secret = os.environ.get("LONGBRIDGE_APP_SECRET", "").strip()
    access_token = os.environ.get("LONGBRIDGE_ACCESS_TOKEN", "").strip()
    if not (app_key and app_secret and access_token):
        return

    last = os.environ.get("LONGBRIDGE_TOKEN_REFRESHED", "").strip()
    if last:
        try:
            ts = datetime.datetime.strptime(last[:10], "%Y-%m-%d")
            if (datetime.date.today() - ts.date()).days < 60:
                return
        except ValueError:
            pass

    try:
        cfg = Config.from_apikey(app_key, app_secret, access_token, http_url=_http_url())
        new_token = cfg.refresh_access_token()
        if not new_token or new_token == access_token:
            return
        os.environ["LONGBRIDGE_ACCESS_TOKEN"] = new_token
        today = datetime.date.today().isoformat()
        os.environ["LONGBRIDGE_TOKEN_REFRESHED"] = today
        _persist_env(new_token, today)
        logger.info("长桥 access token 自动续期成功 (%s)", today)
    except Exception as e:
        logger.warning("长桥 token 保鲜失败(继续用旧 token): %s", e)


def _persist_env(new_token, refreshed):
    """把新 token 写回容器挂载的 .env，保证重启后 compose 仍能读到新值。"""
    path = _env_file()
    if not path:
        return
    try:
        lines = []
        for raw in open(path, "rb").read().decode("utf-8-sig").splitlines():
            lines.append(raw.rstrip("\r\n"))
        out = []
        wrote_refresh = False
        for line in lines:
            if line.startswith("LONGBRIDGE_ACCESS_TOKEN="):
                out.append(f"LONGBRIDGE_ACCESS_TOKEN={new_token}")
            elif line.startswith("LONGBRIDGE_TOKEN_REFRESHED="):
                out.append(f"LONGBRIDGE_TOKEN_REFRESHED={refreshed}")
                wrote_refresh = True
            else:
                out.append(line)
        if not any(l.startswith("LONGBRIDGE_ACCESS_TOKEN=") for l in out):
            out.append(f"LONGBRIDGE_ACCESS_TOKEN={new_token}")
        if not wrote_refresh:
            out.append(f"LONGBRIDGE_TOKEN_REFRESHED={refreshed}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    except Exception as e:
        logger.warning("长桥 token 写回 .env 失败: %s", e)


def _http_url():
    """region=cn 走国内端点, 其余默认官方端点。"""
    region = os.environ.get("LONGBRIDGE_REGION", "").strip().lower()
    if region in ("cn", "cnc"):
        return "https://openapi.longbridge.cn"
    return None


def _build_config():
    from longbridge.openapi import Config
    return Config.from_apikey(
        os.environ.get("LONGBRIDGE_APP_KEY", "").strip(),
        os.environ.get("LONGBRIDGE_APP_SECRET", "").strip(),
        os.environ.get("LONGBRIDGE_ACCESS_TOKEN", "").strip(),
        http_url=_http_url(),
    )


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
    _refresh_if_needed()
    try:
        _CONFIG = _build_config()
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
        if c.startswith(("60", "68", "9", "50")):
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

        from longbridge.openapi import AdjustType, Period, QuoteContext
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

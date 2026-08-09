"""跨数据源一致性校验（旁路监控，不参与实时渲染链路）。
import logging

logger = logging.getLogger(__name__)
用途
----
对同一指标（当前实时价）从两个相互独立的源分别拉取，逐标的比对。
在数据进入 Kelly / 风控计算前，尽早发现某个源返回陈旧、撕裂或占位数据。

与 fallback.py 的关系
---------------------
fallback.py 只在"主源失败时换下一个"，不校验一致性。
本模块是外层旁路：fallback 语义不变（仍取首个可用源），互验只做告警。

可严格对账的配对（各市场两个独立源）
    A  : 新浪(`_fetch_sina`)           vs 腾讯(`_fetch_tx_quotes`)
    HK : 新浪(`_fetch_sina_hk`)        vs 长桥(`longbridge.fetch_spot`)
    US : 腾讯美股(`_fetch_us_spot_tx`) vs 长桥(`longbridge.fetch_spot`)

股息率/PE/PB 等口径与时点不一致，只做区间合理性判断，不做逐源严对账（见 validate_sanity）。

用法
----
    from modules.data.validate import validate_spot_by_market, THRESHOLDS
    mkts = {"A": [codes...], "HK": [...], "US": [...]}
    report = validate_spot_by_market(mkts)   # -> {"rows": [...], "summary": {...}}
"""

import re

# 价差告警阈值（%）：跨市场因时区/币种/停牌差异，美股放开些
THRESHOLDS = {"A": 1.0, "HK": 1.0, "US": 2.0}

# 标的名义上限（%年）：用于股息率合理性兜底
DY_LOWER, DY_UPPER = 0.0, 25.0

_PREFIX = re.compile(r"^(sh|sz|bj|hk|rt_hk)")
# 交易时段内允许的最大价差（%），超出即视为源不一致
_DEFAULT_TIMEOUT = 30


def _strip_prefix(code):
    return _PREFIX.sub("", str(code))


def _src_sina_a(codes):
    """新浪A股实时 → {code: price}"""
    from modules.data.market import _fetch_sina
    out = {}
    df = _fetch_sina(codes)
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            try:
                p = float(r["price"])
            except (TypeError, ValueError):
                continue
            if p > 0:
                out[_strip_prefix(r["c"])] = p
    return out


def _src_tx_a(codes):
    """腾讯实时（A股）→ {code: price}"""
    from modules.data.market import _fetch_tx_quotes
    out = {}
    rows = _fetch_tx_quotes(codes)
    for raw, q in rows.items():
        try:
            p = float(q["price"])
        except (TypeError, ValueError):
            continue
        if p > 0:
            out[_strip_prefix(raw)] = p
    return out


def _src_sina_hk(codes):
    """新浪港股实时 → {code: price}"""
    from modules.data.market import _fetch_sina_hk
    out = {}
    df = _fetch_sina_hk(codes)
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            try:
                p = float(r["price"])
            except (TypeError, ValueError):
                continue
            if p > 0:
                out[_strip_prefix(r["c"])] = p
    return out


def _src_longbridge(codes):
    """长桥实时 → {code: price}"""
    from modules.data.longbridge import fetch_spot
    out = {}
    try:
        for d in fetch_spot(codes) or []:
            try:
                p = float(d.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if p > 0:
                out[_strip_prefix(d["c"])] = p
    except Exception as _e:
        logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    return out


def _src_us_tx(codes):
    """腾讯美股实时 → {code: price}"""
    from modules.data.market_us import _fetch_us_spot_tx
    out = {}
    df = _fetch_us_spot_tx(codes)
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            try:
                p = float(r["price"])
            except (TypeError, ValueError):
                continue
            if p > 0:
                out[_strip_prefix(r["c"])] = p
    return out


# 市场 -> (源1名, 源1拉取器, 源2名, 源2拉取器, 阈值%)
_SOURCES = {
    "A": ("新浪", _src_sina_a, "腾讯", _src_tx_a),
    "HK": ("新浪港股", _src_sina_hk, "长桥", _src_longbridge),
    "US": ("腾讯美股", _src_us_tx, "长桥", _src_longbridge),
}

# 各市场的第三方权威源（独立于新浪/腾讯/长桥的第四方，用于仲裁裁决）
# 注意：不能复用 fetch_spot_a/fetch_spot_hk，它们以新浪为优先且读磁盘缓存，
# 会与出错方"同源"，导致裁决偏向脏数据（已实测否决）。

def _em_price(codes, hk):
    """东方财富实时价（独立于新浪/腾讯）。

    字段码与价格刻度由 modules/data/fields.py 统一登记（A=分/÷100，HK=厘/÷1000）。
    """
    import urllib.request
    import json
    from modules.data.fields import (eastmoney_secid, eastmoney_price_scale,
                                     EASTMONEY)
    out = {}
    market = "HK" if hk else "A"
    spec = EASTMONEY[market]["spec"]
    price_field = spec.price_field
    scale = eastmoney_price_scale(market)
    for c in codes:
        secid = eastmoney_secid(market, c)
        url = (f"http://push2.eastmoney.com/api/qt/stock/get?"
               f"secid={secid}&fields={spec.code_field},{price_field}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = (json.loads(r.read().decode("utf-8")) or {}).get("data") or {}
                p = d.get(price_field)
                if p:
                    out[_strip_prefix(d.get(spec.code_field) or c)] = float(p) / scale
        except Exception as _e:
            logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    return out


def _auth_a(codes):
    return _em_price(codes, hk=False)


def _auth_hk(codes):
    return _em_price(codes, hk=True)


def _auth_us(codes):
    """美股：独立于腾讯/长桥的 Yahoo 行情。"""
    from modules.data.market_us import _get_yf
    out = {}
    try:
        yf = _get_yf()
        if not yf:
            return out
        for c in codes:
            try:
                p = yf.Ticker(c).fast_info["last_price"]
                p = float(p)
                if p and p > 0:
                    out[c] = p
            except Exception:
                continue
    except Exception as _e:
        logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    return out


# 市场 -> (权威源名, 权威拉取器, 仲裁容差%)
_AUTHORITY = {
    "A": ("东方财富", _auth_a, 1.0),
    "HK": ("东方财富", _auth_hk, 1.0),
    "US": ("Yahoo行情", _auth_us, 2.0),
}


def _adjudicate(row, mkt):
    """用第三方独立源仲裁：哪个源偏离权威价，即判定为嫌疑出错源。

    就地更新 row：authority / ap / suspect。返回 suspect 名（或 None）。
    """
    spec = _AUTHORITY.get(mkt)
    if not spec:
        row["authority"], row["ap"], row["suspect"] = None, None, None
        return None
    ap = None
    try:
        ap = (spec[1]([row["code"]]) or {}).get(row["code"])
    except Exception:
        ap = None
    tol = spec[2]
    suspect = None
    if ap is not None and ap > 0:
        for lbl, p in ((row["src1"], row["p1"]), (row["src2"], row["p2"])):
            if p and abs(p - ap) / ap * 100 > tol:
                suspect = lbl
                break
    row["authority"] = spec[0]
    row["ap"] = ap
    row["suspect"] = suspect if (ap is not None and ap > 0) else None
    return suspect


def _recheck(code, mkt):
    """对超阈值标的立即重新拉取两源，排除时序噪声（快照时点不同）。

    返回 (new_p1, new_p2)；任一拉取失败返回对应 None。
    """
    spec = _SOURCES.get(mkt)
    if not spec:
        return None, None
    try:
        mp1 = spec[1]([code]) or {}
    except Exception:
        mp1 = {}
    try:
        mp2 = spec[3]([code]) or {}
    except Exception:
        mp2 = {}
    return mp1.get(code), mp2.get(code)


def validate_spot_by_market(codes_by_mkt, name_fn=None):
    """按市场逐标的交叉校验实时价。

    参数
    ----
    codes_by_mkt : {"A": [..], "HK": [..], "US": [..]}
    name_fn : 可选，返回展示名

    返回
    ----
    {"rows": [...], "summary": {...}}
    """
    rows = []
    cnt = {"ok": 0, "mismatch": 0, "missing_s1": 0, "total": 0}
    for mkt, codes in (codes_by_mkt or {}).items():
        spec = _SOURCES.get(mkt)
        if not spec or not codes:
            continue
        try:
            p1 = spec[1](codes) or {}
        except Exception:
            p1 = {}
        try:
            p2 = spec[3](codes) or {}
        except Exception:
            p2 = {}
        keys = sorted(set(list(p1.keys()) + list(p2.keys())))
        cnt["total"] += len(keys)
        for c in keys:
            name = name_fn(c) if name_fn else str(c)
            row = {"code": c, "mkt": mkt, "name": name,
               "src1": spec[0], "src2": spec[2],
               "p1": p1.get(c), "p2": p2.get(c), "diff_pct": None,
               "authority": None, "ap": None, "suspect": None}
            if c not in p1:
                row["status"] = "missing_s1"
                cnt["missing_s1"] += 1
            elif c not in p2:
                row["status"] = "only_s1"
            else:
                base = p2[c] if p2[c] else p1[c]
                diff = abs(p1[c] - p2[c]) / base * 100 if base else 0.0
                row["diff_pct"] = round(diff, 2)
                th = THRESHOLDS.get(mkt, 2.0)
                if diff <= th:
                    row["status"] = "ok"
                    cnt["ok"] += 1
                else:
                    # 超阈值：立即双检确认。时序噪声下差异会回落，真撕裂会持续
                    new_p1, new_p2 = _recheck(c, mkt)
                    if None not in (new_p1, new_p2) and new_p1 > 0 and new_p2 > 0:
                        new_diff = abs(new_p1 - new_p2) / new_p2 * 100
                        row["diff_pct"] = round(new_diff, 2)
                        row["p1"], row["p2"] = new_p1, new_p2
                        if new_diff <= th:
                            row["status"] = "ok(recheck)"
                            cnt["ok"] += 1
                        else:
                            # 复检仍超阈值：第三方主线仲裁，标出嫌疑源
                            _adjudicate(row, mkt)
                            row["status"] = "mismatch"
                            cnt["mismatch"] += 1
                    else:
                        # 复检拉取失败：无法确认，保守 mismatch，同时尽力仲裁
                        _adjudicate(row, mkt)
                        row["status"] = "mismatch"
                        cnt["mismatch"] += 1
            rows.append(row)
    cnt["coverage"] = round(cnt["ok"] / cnt["total"], 3) if cnt["total"] else 0.0
    return {"rows": rows, "summary": cnt}


def validate_sanity(codes_by_market, div_yield_fn=None):
    """口径不统一的指标的合理性体检（如股息率）。

    货源间只是"交叉参考"：即便同标的不同源 DY 有 ±30% 差异也不必报错，
    仅当超出物理合理区间时给出告警，避免被源的口径/时点差异污染。
    """
    issues = []
    if not div_yield_fn:
        return issues
    for mkt, codes in (codes_by_market or {}).items():
        for c in codes:
            d = div_yield_fn(c, mkt)
            if d is None:
                continue
            if d < DY_LOWER or d > DY_UPPER:
                issues.append(f"[{mkt}] {c} 股息率 {d:.2f}% 超出合理区间 "
                              f"({DY_LOWER}–{DY_UPPER}%)")
    return issues
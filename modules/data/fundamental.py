"""财务/分红/负债/现金流/审计 — 同花顺THS主数据源"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import akshare as ak
import pandas as pd

from .cache import load, retry, save

logger = logging.getLogger(__name__)
_MAX_WORKERS = 8


def _pct(v):
    """解析百分比字符串 '15.90%' → 15.9，纯数字直接返回"""
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip().replace(",", "").replace("％", "%")
        if v.endswith("%"):
            try:
                return float(v[:-1])
            except ValueError:
                return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _is_bogus(info):
    """检查缓存数据是否全是 None（旧代码写入的无效缓存）"""
    for key in ("roe", "eps", "pe", "pb", "debt", "ocf_ratio"):
        if info.get(key) is not None:
            return False
    return True


def _safe_fhps(c):
    """分红明细：基金/REIT 等无股票分红数据的代码会让 akshare 抛
    TypeError/返回 None，此处统一兜底为空 DataFrame，避免 retry 5 次浪费。"""
    try:
        d = ak.stock_fhps_detail_em(symbol=c)
    except Exception:
        return pd.DataFrame()
    if d is None or d.empty:
        return pd.DataFrame()
    return d


def _safe_ths(c):
    """同花顺财务摘要：基金/REIT 无对应数据时 akshare 会抛
    AttributeError('NoneType' has no attribute 'string')，兜底为空 DataFrame。"""
    try:
        d = ak.stock_financial_abstract_ths(symbol=c)
    except Exception:
        return pd.DataFrame()
    if d is None or d.empty:
        return pd.DataFrame()
    return d


def _fetch_one_fin(c):
    k = str(c)
    df = load("fin", k)
    if df is not None:
        info_cached = df.iloc[0].to_dict()
        if not _is_bogus(info_cached):
            return c, info_cached
    df_expired = load("fin", k, allow_expired=True)
    if df_expired is not None:
        info_cached = df_expired.iloc[0].to_dict()
        if not _is_bogus(info_cached):
            return c, info_cached

    info = {"roe": None, "eps": None, "bps": None, "debt": None, "ocf_ratio": None, "audit": True, "div_years": 0, "total_shares": None}

    # ── 同花顺财务摘要（主数据源，EM阻断时依然可用） ──
    try:
        d = _safe_ths(c)
        if d is not None and not d.empty:
            d = d.sort_values("报告期", ascending=False)
            r = d.iloc[0]
            info["eps"] = _pct(r.get("基本每股收益"))
            info["bps"] = _pct(r.get("每股净资产"))
            info["debt"] = _pct(r.get("资产负债率"))
            # ROE/现金流: 优先用最新年报(12-31), 避免季报数据偏低
            for _, rr in d.iterrows():
                if str(rr.get("报告期", "")).endswith("12-31"):
                    info["roe"] = _pct(rr.get("净资产收益率"))
                    break
            if not info["roe"]:
                info["roe"] = _pct(r.get("净资产收益率"))
            cfps = _pct(r.get("每股经营现金流"))
            if info["eps"] and cfps and info["eps"] > 0:
                info["ocf_ratio"] = round(cfps / info["eps"] * 100, 1)
    except Exception as _e:
        logger.warning(f"[{__name__}] %s", _e, exc_info=True)

    # ── 分红年数 + 总股本（用于计算市值） ──
    try:
        d = _safe_fhps(c)
        if d is not None and not d.empty:
            d = d[d["除权除息日"].notna()].sort_values("除权除息日", ascending=False)
            yrs = 0
            for _, rr in d.iterrows():
                if rr.get("现金分红-现金分红比例", 0) > 0:
                    yrs += 1
                else:
                    break
            info["div_years"] = min(yrs, 10)
            shares = d.iloc[0].get("总股本")
            if shares and float(shares) > 0:
                info["total_shares"] = float(shares)
    except Exception as _e:
        logger.warning(f"[{__name__}] %s", _e, exc_info=True)

    save("fin", k, pd.DataFrame([info]))
    return c, info


def fetch_fins(codes):
    res = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        fut = {pool.submit(_fetch_one_fin, c): c for c in codes}
        for f in as_completed(fut):
            try:
                c, info = f.result(timeout=180)
                res[c] = info
            except Exception as _e:
                logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    return res


def _num(info, *keys):
    """从 yfinance info dict 取第一个非空数值。"""
    for k in keys:
        try:
            v = info.get(k)
            if v is not None:
                v = float(v)
                if v == v:  # not NaN
                    return v
        except (TypeError, ValueError):
            continue
    return None


def _fetch_one_fin_us(code):
    """美股单只基本面：yfinance info + cashflow，失败降级 FMP。

    返回 (code, {roe, eps, bps, debt, pe, pb, ocf_ratio, div_years, total_shares})。
    任一字段缺失保持 None（上层 _fmt 会写"数据不足"），不推断。
    """
    info = {"roe": None, "eps": None, "bps": None, "debt": None, "pe": None,
            "pb": None, "ocf_ratio": None, "audit": True, "div_years": 0,
            "total_shares": None}
    from modules.data.market_us import _get_yf
    yf = _get_yf()
    if yf:
        ticker = yf.Ticker(code)
        ti = {}
        # Yahoo 对数据中心 IP 常限流（YFRateLimitError）。此处快速重试 1 次即可：
        # 失败的请求不值得拖慢每次刷新，靠 1h 缓存 + 后续刷新自然解限后再取。
        for _attempt in range(2):
            try:
                ti = ticker.info or {}
                break
            except Exception:
                if _attempt == 0:
                    import time as _time
                    _time.sleep(1.0)
        if ti:
            info["pe"] = _num(ti, "trailingPE") or _num(ti, "forwardPE")
            info["pb"] = _num(ti, "priceToBook")
            roe = _num(ti, "returnOnEquity")
            if roe is not None:
                info["roe"] = round(roe * 100, 2)
            info["eps"] = _num(ti, "trailingEps")
            info["bps"] = _num(ti, "bookValue")
            info["total_shares"] = _num(ti, "sharesOutstanding")
            # 资产负债率：优先 D/E 换算 D/(D+E)；缺则留空
            de = _num(ti, "debtToEquity")
            if de is not None:
                de_pct = de if abs(de) > 1 else de * 100
                info["debt"] = round(de_pct / (de_pct + 100.0) * 100.0, 2) if (de_pct + 100.0) > 0 else None
            # 经营现金流 / 净利润
            try:
                cf = ticker.cashflow
                if cf is not None and not cf.empty:
                    try:
                        ocf = cf.loc["Operating Cash Flow"].iloc[0]
                        ni = cf.loc["Net Income"].iloc[0]
                        if ocf is not None and ni and float(ni) != 0:
                            info["ocf_ratio"] = round(float(ocf) / float(ni) * 100.0, 1)
                    except (KeyError, IndexError, TypeError, ValueError):
                        pass
            except Exception as _e:
                logger.warning(f"[{__name__}] %s", _e, exc_info=True)
            return code, info
        # yfinance 被限流(ti 为空)或现金流失败 → 降级 FMP
    try:
        from .fmp import fetch_us_finance
        fin = fetch_us_finance(code)
        for k in ("roe", "eps", "bps", "debt", "pe", "pb", "ocf_ratio", "total_shares"):
            if fin.get(k) is not None:
                info[k] = fin[k]
        if info.get("pe") or info.get("roe") or info.get("pb"):
            logger.info("[%s] 美股基本面降级 FMP (%s)", code,
                        fin.get("_source", "fmp"))
            return code, info
    except Exception as _e:
        logger.warning(f"[{__name__}] FMP 降级失败 {code}: %s", _e, exc_info=True)
    return code, info


def fetch_fins_us(codes):
    res = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        fut = {pool.submit(_fetch_one_fin_us, c): c for c in codes}
        for f in as_completed(fut):
            try:
                c, info = f.result(timeout=180)
                res[c] = info
            except Exception as _e:
                logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    return res




def fetch_divs(codes):
    r = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        fut = {pool.submit(_fetch_one_div, c): c for c in codes}
        for f in as_completed(fut):
            try:
                c, d = f.result(timeout=180)
                r[c] = d
            except Exception as _e:
                logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    return r




def fetch_divs_hk(codes):
    r = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        fut = {pool.submit(_fetch_one_div_hk, c): c for c in codes}
        for f in as_completed(fut):
            try:
                c, d = f.result(timeout=180)
                r[c] = d
            except Exception as _e:
                logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    return r


def _get_yf():
    try:
        import yfinance as yf
        return yf
    except ImportError:
        return None



def _av_used_today(idx):
    """AlphaVantage key idx 当日使用次数 (data/av_usage.jsonl)"""
    import os
    from datetime import date
    today = date.today().isoformat()
    n = 0
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "av_usage.jsonl")) as f:
            for line in f:
                if line.startswith(today + " " + str(idx)):
                    n += 1
    except FileNotFoundError:
        return 0
    except Exception:
        return 999
    return n


def _av_bump(idx):
    """记录一次 AlphaVantage key idx 使用"""
    import os
    from datetime import date
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "av_usage.jsonl"), "a") as f:
            f.write(date.today().isoformat() + " " + str(idx) + "\n")
    except Exception:
        pass


def _av_append_fallback(c, result):
    """美股分红最后兜底: AlphaVantage DIVIDENDS (腾讯+yfinance 均失败)。
    双 key 轮换, 每 key 每日<=25 次, 超出即停。"""
    import os
    from datetime import date, timedelta
    keys = [k for k in (os.environ.get("ALPHAVANTAGE_API_KEY1", "").strip(),
                        os.environ.get("ALPHAVANTAGE_API_KEY2", "").strip()) if k]
    if not keys or result.get("ttm"):
        return result
    import requests as _req
    for idx, key in enumerate(keys):
        if _av_used_today(idx) >= 25:
            continue
        try:
            r = _req.get("https://www.alphavantage.co/query",
                         params={"function": "DIVIDENDS", "symbol": c, "apikey": key},
                         timeout=12)
            _av_bump(idx)
            rows = (r.json() or {}).get("data") or []
            if not rows:
                continue
            cutoff = date.today() - timedelta(days=365)
            ttm = 0.0
            for row in rows:
                try:
                    ed = date.fromisoformat(row.get("ex_dividend_date", ""))
                except Exception:
                    continue
                if ed >= cutoff:
                    ttm += float(row.get("amount", 0) or 0)
            if ttm > 0:
                result["ttm"] = round(ttm, 4)
                result["lfy"] = round(ttm, 4)
            break
        except Exception as _e:
            logger.warning(f"[{__name__}] AlphaVantage 兜底失败 %s: %s", c, _e)
    return result
def _tx_us_quote(code):
    """腾讯美股实时报价 → 字段列表 或 None
    URL: http://qt.gtimg.cn/q=us{CODE}
    field[0]=状态, [1]=名称, [3]=最新价, [4]=昨收, [52]=股息率%, [66]=每股年分红
    """
    import re

    import requests
    try:
        r = requests.get(f"http://qt.gtimg.cn/q=us{code}", timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
        m = re.search(r'"(.+)"', r.text.strip())
        if not m:
            return None
        parts = m.group(1).split("~")
        if len(parts) < 67 or parts[0] != "200":
            return None
        return parts
    except Exception:
        return None


def _fetch_one_div_us(c):
    """美股每股分红 (腾讯接口主, yfinance 备)
    腾讯报价 field[66]=每股年分红, field[52]=股息率%
    返回 {"ttm": 每股分红, "lfy": 每股分红}，与 fetch_divs 口径一致。
    """
    k = f"{c}_div_us"
    df = load("div", k)
    if df is not None:
        row = df.iloc[0]
        if "ttm" in row.index and "lfy" in row.index:
            cached_ttm = float(row["ttm"])
            cached_lfy = float(row["lfy"])
            # 缓存值为 0 说明上次拉取失败，不信任缓存，重新拉取
            if cached_ttm > 0 or cached_lfy > 0:
                return c, {"ttm": cached_ttm, "lfy": cached_lfy}
    df_expired = load("div", k, allow_expired=True)
    if df_expired is not None:
        row = df_expired.iloc[0]
        if "ttm" in row.index and "lfy" in row.index:
            cached_ttm = float(row["ttm"])
            cached_lfy = float(row["lfy"])
            if cached_ttm > 0 or cached_lfy > 0:
                return c, {"ttm": cached_ttm, "lfy": cached_lfy}

    result = {"ttm": 0.0, "lfy": 0.0}

    # 主数据源：腾讯报价
    parts = _tx_us_quote(c)
    if parts:
        try:
            annual_div = float(parts[66] or 0)
            if annual_div > 0:
                result["ttm"] = round(annual_div, 4)
                result["lfy"] = round(annual_div, 4)
        except (ValueError, IndexError):
            pass

    # 备用：yfinance
    if result["ttm"] == 0:
        yf = _get_yf()
        if yf is not None:
            try:
                ticker = yf.Ticker(c)
                info = ticker.info or {}
                dy_ratio = info.get("dividend_yield")
                price = info.get("currentPrice") or info.get("regularMarketPrice") or 0.0
                if dy_ratio and price > 0:
                    per_share = float(dy_ratio) * float(price)
                    if per_share > 0:
                        result["ttm"] = round(per_share, 4)
                        result["lfy"] = round(per_share, 4)
            except Exception as _e:
                logger.warning(f"[{__name__}] %s", _e, exc_info=True)

    # 兜底2: Alpha Vantage (腾讯+yfinance 均失败且配额内时最后尝试)
    if result["ttm"] == 0:
        result = _av_append_fallback(c, result)
    try:
        save("div", k, pd.DataFrame([result]))
    except Exception as _e:
        logger.warning(f"[{__name__}] %s", _e, exc_info=True)  # 缓存失败不影响返回值
    return c, result


def fetch_divs_us(codes):
    """美股分红数据 (并行, 腾讯接口主数据源)"""
    r = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        fut = {pool.submit(_fetch_one_div_us, c): c for c in codes}
        for f in as_completed(fut):
            try:
                c, d = f.result(timeout=180)
                r[c] = d
            except Exception as _e:
                logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    return r


def _fetch_one_fin_hk(c):
    """获取单只港股财务数据 (东方财富港股财务指标)"""
    k = "hk_" + str(c)
    df = load("fin", k)
    if df is not None:
        info_cached = df.iloc[0].to_dict()
        if not _is_bogus(info_cached):
            return c, info_cached
    df_expired = load("fin", k, allow_expired=True)
    if df_expired is not None:
        info_cached = df_expired.iloc[0].to_dict()
        if not _is_bogus(info_cached):
            return c, info_cached

    info = {"roe": None, "eps": None, "bps": None, "debt": None, "pe": None, "pb": None,
            "ocf_ratio": None, "audit": True, "div_years": 0, "total_shares": None, "div_ttm": None}

    try:
        d = retry(lambda: ak.stock_hk_financial_indicator_em(symbol=c))
        if d is not None and not d.empty:
            r = d.iloc[0]
            info["eps"] = _pct(r.get("基本每股收益(元)"))
            info["bps"] = _pct(r.get("每股净资产(元)"))
            info["roe"] = _pct(r.get("股东权益回报率(%)"))
            info["pe"] = _pct(r.get("市盈率"))
            info["pb"] = _pct(r.get("市净率"))
            info["ocf_ratio"] = _pct(r.get("每股经营现金流(元)"))
            info["total_shares"] = _pct(r.get("已发行股本(股)"))
            # 股息: 从财务指标里拿每股股息TTM
            info["div_ttm"] = _pct(r.get("每股股息TTM(港元)"))
    except Exception as _e:
        logger.warning(f"[{__name__}] %s", _e, exc_info=True)

    # 尝试通过财务分析指标获取更多数据 (如负债率)
    try:
        d2 = retry(lambda: ak.stock_financial_hk_analysis_indicator_em(symbol=c, indicator="年度"))
        if d2 is not None and not d2.empty:
            for _, rr in d2.iterrows():
                debt_raw = rr.get("DEBT_ASSET_RATIO")
                if debt_raw is not None:
                    info["debt"] = float(debt_raw)
                    break
    except Exception as _e:
        logger.warning(f"[{__name__}] %s", _e, exc_info=True)

    # 分红数据 (使用已有的 fetch_divs_hk 逻辑的底层函数)
    try:
        df_ = retry(lambda: ak.stock_hk_dividend_payout_em(symbol=c))
        if df_ is not None and not df_.empty:
            df_ = df_[df_["除净日"].notna()].sort_values("除净日", ascending=False)
            yrs = 0
            for _, rr in df_.iterrows():
                try:
                    if float(rr.get("分红方案", "0").replace("每股派港币", "").replace("元", "")) > 0:
                        yrs += 1
                    else:
                        break
                except (ValueError, TypeError):
                    break
            info["div_years"] = min(yrs, 10)
            shares_raw = df_.iloc[0].get("总股本") if "总股本" in df_.columns else None
            if shares_raw and float(shares_raw) > 0:
                info["total_shares"] = float(shares_raw)
    except Exception as _e:
        logger.warning(f"[{__name__}] %s", _e, exc_info=True)

    save("fin", k, pd.DataFrame([info]))
    return c, info


def fetch_fins_hk(codes):
    """获取港股财务数据 (并行)"""
    res = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        fut = {pool.submit(_fetch_one_fin_hk, c): c for c in codes}
        for f in as_completed(fut):
            try:
                c, info = f.result(timeout=180)
                res[c] = info
            except Exception as _e:
                logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    return res

def _div_save_or_fallback(k, result):
    """统一分红防护：只有拿到真实正数才落盘；抓取失败/为空时回退
    上次有效缓存，避免用 0/0 覆盖好数据。A/HK/REIT 共用同一逻辑。"""
    if result["ttm"] > 0 or result["lfy"] > 0:
        save("div", k, pd.DataFrame([result]))
        return result
    df_expired = load("div", k, allow_expired=True)
    if df_expired is not None:
        row = df_expired.iloc[0]
        if "ttm" in row.index and "lfy" in row.index:
            old = {"ttm": float(row["ttm"]), "lfy": float(row["lfy"])}
            if old["ttm"] > 0 or old["lfy"] > 0:
                return old
    return result


def _fetch_one_div(c):
    k = f"{c}_div"
    df = load("div", k)
    if df is not None:
        row = df.iloc[0]
        if "ttm" in row.index and "lfy" in row.index:
            return c, {"ttm": float(row["ttm"]), "lfy": float(row["lfy"])}
    result = {"ttm": 0.0, "lfy": 0.0}
    try:
        df_ = _safe_fhps(c)
        if df_.empty or "除权除息日" not in df_.columns:
            raise ValueError("分红明细缺失或字段异常")
        df_ = df_[df_["除权除息日"].notna()].sort_values("除权除息日", ascending=False)
        if not df_.empty:
            now = pd.Timestamp.now()
            one_year_ago = now - pd.Timedelta(days=365)
            last_fy_year = now.year - 1
            has_fiscal_year = "分红年度" in df_.columns
            ttm_total = 0.0
            lfy_total = 0.0
            for _, rr in df_.iterrows():
                ex_date = pd.to_datetime(rr.get("除权除息日"))
                div_raw = float(rr.get("现金分红-现金分红比例", 0))
                div_ps = div_raw / 10.0
                if div_ps <= 0:
                    continue
                # TTM: 近12个月
                if ex_date >= one_year_ago:
                    ttm_total += div_ps
                # LFY: 上一个完整财年
                if has_fiscal_year:
                    fy = str(rr.get("分红年度", "")).strip()
                    if fy == str(last_fy_year):
                        lfy_total += div_ps
                else:
                    fy_start = pd.Timestamp(year=last_fy_year, month=1, day=1)
                    fy_end = pd.Timestamp(year=last_fy_year, month=12, day=31)
                    if fy_start <= ex_date <= fy_end:
                        lfy_total += div_ps
            result["ttm"] = round(ttm_total, 4)
            result["lfy"] = round(lfy_total, 4)
    except Exception as _e:
        logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    return c, _div_save_or_fallback(k, result)

def _fetch_one_div_hk(c):
    k = f"{c}_div_hk"
    df = load("div", k)
    if df is not None:
        row = df.iloc[0]
        if "ttm" in row.index and "lfy" in row.index:
            return c, {"ttm": float(row["ttm"]), "lfy": float(row["lfy"])}
    result = {"ttm": 0.0, "lfy": 0.0}
    try:
        df_ = retry(lambda: ak.stock_hk_dividend_payout_em(symbol=c))
        if df_.empty or "除净日" not in df_.columns:
            raise ValueError("分红明细缺失或字段异常")
        df_ = df_[df_["除净日"].notna()].sort_values("除净日", ascending=False)
        if not df_.empty:
            now = pd.Timestamp.now()
            one_year_ago = now - pd.Timedelta(days=365)
            last_fy_year = now.year - 1
            has_fiscal_year = "分红年度" in df_.columns
            ttm_total = 0.0
            lfy_total = 0.0
            for _, rr in df_.iterrows():
                ex_date = pd.to_datetime(rr.get("除净日"))
                div_raw = str(rr.get("分红方案", "0"))
                # 解析每股分红金额
                hkd_match = re.search(r'相当于港币([\d.]+)元', div_raw)
                if hkd_match:
                    div_ps = float(hkd_match.group(1))
                else:
                    num_match = re.search(r'派(?:港币|人民币|美元)([\d.]+)元', div_raw)
                    div_ps = float(num_match.group(1)) if num_match else 0.0
                if div_ps <= 0:
                    continue
                # TTM: 近12个月
                if ex_date >= one_year_ago:
                    ttm_total += div_ps
                # LFY: 上一个完整财年
                if has_fiscal_year:
                    fy = str(rr.get("分红年度", "")).strip()
                    if fy == str(last_fy_year):
                        lfy_total += div_ps
                else:
                    fy_start = pd.Timestamp(year=last_fy_year, month=1, day=1)
                    fy_end = pd.Timestamp(year=last_fy_year, month=12, day=31)
                    if fy_start <= ex_date <= fy_end:
                        lfy_total += div_ps
            result["ttm"] = round(ttm_total, 4)
            result["lfy"] = round(lfy_total, 4)
    except Exception as _e:
        logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    return c, _div_save_or_fallback(k, result)


_FUND_FH_CACHE = {}


def _fund_fh_year(yr):
    """基金分红全量表（每年一次，进程内共享，避免并行重复拉取）"""
    if yr not in _FUND_FH_CACHE:
        _FUND_FH_CACHE[yr] = retry(lambda: ak.fund_fh_em(year=yr), max_n=3)
    return _FUND_FH_CACHE[yr]


def _fetch_one_div_reit(c):
    """REIT 每股分红（基金分红接口 fund_fh_em，逐条按权益登记日归类）
    与 fetch_divs 口径一致，返回 {"ttm": 每股分红, "lfy": 每股分红}
    """
    k = f"{c}_div_reit"
    df = load("div", k)
    if df is not None:
        row = df.iloc[0]
        if "ttm" in row.index and "lfy" in row.index:
            return c, {"ttm": float(row["ttm"]), "lfy": float(row["lfy"])}
    result = {"ttm": 0.0, "lfy": 0.0}
    try:
        now = pd.Timestamp.now()
        one_year_ago = now - pd.Timedelta(days=365)
        last_fy_year = now.year - 1
        ttm_total = 0.0
        lfy_total = 0.0
        for yr in (str(now.year), str(last_fy_year)):
            d = _fund_fh_year(yr)
            if d is None or d.empty:
                continue
            col_code = "基金代码" if "基金代码" in d.columns else "代码"
            col_date = "权益登记日" if "权益登记日" in d.columns else "除息日"
            sub = d[d[col_code].astype(str) == str(c)].copy()
            if sub.empty:
                continue
            sub[col_date] = pd.to_datetime(sub[col_date], errors="coerce")
            sub["分红"] = pd.to_numeric(sub["分红"], errors="coerce")
            for _, rr in sub.iterrows():
                rec_date = pd.to_datetime(rr[col_date])
                if pd.isna(rec_date):
                    continue
                div_ps = float(rr["分红"]) if pd.notna(rr["分红"]) else 0.0
                if div_ps <= 0:
                    continue
                if rec_date >= one_year_ago:
                    ttm_total += div_ps
                if rec_date.year == last_fy_year:
                    lfy_total += div_ps
        result["ttm"] = round(ttm_total, 4)
        result["lfy"] = round(lfy_total, 4)
    except Exception as _e:
        logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    return c, _div_save_or_fallback(k, result)


def fetch_divs_reit(codes):
    """REIT 分红数据 (并行)"""
    r = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        fut = {pool.submit(_fetch_one_div_reit, c): c for c in codes}
        for f in as_completed(fut):
            try:
                c, d = f.result(timeout=180)
                r[c] = d
            except Exception as _e:
                logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    return r

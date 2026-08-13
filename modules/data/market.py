# market data utilities
import datetime
import logging
import re
import urllib.request

import akshare as ak
import numpy as np
import pandas as pd

from .cache import load, retry, save, validate_spot

logger = logging.getLogger(__name__)
SINA_URL = "https://hq.sinajs.cn/list="

TX_KLINE_URL = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="

TX_QUOTE_URL = "http://web.sqt.gtimg.cn/q="

SINA_MC_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getStock"



def _parse_sina_line(line):

    m = re.match(r'var hq_str_(\w+)="(.+)"', line.strip())

    if not m:

        return None

    code = m.group(1)

    parts = m.group(2).split(",")

    if len(parts) < 10:

        return None

    try:

        price = float(parts[2]) if parts[2] else 0.0

        pre_close = float(parts[3]) if parts[3] else 0.0

        chg = round((price - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0.0

        amount = float(parts[9]) if parts[9] else 0.0

    except (ValueError, IndexError):

        return None

    return {"c": code, "n": parts[0], "price": price, "chg": chg,

            "pre_close": pre_close, "amount": amount}



def _fetch_sina(codes):

    if isinstance(codes, (list, tuple)):

        code_str = ",".join(_sina_prefix(c) for c in codes)

    else:

        code_str = _sina_prefix(codes)

    req = urllib.request.Request(SINA_URL + code_str,

        headers={"Referer": "https://finance.sina.com.cn"})

    with urllib.request.urlopen(req, timeout=30) as r:

        text = r.read().decode("gbk")

    rows = []

    for line in text.strip().split("\n"):

        row = _parse_sina_line(line)

        if row:

            rows.append(row)

    df = pd.DataFrame(rows)

    if not df.empty:

        df["c"] = df["c"].str.replace(r"^sh|^sz|^rt_hk", "", regex=True)

    return df



def _sina_prefix(code):

    if code.startswith(("5", "6")):

        return "sh" + code

    if code.startswith(("0", "1", "2", "3")):

        return "sz" + code

    return code



def _tx_code(code):

    if code.startswith(("5", "6")):

        return "sh" + code

    if code.startswith("9"):

        return "bj" + code

    return "sz" + code



SINA_HK_URL = "https://hq.sinajs.cn/list="



def _parse_hk_sina_line(line):

    m = re.match(r'var hq_str_(\w+)="(.+)"', line.strip())

    if not m:

        return None

    code = m.group(1)

    parts = m.group(2).split(",")

    if len(parts) < 10:

        return None

    try:

        price = float(parts[2]) if parts[2] else 0.0

        pre_close = float(parts[3]) if parts[3] else 0.0

        chg = round((price - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0.0

        amount = float(parts[6]) if len(parts) > 6 and parts[6] else 0.0

    except (ValueError, IndexError):

        return None

    return {"c": code, "n": parts[0], "price": price, "chg": chg,

            "pre_close": pre_close, "amount": amount}



def _fetch_sina_hk(codes):

    code_str = ",".join("rt_hk" + c for c in codes)

    req = urllib.request.Request(SINA_HK_URL + code_str,

        headers={"Referer": "https://finance.sina.com.cn"})

    with urllib.request.urlopen(req, timeout=30) as r:

        text = r.read().decode("gbk")

    rows = []

    for line in text.strip().split("\n"):

        row = _parse_hk_sina_line(line)

        if row:

            rows.append(row)

    df = pd.DataFrame(rows)

    if not df.empty:

        df["c"] = df["c"].str.replace(r"^rt_hk", "", regex=True)

    return df



def _fetch_hk_index_history(symbol="hkHSI", days=1200):

    """港股指数收盘序列（腾讯 K 线，默认恒生指数）。

    返回 Series[date->close] 或 None。
    """
    import json
    url = TX_KLINE_URL + f"{symbol},day,,,{days},qfq"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
        data = json.loads(raw)
        items = data.get("data", {}).get(symbol, {}).get("day") or []
        if not items:
            return None
        df = pd.DataFrame([
            {"date": pd.to_datetime(it[0]), "close": float(it[1])} for it in items
        ])
        return df.set_index("date")["close"].astype(float).sort_index()
    except Exception as e:
        logger.warning("腾讯港股指数历史失败: %s", e)
        return None



# market data utilities

def _fetch_tx_quotes(codes):

    # market data utilities

    code_str = ",".join(_tx_code(c) for c in codes)

    url = TX_QUOTE_URL + code_str

    try:

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

        with urllib.request.urlopen(req, timeout=30) as r:

            text = r.read().decode("gbk")

    except Exception:

        return {}

    rows = {}

    for line in text.strip().split(";"):

        if not line.strip():

            continue

        parts = line.split("~")

        if len(parts) < 47:

            continue

        try:

            code = parts[2]

            price = float(parts[3]) if parts[3] else 0.0

            pre_close = float(parts[4]) if parts[4] else 0.0

            if price > 0 and pre_close > 0:
                chg = round((price - pre_close) / pre_close * 100, 2)
            else:
                chg = 0.0

            amount = float(parts[37]) * 10000 if len(parts) > 37 and parts[37] else 0.0

            pe = float(parts[39]) if len(parts) > 39 and parts[39] else None

            mcap = float(parts[44]) if len(parts) > 44 and parts[44] else None

            pb = float(parts[46]) if len(parts) > 46 and parts[46] else None

            rows[code] = {"price": price, "pre_close": pre_close, "chg": chg,

                          "amount": amount, "pe": pe, "pb": pb, "mcap": mcap}

        except (ValueError, IndexError):
            pass

    return rows



def _fetch_tx_histories(codes, days=500):

    # market data utilities

    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = {}



    def _one(code):

        try:

            url = TX_KLINE_URL + f"{_tx_code(code)},day,,,{days},qfq"

            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

            with urllib.request.urlopen(req, timeout=30) as r:

                raw = r.read().decode("utf-8")

            import json

            data = json.loads(raw)

            items = data.get("data", {}).get(_tx_code(code), {}).get("qfqday", [])

            if not items:

                return None

            rows = []

            for item in items:

                rows.append({"date": item[0], "open": float(item[1]),

                             "close": float(item[2]), "high": float(item[3]),

                             "low": float(item[4]), "volume": float(item[5]) * 100})

            df = pd.DataFrame(rows)

            df["date"] = pd.to_datetime(df["date"])

            return code, df

        except Exception as _e:
            logger.warning(f"[{code}] %s", _e, exc_info=True)
            return None



    with ThreadPoolExecutor(max_workers=6) as pool:

        futs = {pool.submit(_one, c): c for c in codes}

        for f in as_completed(futs):

            try:

                r = f.result(timeout=60)

                if r:

                    results[r[0]] = r[1]

            except Exception as _e:
                logger.warning(f"[{futs[f]}] %s", _e, exc_info=True)

    return results



def _fetch_sina_all_stocks():

    # market data utilities

    import json
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # market data utilities

    cached = load("spot", "a_list_sina")

    if cached is not None:

        return cached

    urls = []

    for pg in range(1, 100):

        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"

               f"Market_Center.getHQNodeData?page={pg}&num=100&sort=code&asc=0&node=hs_a")

        urls.append(url)

    stocks = []



    def _fetch(pg, url):

        try:

            req = urllib.request.Request(url,

                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})

            with urllib.request.urlopen(req, timeout=30) as r:

                text = r.read().decode("gbk")

            data = json.loads(text)

            if not isinstance(data, list) or not data:

                return pg, []

            items = []

            for item in data:

                c = item.get("code", "")

                n = item.get("name", "")

                if c and n:

                    items.append((c, n))

            return pg, items

        except Exception as _e:
            logger.warning(f"[{pg}] %s", _e, exc_info=True)

            return pg, []



    with ThreadPoolExecutor(max_workers=12) as pool:

        futs = {pool.submit(_fetch, pg, url): pg for pg, url in enumerate(urls)}

        for f in as_completed(futs):

            pg, items = f.result()

            stocks.extend(items)

    if stocks:

        df = pd.DataFrame(stocks, columns=["c", "n"])

        df = df.drop_duplicates("c")

        save("spot", "a_list_sina", df)

        return df

    return pd.DataFrame()



def _fetch_spot_tx_fallback(codes=None, min_amount=0):

    """

    pass

    pass

    """

    from concurrent.futures import ThreadPoolExecutor, as_completed

    df = _fetch_sina_all_stocks()

    if df.empty:

        return pd.DataFrame()

    all_codes = df["c"].tolist()

    # market data utilities

    batch_size = 200

    batches = [all_codes[i:i + batch_size] for i in range(0, len(all_codes), batch_size)]

    all_quotes = {}



    def _fetch_batch(batch):

        return _fetch_tx_quotes(batch)



    with ThreadPoolExecutor(max_workers=8) as pool:

        futs = {pool.submit(_fetch_batch, b): i for i, b in enumerate(batches)}

        for f in as_completed(futs):

            try:

                all_quotes.update(f.result())

            except Exception as _e:
                logger.warning(f"[{futs[f]}] %s", _e, exc_info=True)

    rows = []

    for _, r in df.iterrows():

        c = r["c"]

        q = all_quotes.get(c)

        if q is None:

            continue

        if q.get("amount", 0) < min_amount:

            continue

        rows.append({"c": c, "n": r["n"], "price": q["price"],

                      "chg": q["chg"], "pe": q["pe"], "pb": q["pb"],

                      "mcap": q["mcap"], "amount": q["amount"]})

    if not rows:

        return pd.DataFrame()

    res = pd.DataFrame(rows)

    res["_src"] = "tencent"

    return res



def _fetch_ths_one(code):

    """fetch single stock THS financial abstract (eps, bps)"""

    k = "ths_" + str(code)

    from .cache import load as _load_ths
    from .cache import save as _save_ths

    df = _load_ths("fin", k)

    if df is not None:

        return code, df.iloc[0].to_dict()

    try:

        d = ak.stock_financial_abstract_ths(symbol=code)

        if d is None or d.empty:

            return code, {}

        # Use column index instead of names to avoid encoding issues

        d = d.sort_values(d.columns[0], ascending=False)

        eps = None

        for _, r in d.iterrows():

            v = r.iloc[1] if len(r) > 1 else None  # EPS column

            if v is not None:

                try:

                    eps = float(str(v).strip().replace(",", ""))

                    if eps:

                        break

                except (ValueError, TypeError):
                    pass

        bps = None

        if len(d) > 0:

            v = d.iloc[0].iloc[2] if len(d.iloc[0]) > 2 else None  # BPS column

            if v is not None:

                try:

                    bps = float(str(v).strip().replace(",", ""))

                except (ValueError, TypeError):
                    pass

        result = {"eps": eps, "bps": bps}

        _save_ths("fin", k, pd.DataFrame([result]))

        return code, result

    except Exception:

        return code, {}



def _pct(v):

    """parse percentage value from various formats"""

    if v is None:

        return None

    if isinstance(v, str):

        v = v.strip().replace(",", "").replace("%", "")

    try:

        return float(v)

    except (ValueError, TypeError):

        return None



def _fetch_ths_pe_pb(codes):

    # market data utilities

    from concurrent.futures import ThreadPoolExecutor, as_completed

    fins = {}

    with ThreadPoolExecutor(max_workers=6) as pool:

        fut = {pool.submit(_fetch_ths_one, c): c for c in codes}

        for f in as_completed(fut):

            try:

                c = fut[f]

                r = f.result(timeout=30)

                if r:

                    fins[c] = r

            except Exception as _e:
                logger.warning(f"[{c}] %s", _e, exc_info=True)

    return fins





def _fetch_spot_fallback(fn_spot, codes):

    errs = []

    for name, fn in fn_spot:

        try:

            df = retry(fn, timeout=60)

            if df is not None and not df.empty:

                matched = df[df["c"].isin(codes)] if codes else df

                if not matched.empty:

                    return matched, name

                errs.append("%s: 无匹配行情" % name)

            else:

                errs.append("%s: 返回空数据" % name)

        except Exception as e:

            errs.append("%s: %s" % (name, e))

    raise ConnectionError("行情源全部失败: " + "; ".join(errs))





def fetch_spot_a(codes):

    df=load("spot","a_all")

    if df is not None:

        m=df[df["c"].isin(codes)].copy() if "c" in df.columns else df

        if not m.empty:
            px = pd.to_numeric(m["price"], errors="coerce").fillna(0) if "price" in m.columns else pd.Series(0, index=m.index)
            if (px > 0).all():
                return m
            logger.warning(f"[fetch_spot_a] 缓存含无效价格 {(px<=0).sum()}/{len(m)} 行，重新抓取")

    from modules.data.fallback import fetch_spot_with_fallback
    raw_df, src = fetch_spot_with_fallback(codes, mkt="A")
    if raw_df is None or raw_df.empty:
        raise ConnectionError("行情源全部失败: A股多级降级未返回数据")
    raw = raw_df.copy()


    df = raw.copy()



    df["pe"]=None; df["pb"]=None; df["mcap"]=None


    df["_src"]=src

    # market data utilities

    try:

        tx=_fetch_tx_quotes(list(codes))

        for idx,row in df.iterrows():

            c=row["c"]

            q=tx.get(c)

            if q:

                if q.get("pe") is not None:

                    df.at[idx,"pe"]=q["pe"]

                if q.get("pb") is not None:

                    df.at[idx,"pb"]=q["pb"]

                if q.get("mcap") is not None:

                    df.at[idx,"mcap"]=q["mcap"]

    except Exception as _e:
        logger.warning(f"[{codes}] %s", _e, exc_info=True)

    # market data utilities

    try:

        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import TimeoutError as _FutTimeout
        _ths_pool = ThreadPoolExecutor(max_workers=1)
        try:
            _ths_fut = _ths_pool.submit(_fetch_ths_pe_pb, list(codes))
            ths = _ths_fut.result(timeout=25)
        except _FutTimeout:
            ths = {}
        finally:
            _ths_pool.shutdown(wait=False, cancel_futures=True)

    except Exception:
        ths = {}

        for idx,row in df.iterrows():

            c=row["c"]

            price=row["price"]

            if c in ths and price and price>0:

                fi=ths[c]

                if (df.at[idx,"pe"] is None or df.at[idx,"pe"]<=0) and fi.get("eps") and fi["eps"]>0:

                    df.at[idx,"pe"]=round(price/fi["eps"], 2)

                if (df.at[idx,"pb"] is None or df.at[idx,"pb"]<=0) and fi.get("bps") and fi["bps"]>0:

                    df.at[idx,"pb"]=round(price/fi["bps"], 2)

    except Exception as _e:
        logger.warning(f"[{codes}] %s", _e, exc_info=True)

    df=validate_spot(df)

    px = pd.to_numeric(df["price"], errors="coerce").fillna(0)
    df = df[px > 0]
    if df.empty:
        raise ConnectionError("A股行情全部无有效价格，保留旧缓存")
    save("spot","a_all",df)

    return df



def fetch_spot_hk(codes):

    df=load("spot","hk_all")

    if df is not None:

        m=df[df["c"].isin(codes)].copy()

        if not m.empty:
            px = pd.to_numeric(m["price"], errors="coerce").fillna(0) if "price" in m.columns else pd.Series(0, index=m.index)
            if (px > 0).all():
                return m
            logger.warning(f"[fetch_spot_hk] 缓存含无效价格 {(px<=0).sum()}/{len(m)} 行，重新抓取")

    from modules.data.fallback import fetch_spot_with_fallback
    raw_df, src = fetch_spot_with_fallback(codes, mkt="HK")
    if raw_df is None or raw_df.empty:
        raise ConnectionError("行情源全部失败: 港股多级降级未返回数据")
    raw = raw_df.copy()

    df = raw.copy()

    df["pe"]=None; df["pb"]=None; df["mcap"]=None

    px = pd.to_numeric(df["price"], errors="coerce").fillna(0)
    df = df[px > 0]
    if df.empty:
        raise ConnectionError("港股行情全部无有效价格，保留旧缓存")
    save("spot","hk_all",df); return df



def fetch_spot_a_all():
    """full A-share market list"""
    from .cache import load as _load_f
    from .cache import save as _save_f
    df = _load_f("spot", "a_all_full")
    if df is not None:
        return df
    try:
        df = ak.stock_zh_a_spot_em()
        # rename columns by position (avoid encoding issues with Chinese names)
        cols = df.columns.tolist()
        rename = {}
        for old_c in cols:
            c = str(old_c).strip()
            if c.endswith("\u7801") or "code" in c.lower():
                rename[old_c] = "c"
            elif c.endswith("\u79f0") or "name" in c.lower():
                rename[old_c] = "n"
            elif c.endswith("\u4ef7") or "price" in c.lower():
                rename[old_c] = "price"
            elif c.endswith("\u5e45") or "chg" in c.lower():
                rename[old_c] = "chg"
            elif c.endswith("\u6001)") or "pe" in c.lower():
                rename[old_c] = "pe"
            elif c.endswith("\u7387") or "pb" in c.lower():
                rename[old_c] = "pb"
            elif c.endswith("\u503c") or "mcap" in c.lower():
                rename[old_c] = "mcap"
            elif c.endswith("\u989d") or "amount" in c.lower():
                rename[old_c] = "amount"
        df = df.rename(columns=rename)
        for col in ["price", "chg", "amount", "pe", "pb", "mcap"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "mcap" in df.columns:
            df["mcap"] = df["mcap"] / 1e8
        if "n" in df.columns:
            df = df[~df["n"].str.contains("ST|\u9000|N", na=False)]
        df = df[df["price"] > 0]
        if "amount" in df.columns:
            df = df[df["amount"] > 50_000_000]
        _save_f("spot", "a_all_full", df)
        return df
    except Exception as _e:
        logger.warning(f"[fetch_spot_a_all] %s", _e, exc_info=True)
    try:
        df = _fetch_spot_tx_fallback(min_amount=50_000_000)
        if not df.empty and "n" in df.columns:
            df = df[~df["n"].str.contains("ST|\u9000|N", na=False)]
            df = df[df["price"] > 0]
            _save_f("spot", "a_all_full", df)
        return df
    except Exception as _e:
        logger.warning(f"[fetch_spot_a_all] %s", _e, exc_info=True)
    return pd.DataFrame()

def fetch_spot_a_filtered(amount_min=50_000_000):

    df=fetch_spot_a_all()

    return df[df["amount"]>=amount_min] if "amount" in df.columns else df



def _fetch_hist_em_or_tx(code, start, end):

    """"""

    try:

        d = retry(lambda: ak.stock_zh_a_hist(symbol=code, period="daily",

                   start_date=start, end_date=end, adjust="qfq"))

        if d is not None and not d.empty:

            d = d.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",

                                   "最高": "high", "最低": "low", "成交量": "volume"})

            d["date"] = pd.to_datetime(d["date"])

            return d

    except Exception as _e:
        logger.warning(f"[{code}] %s", _e, exc_info=True)

    # market data utilities

    try:

        tx = _fetch_tx_histories([code], days=1000)

        df = tx.get(code)

        if df is not None and not df.empty:

            s = pd.to_datetime(start)

            e = pd.to_datetime(end)

            return df[(df["date"] >= s) & (df["date"] <= e)]

    except Exception as _e:
        logger.warning(f"[{code}] %s", _e, exc_info=True)

    return None



def fetch_vol(code, mkt="A"):

    k=f"{code}_vol"; df=load("vol",k)

    if df is not None: return float(df["v"].iloc[0])

    end=datetime.date.today(); start=end-datetime.timedelta(days=120)

    try:

        if mkt=="A":

            d=_fetch_hist_em_or_tx(code, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))

        elif mkt=="US":

            from modules.data.market_us import fetch_us_hist

            d=fetch_us_hist(code, start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))

        else:

            d=retry(lambda: ak.stock_hk_hist(symbol=code,period="daily",

                start_date=start.strftime("%Y%m%d"),end_date=end.strftime("%Y%m%d"),adjust="qfq"))

        if d is not None and not d.empty:

            cls=d["close"].values if "close" in d.columns else d["收盘"].values

            if len(cls)>10:

                v=float(np.std(np.log(cls[1:]/cls[:-1]))*np.sqrt(252))

                if v>0: save("vol",k,pd.DataFrame([{"v":v}])); return v

    except Exception as _e:
        logger.warning(f"[{code}] %s", _e, exc_info=True)
    # 港美股波动率备援：主数据源(akshare/美股接口)失败时用长桥日K计算真实已实现波动率，
    # 避免回落 20% 占位波动率导致蒙特卡洛回撤被判定"数据不足"。
    if mkt in ("H", "HK", "US"):
        try:
            from modules.data.longbridge import fetch_hist as _lb_fetch_hist
            _d = _lb_fetch_hist(code, days=120)
            if _d is not None and not _d.empty and "close" in _d.columns:
                _cls = _d["close"].to_numpy(dtype=float)
                if len(_cls) > 10:
                    _v = float(np.std(np.log(_cls[1:] / _cls[:-1])) * np.sqrt(252))
                    if _v > 0:
                        save("vol", k, pd.DataFrame([{"v": _v}]))
                        return _v
        except Exception as _e:
            logger.warning(f"[{code}] %s", _e, exc_info=True)
    return 0.2



def fetch_listing_days(code, mkt="A"):

    k=f"{code}_list"; df=load("listing",k)

    if df is not None: return int(df["days"].iloc[0])

    end=datetime.date.today()

    try:

        if mkt=="A":

            d=_fetch_hist_em_or_tx(code, "20000101", end.strftime("%Y%m%d"))

        else:

            d=retry(lambda: ak.stock_hk_hist(symbol=code,period="daily",

                start_date="20000101",end_date=end.strftime("%Y%m%d"),adjust="qfq"))

        if d is not None and not d.empty:

            first_date=d["date"].iloc[0] if "date" in d.columns else d["鏃ユ湡"].iloc[0]

            if isinstance(first_date, str):

                first_date = datetime.datetime.strptime(first_date, "%Y-%m-%d")

            days=(end - first_date.date()).days if hasattr(first_date, "date") else (end - first_date).days

            if days>0: save("listing",k,pd.DataFrame([{"days":days}])); return days

    except Exception as _e:
        logger.warning(f"[{code}] %s", _e, exc_info=True)
    return 9999



def fetch_sma(code, period=20, mkt="A"):

    k=f"{code}_sma{period}"; df=load("sma",k)

    if df is not None: return float(df["sma"].iloc[0])

    end=datetime.date.today(); start=end-datetime.timedelta(days=period*2)

    try:

        if mkt=="A":

            d=_fetch_hist_em_or_tx(code, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))

        elif mkt=="US":

            from modules.data.market_us import fetch_us_hist

            d=fetch_us_hist(code, start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))

        else:

            d=retry(lambda: ak.stock_hk_hist(symbol=code,period="daily",

                start_date=start.strftime("%Y%m%d"),end_date=end.strftime("%Y%m%d"),adjust="qfq"))

        if d is not None and not d.empty and len(d)>=period:

            cls=d["close"].values if "close" in d.columns else d["收盘"].values

            sma=float(cls[-period:].mean())

            if sma>0: save("sma",k,pd.DataFrame([{"sma":sma}])); return sma

    except Exception as _e:
        logger.warning(f"[{code}] %s", _e, exc_info=True)
    return None



def fetch_price_percentile(code, years=5, mkt="A"):

    k=f"{code}_pctl{years}"; df=load("pctl",k)

    if df is not None: return float(df["pctl"].iloc[0])

    end=datetime.date.today(); start=end-datetime.timedelta(days=years*365)

    try:

        if mkt=="A":

            d=_fetch_hist_em_or_tx(code, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))

        elif mkt=="US":

            from modules.data.market_us import fetch_us_hist

            d=fetch_us_hist(code, start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))

        else:

            d=retry(lambda: ak.stock_hk_hist(symbol=code,period="daily",

                start_date=start.strftime("%Y%m%d"),end_date=end.strftime("%Y%m%d"),adjust="qfq"))

        if d is not None and not d.empty and len(d)>20:

            prices=d["close"].values if "close" in d.columns else d["收盘"].values

            cur=prices[-1]

            pctl=float(np.sum(prices<=cur)/len(prices)*100)

            save("pctl",k,pd.DataFrame([{"pctl":pctl}])); return pctl

    except Exception as _e:
        logger.warning(f"[{code}] %s", _e, exc_info=True)
    # 失败返回 None: 让调用方区分"无数据"与真实分位, 美股可回退手工成长因子表
    return None



def fetch_vols(codes, mkt="A"):

    from concurrent.futures import ThreadPoolExecutor, as_completed

    r = {}

    with ThreadPoolExecutor(max_workers=8) as pool:

        futs = {pool.submit(fetch_vol, c, mkt): c for c in codes}

        for f in as_completed(futs):

            try:

                c = futs[f]

                v = f.result()

                if v is not None:

                    r[c] = v

            except Exception as _e:
                logger.warning(f"[{c}] %s", _e, exc_info=True)

    return r



def fetch_smas(codes, period=20, mkt="A"):

    from concurrent.futures import ThreadPoolExecutor, as_completed

    r = {}

    with ThreadPoolExecutor(max_workers=8) as pool:

        futs = {pool.submit(fetch_sma, c, period, mkt): c for c in codes}

        for f in as_completed(futs):

            try:

                c = futs[f]

                v = f.result()

                if v is not None:

                    r[c] = v

            except Exception as _e:
                logger.warning(f"[{c}] %s", _e, exc_info=True)

    return r



def fetch_price_percentiles(codes, years=5, mkt="A"):

    from concurrent.futures import ThreadPoolExecutor, as_completed

    r = {}

    with ThreadPoolExecutor(max_workers=8) as pool:

        futs = {pool.submit(fetch_price_percentile, c, years, mkt): c for c in codes}

        for f in as_completed(futs):

            try:

                c = futs[f]

                v = f.result()

                if v is not None:

                    r[c] = v

            except Exception as _e:
                logger.warning(f"[{c}] %s", _e, exc_info=True)

    return r

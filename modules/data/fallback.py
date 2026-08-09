"""数据源多级降级模块
参考 EasyXT 的数据源自动降级机制：
AKShare → 腾讯行情 → Baostock → 东方财富
"""
import logging
import datetime, logging
import pandas as pd
from .cache import load, save, retry

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BAOSTOCK_AVAILABLE = False
try:
    import baostock as bs
    BAOSTOCK_AVAILABLE = True
except ImportError:
    BAOSTOCK_AVAILABLE = False

def _fetch_baostock_spot(codes):
    if not BAOSTOCK_AVAILABLE:
        return None
    try:
        lg = bs.login()
        if lg.error_code != '0':
            bs.logout()
            return None
        
        rows = []
        for code in codes:
            if code.startswith('6'):
                bs_code = f"sh.{code}"
            else:
                bs_code = f"sz.{code}"
            
            rs = bs.query_history_k_data_plus(
                bs_code, "date,code,close,peTTM,pbMRQ,totalShares",
                start_date=datetime.date.today().strftime("%Y-%m-%d"),
                end_date=datetime.date.today().strftime("%Y-%m-%d"),
                frequency="d", adjustflag="3"
            )
            
            if rs.error_code == '0':
                data_list = []
                while rs.next():
                    data_list.append(rs.get_row_data())
                if data_list:
                    d = data_list[0]
                    rows.append({
                        "c": code,
                        "n": "",
                        "price": float(d[2]) if d[2] else 0.0,
                        "chg": 0.0,
                        "pe": float(d[3]) if d[3] else None,
                        "pb": float(d[4]) if d[4] else None,
                        "mcap": float(d[5]) * float(d[2]) / 1e8 if d[5] and d[2] else None,
                        "amount": 0.0
                    })
        
        bs.logout()
        if rows:
            df = pd.DataFrame(rows)
            df["_src"] = "baostock"
            return df
        return None
    except Exception as e:
        logger.warning(f"Baostock fetch failed: {e}")
        try:
            bs.logout()
        except Exception as _e:
            logger.warning(f"[{__name__}] %s", _e, exc_info=True)
        return None

def _fetch_eastmoney_spot(codes):
    """东方财富A股行情兜底。字段码与刻度见 modules/data/fields.py。"""
    import urllib.request, json
    from modules.data.fields import EASTMONEY, eastmoney_secid, decode_eastmoney_price
    spec = EASTMONEY["A"]["spec"]
    try:
        url = "http://push2.eastmoney.com/api/qt/stock/get"
        rows = []
        for code in codes:
            secid = eastmoney_secid("A", code)
            req = urllib.request.Request(
                f"{url}?secid={secid}&fields={spec.code_field},{spec.name_field},"
                f"{spec.price_field},{spec.prev_field},{spec.mcap_field},"
                f"{spec.amount_field},{spec.pe_field},{spec.pb_field},{spec.chg_field}",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
                d = data.get("data", {})
                if d:
                    price = decode_eastmoney_price("A", d.get(spec.price_field))
                    prev = decode_eastmoney_price("A", d.get(spec.prev_field))
                    if price > 0 and prev > 0:
                        chg = round((price - prev) / prev * 100, 2)
                    else:
                        chg = float(d.get(spec.chg_field) or 0) / 100.0
                    pe = float(d.get(spec.pe_field) or 0) / 10.0
                    pb = float(d.get(spec.pb_field) or 0) / 100.0
                    rows.append({
                        "c": str(d.get(spec.code_field) or code).replace("SH", "").replace("SZ", ""),
                        "n": d.get(spec.name_field, ""),
                        "price": price,
                        "chg": chg,
                        "pe": pe if pe > 0 else None,
                        "pb": pb if pb > 0 else None,
                        "mcap": float(d.get(spec.mcap_field) or 0) / 1e8 if d.get(spec.mcap_field) else None,
                        "amount": float(d.get(spec.amount_field) or 0)
                    })

        if rows:
            df = pd.DataFrame(rows)
            df["_src"] = "eastmoney"
            return df
        return None
    except Exception as e:
        logger.warning(f"Eastmoney fetch failed: {e}")
        return None

def _lb_spot_df(codes):
    """长桥行情 → DataFrame, 失败返回 None。"""
    from .longbridge import fetch_spot as _lb_fetch
    rows = _lb_fetch(codes)
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df["_src"] = "longbridge"
    return df


def _us_spot_df(codes):
    """美股行情(腾讯实时优先) → DataFrame, 失败返回 None。"""
    from .market_us import fetch_us_spot
    df = fetch_us_spot(codes)
    if df is None or df.empty:
        return None
    df = df.copy()
    df["_src"] = "tencent_us"
    return df


def fetch_spot_with_fallback(codes, mkt="A"):
    """
    多级降级获取行情数据
    顺序: 长桥 → 东财 → 新浪 → 腾讯 (A股)
          长桥 → 新浪HK → 腾讯 (港股)
          长桥 → 腾讯美股 → yahoo (美股)

    与前几版不同: 不"先到先得", 而是多源合并补齐——
    主源返回后仍有缺口(如长桥缺REIT)时继续用后续源补足,
    直到覆盖全部 codes 或源耗尽, 避免部分标的选不到行情。

    Args:
        codes: 股票代码列表
        mkt: "A" / "HK" / "US"

    Returns:
        (DataFrame, source_name)。source 为贡献代码最多的源名, 用于日志与校验。
    """
    from .market import _fetch_sina, _fetch_sina_hk, _fetch_spot_tx_fallback

    if mkt == "A":
        sources = [
            ("longbridge", lambda: _lb_spot_df(codes), 1),
            ("eastmoney", lambda: _fetch_eastmoney_spot(codes), 5),
            ("sina", lambda: _fetch_sina(codes), 5),
            ("tencent", lambda: _fetch_spot_tx_fallback(codes=codes), 5),
        ]
    elif mkt == "HK":
        sources = [
            ("longbridge", lambda: _lb_spot_df(codes), 1),
            ("sina_hk", lambda: _fetch_sina_hk(codes), 5),
            ("tencent", lambda: _fetch_spot_tx_fallback(codes=codes), 5),
        ]
    elif mkt == "US":
        sources = [
            ("longbridge", lambda: _lb_spot_df(codes), 1),
            ("tencent_us", lambda: _us_spot_df(codes), 5),
        ]
    else:
        raise ValueError(f"未知市场: {mkt}")

    want = {str(c) for c in codes} if codes else None
    result = {}
    src_usage = {}
    errs = []
    for name, fn, nretry in sources:
        try:
            df = retry(fn, max_n=nretry)
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                c = str(row.get("c"))
                if c in result or c not in want:
                    continue
                result[c] = row
                src_usage[c] = name
            if want is None or len(result) >= len(want):
                break
        except Exception as e:
            errs.append(f"{name}: {str(e)[:60]}")
            logger.warning(f"Source {name} failed: {e}")

    if not result:
        logger.error(f"All sources failed: {'; '.join(errs)}")
        return None, errs[0].split(":")[0] if errs else "none"

    out = pd.DataFrame(list(result.values()))
    usage = list(src_usage.values())
    primary = max(set(usage), key=usage.count)
    out["_src"] = out["c"].map(src_usage)
    missing = sorted(want - set(src_usage.keys())) if want is not None else []
    if missing:
        logger.info(f"fallback 仍缺 {len(missing)} 只: {missing}, 主源={primary}")
    return out.reset_index(drop=True), primary

def fetch_hist_with_fallback(code, start_date, end_date, mkt="A"):
    """
    多级降级获取历史K线数据
    
    Args:
        code: 股票代码
        start_date: 开始日期 "YYYYMMDD"
        end_date: 结束日期 "YYYYMMDD"
        mkt: "A" 或 "HK"
    
    Returns:
        DataFrame 或 None
    """
    import akshare as ak
    from .market import _fetch_tx_histories, _fetch_hist_em_or_tx
    
    sources = [
        ("akshare_em", lambda: _fetch_hist_em_or_tx(code, start_date, end_date)),
        ("tencent_kline", lambda: _fetch_tx_histories([code], days=1000).get(code)),
    ]
    
    if BAOSTOCK_AVAILABLE and mkt == "A":
        def _bs_fetch():
            try:
                lg = bs.login()
                if lg.error_code != '0':
                    bs.logout()
                    return None
                
                bs_code = f"sh.{code}" if code.startswith('6') else f"sz.{code}"
                rs = bs.query_history_k_data_plus(
                    bs_code, "date,open,high,low,close,volume",
                    start_date=start_date, end_date=end_date,
                    frequency="d", adjustflag="3"
                )
                
                if rs.error_code == '0':
                    data_list = []
                    while rs.next():
                        data_list.append(rs.get_row_data())
                    bs.logout()
                    if data_list:
                        df = pd.DataFrame(data_list, columns=["date", "open", "high", "low", "close", "volume"])
                        df["date"] = pd.to_datetime(df["date"])
                        for col in ["open", "high", "low", "close", "volume"]:
                            df[col] = pd.to_numeric(df[col], errors="coerce")
                        return df
                bs.logout()
                return None
            except Exception:
                try:
                    bs.logout()
                except Exception as _e:
                    logger.warning(f"[{__name__}] %s", _e, exc_info=True)
                return None
        
        sources.append(("baostock", _bs_fetch))
    
    for name, fn in sources:
        try:
            df = retry(fn)
            if df is not None and not df.empty:
                logger.info(f"Successfully fetched history from {name}")
                return df
        except Exception as e:
            logger.warning(f"History source {name} failed: {e}")
    
    return None

def fetch_full_market_with_fallback(amount_min=50_000_000):
    """多级降级获取全市场A股数据"""
    import akshare as ak
    from .market import _fetch_spot_tx_fallback, _fetch_sina_all_stocks
    
    sources = [
        ("akshare_em", lambda: ak.stock_zh_a_spot_em()),
        ("tencent_full", lambda: _fetch_spot_tx_fallback(min_amount=amount_min)),
    ]
    
    for name, fn in sources:
        try:
            df = retry(fn)
            if df is not None and not df.empty:
                if name == "akshare_em":
                    df = df.rename(columns={
                        "代码":"c","名称":"n","最新价":"price","涨跌幅":"chg",
                        "市盈率-动态":"pe","市净率":"pb","总市值":"mcap","成交额":"amount"
                    })
                    for col in ["price","chg","amount","pe","pb","mcap"]:
                        if col in df.columns:
                            df[col] = pd.to_numeric(df[col], errors="coerce")
                    if "mcap" in df.columns:
                        df["mcap"] = df["mcap"] / 1e8
                df = df[~df["n"].str.contains("ST|退|N", na=False)]
                df = df[df["price"] > 0]
                if "amount" in df.columns:
                    df = df[df["amount"] >= amount_min]
                df["_src"] = name
                logger.info(f"Full market data from {name}, {len(df)} stocks")
                return df
        except Exception as e:
            logger.warning(f"Full market source {name} failed: {e}")
    
    return pd.DataFrame()

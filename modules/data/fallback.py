"""数据源多级降级模块
参考 EasyXT 的数据源自动降级机制：
AKShare → 腾讯行情 → Baostock → 东方财富
"""
import datetime, logging
import pandas as pd
from .cache import load, save, retry

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
        except Exception:
            pass
        return None

def _fetch_eastmoney_spot(codes):
    import urllib.request, json
    try:
        url = "http://push2.eastmoney.com/api/qt/stock/get"
        rows = []
        for code in codes:
            if code.startswith('6'):
                secid = f"1.{code}"
            else:
                secid = f"0.{code}"
            
            req = urllib.request.Request(
                f"{url}?secid={secid}&fields=f57,f58,f116,f117,f152,f167,f168,f170,f184",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
                d = data.get("data", {})
                if d:
                    rows.append({
                        "c": d.get("f57", "").replace("SH", "").replace("SZ", ""),
                        "n": d.get("f58", ""),
                        "price": float(d.get("f116", 0)),
                        "chg": float(d.get("f117", 0)),
                        "pe": float(d.get("f167", 0)) if d.get("f167") else None,
                        "pb": float(d.get("f168", 0)) if d.get("f168") else None,
                        "mcap": float(d.get("f170", 0)) / 1e8 if d.get("f170") else None,
                        "amount": float(d.get("f184", 0)) / 1e4 if d.get("f184") else 0.0
                    })
        
        if rows:
            df = pd.DataFrame(rows)
            df["_src"] = "eastmoney"
            return df
        return None
    except Exception as e:
        logger.warning(f"Eastmoney fetch failed: {e}")
        return None

def fetch_spot_with_fallback(codes, mkt="A"):
    """
    多级降级获取行情数据
    顺序：AKShare → 腾讯 → Baostock → 东方财富
    
    Args:
        codes: 股票代码列表
        mkt: "A" 或 "HK"
    
    Returns:
        (DataFrame, source_name) 或 (None, None)
    """
    from .market import _fetch_sina, _fetch_sina_hk, _fetch_tx_quotes, _fetch_spot_tx_fallback
    
    sources = []
    
    if mkt == "A":
        sources = [
            ("sina", lambda: _fetch_sina(codes)),
            ("tencent", lambda: _fetch_spot_tx_fallback(codes=codes)),
            ("baostock", lambda: _fetch_baostock_spot(codes)),
            ("eastmoney", lambda: _fetch_eastmoney_spot(codes)),
        ]
    else:
        sources = [
            ("sina_hk", lambda: _fetch_sina_hk(codes)),
            ("tencent", lambda: _fetch_spot_tx_fallback(codes=codes)),
        ]
    
    errs = []
    for name, fn in sources:
        try:
            df = retry(fn)
            if df is not None and not df.empty:
                matched = df[df["c"].isin(codes)] if codes else df
                if not matched.empty:
                    logger.info(f"Successfully fetched data from {name}")
                    return matched, name
        except Exception as e:
            errs.append(f"{name}: {str(e)[:50]}")
            logger.warning(f"Source {name} failed: {e}")
    
    logger.error(f"All sources failed: {'; '.join(errs)}")
    return None, None

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
                except Exception:
                    pass
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

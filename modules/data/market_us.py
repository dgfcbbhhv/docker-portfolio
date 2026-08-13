"""美股行情数据获取 (akshare 主 + yfinance 备)
提供 AAPL/NVDA/MSFT 等美股的历史日线、实时行情

akshare 自带无需额外安装;
yfinance 懒加载——Docker 未安装时自动降级
"""
import datetime
import logging

import pandas as pd

logger = logging.getLogger(__name__)

_US_PREFIX = {s: "105."+s for s in ["AAPL","NVDA","MSFT","GOOGL","AMZN","META","TSLA","GOOG"]}

def _ak_symbol(code):
    if "." in code:
        return code
    cu = code.upper().strip()
    if cu in _US_PREFIX:
        return _US_PREFIX[cu]
    return "105." + cu

def _get_yf():
    try:
        import yfinance as yf
        return yf
    except ImportError:
        return None


def _tx_us_quote(code):
    """腾讯美股实时报价 → 字段列表 或 None
    field[0]=状态, [1]=名称, [3]=最新价, [4]=昨收, [39]=PE, [47]=PB(市净率),
    [52]=股息率%, [66]=每股年分红
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
        if len(parts) < 40 or parts[0] != "200":
            return None
        return parts
    except Exception:
        return None


def _tx_us_kline(code, start=None, end=None):
    """腾讯美股日 K 线兜底（HTTPS，快速可用）。

    返回 DataFrame[date,open,high,low,close,volume] 或 None。
    """

    import requests
    if start is None or end is None:
        return None
    # 腾讯 K 线需要带交易所后缀的代码 (NASDAQ=.OQ, NYSE=.N), 裸代码会返回残缺数据。
    # 依次尝试 .OQ → .N → 裸代码, 取范围内行数最多且 >20 的结果。
    candidates = ["us%s.OQ" % code, "us%s.N" % code, "us%s" % code]
    best = None
    for sym in candidates:
        try:
            url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
                   "param=%s,day,,,1500,qfq" % sym)
            r = requests.get(url, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0",
                                      "Referer": "https://gu.qq.com/"})
            d = r.json()
            node = d.get("data", {}).get(sym, {})
            rows = node.get("day") or node.get("qfqday") or []
            if not rows:
                continue
            res = []
            for row in rows:
                # [日期, 开, 收, 高, 低, 量]
                if len(row) < 6:
                    continue
                res.append({"date": row[0], "open": float(row[1]),
                            "close": float(row[2]), "high": float(row[3]),
                            "low": float(row[4]), "volume": float(row[5])})
            if not res:
                continue
            df = pd.DataFrame(res)
            df = df[df["date"].between(start, end)].reset_index(drop=True)
            if len(df) > 20 and (best is None or len(df) > len(best)):
                best = df
        except Exception as e:
            logger.warning("腾讯 %s 历史数据失败 (%s): %s", code, sym, e)
    return best


def fetch_us_hist(code, start=None, end=None, days=750):
    if start is None:
        end_d = datetime.date.today()
        start_d = end_d - datetime.timedelta(days=days)
        start = start_d.strftime("%Y-%m-%d")
    if end is None:
        end = datetime.date.today().strftime("%Y-%m-%d")
    # 腾讯 K 线优先：内网/国内网络最快最稳
    df_tx = _tx_us_kline(code, start, end)
    if df_tx is not None and not df_tx.empty:
        return df_tx
    try:
        import akshare as ak
        symbol = _ak_symbol(code)
        df = ak.stock_us_hist(symbol=symbol, period="daily",
                              start_date=start.replace("-",""),
                              end_date=end.replace("-",""), adjust="qfq")
        if df is not None and not df.empty:
            df = df.rename(columns={"日期":"date","开盘":"open","收盘":"close","最高":"high","最低":"low","成交量":"volume"})
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            return df[["date","open","high","low","close","volume"]]
    except Exception as e:
        logger.warning("akshare %s 历史数据失败: %s", code, e)
    # 长桥 OpenAPI K线备援（美/港/A股统一）
    try:
        from .longbridge import fetch_hist as _lb_hist
        df_lb = _lb_hist(code, start, end, days=days)
        if df_lb is not None and not df_lb.empty:
            return df_lb
    except Exception as e:
        logger.warning("长桥 %s 历史数据失败: %s", code, e)
    yf = _get_yf()
    if yf is None:
        return None
    try:
        ticker = yf.Ticker(code)
        df = ticker.history(start=start, end=end, auto_adjust=True)
        if df is not None and not df.empty:
            df = df.reset_index()
            df.columns = [c.lower() for c in df.columns]
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            return df[["date","open","high","low","close","volume"]]
    except Exception as e:
        logger.warning("yfinance %s 历史数据失败: %s", code, e)
    return None

def fetch_us_spot(codes):
    # 腾讯实时接口优先：内网/国内网络最快最稳，且已验证可用
    df_tx = _fetch_us_spot_tx(codes)
    missing = []
    if df_tx is not None and not df_tx.empty:
        df_tx = df_tx.copy()
        if "pe" not in df_tx.columns:
            df_tx["pe"] = None
        if "pb" not in df_tx.columns:
            df_tx["pb"] = None
        # 腾讯只覆盖部分美股，港股(A股同理)常返回0 → 用长桥补齐
        miss_idx = df_tx.index[df_tx["price"].astype(float) <= 0].tolist()
        missing = [df_tx.loc[i, "c"] for i in miss_idx]
        ok_mask = df_tx["price"].astype(float) > 0
        if ok_mask.any() and not missing:
            return df_tx

    # 长桥 OpenAPI 补齐（港股/美股/A股统一，可配模拟盘）
    try:
        from .longbridge import fetch_spot as _lb_fetch
        lb = _lb_fetch(missing if missing else codes)
        if lb:
            df_lb = pd.DataFrame(lb)
            if not df_lb.empty:
                df_lb = df_lb.rename(columns={"c": "code"})
                lb_map = df_lb[df_lb["price"].astype(float) > 0].set_index("code").to_dict("index")
                if missing:
                    # 合并：腾讯已有行保留，缺失行用长桥填充
                    for i in miss_idx:
                        c = df_tx.loc[i, "c"]
                        if c in lb_map:
                            row = lb_map[c]
                            df_tx.loc[i, "name"] = row.get("name", c)
                            df_tx.loc[i, "price"] = row.get("price", 0.0)
                            df_tx.loc[i, "chg"] = row.get("chg", 0.0)
                            df_tx.loc[i, "pe"] = row.get("pe")
                            df_tx.loc[i, "pb"] = row.get("pb")
                    return df_tx
                return df_lb[["code", "name", "price", "chg", "pe", "pb"]]
    except Exception as e:
        logger.warning("长桥美股实时失败: %s", e)
    if df_tx is not None and not df_tx.empty and df_tx["price"].astype(float).sum() > 0:
        return df_tx

    try:
        import akshare as ak
        spot_df = ak.stock_us_spot_em()
        if spot_df is not None and not spot_df.empty:
            results = []
            for code in codes:
                cu = code.upper().strip()
                sym = _ak_symbol(code)
                match = spot_df[spot_df["代码"] == sym]
                if match.empty:
                    match = spot_df[spot_df["名称"].str.contains(cu, case=False, na=False)]
                if not match.empty:
                    row = match.iloc[0]
                    results.append({"c":code, "name":row.get("名称",code),
                                    "price":row.get("最新价",0.0), "chg":row.get("涨跌幅",0.0), "pe":row.get("市盈率-动态",None), "pb":row.get("市净率",None)})
                else:
                    results.append({"c":code, "name":code, "price":0.0, "chg":0.0, "pe":None, "pb":None})
            return pd.DataFrame(results) if results else pd.DataFrame()
    except Exception as e:
        logger.warning("akshare 美股实时失败: %s", e)

    yf = _get_yf()
    if yf is not None:
        results = []
        for code in codes:
            try:
                ticker = yf.Ticker(code)
                info = ticker.info
                price = info.get("currentPrice") or info.get("regularMarketPrice") or 0.0
                prev_close = info.get("previousClose") or 0.0
                chg = round((price-prev_close)/prev_close*100, 2) if prev_close > 0 else 0.0
                name = info.get("shortName") or info.get("longName") or code
                results.append({"c":code, "name":name, "price":price, "chg":chg})
            except Exception:
                results.append({"c":code, "name":code, "price":0.0, "chg":0.0, "pe":None, "pb":None})
        df_yf = pd.DataFrame(results) if results else pd.DataFrame()
        if not df_yf.empty and df_yf["price"].astype(float).sum() > 0:
            return df_yf

    return _empty_fallback(codes)


def _fetch_us_spot_tx(codes):
    """腾讯美股实时报价（快速，单只串行，超时短）"""
    results = []
    for code in codes:
        parts = _tx_us_quote(code)
        if parts:
            try:
                price = float(parts[3] or 0)
                prev = float(parts[4] or 0)
                chg = round((price - prev) / prev * 100, 2) if prev > 0 else 0.0
                name = parts[1] if len(parts) > 1 else code
                pe_val = None
                try:
                    pe_val = float(parts[39]) if parts[39] else None
                except (ValueError, IndexError):
                    pass
                pb_val = None
                try:
                    pb_val = float(parts[47]) if len(parts) > 47 and parts[47] else None
                except (ValueError, IndexError):
                    pass
                results.append({"c":code, "name":name, "price":price, "chg":chg,
                                "pe":pe_val, "pb":pb_val})
            except (ValueError, IndexError):
                results.append({"c":code, "name":code, "price":0.0, "chg":0.0, "pe":None, "pb":None})
        else:
            results.append({"c":code, "name":code, "price":0.0, "chg":0.0, "pe":None, "pb":None})
    return pd.DataFrame(results) if results else pd.DataFrame()

def _empty_fallback(codes):
    """最终兜底：返回0价格DataFrame，至少显示持股列表"""
    import pandas as pd
    return pd.DataFrame([{"c":c, "name":c, "price":0.0, "chg":0.0, "pe":None, "pb":None} for c in codes])


def fetch_us_index_history(symbol=".INX", days=1200):
    """美股指数收盘序列（标普500）。返回 Series[date->close] 或 None。

    - 主源: akshare index_us_stock_sina (.INX, 覆盖长期历史)
    - 兜底: 腾讯 K 线 usINX (仅最近几天，优先用于短期 horizon)
    """
    import json
    import urllib.request

    series = None
    try:
        import akshare as ak
        df = ak.index_us_stock_sina(symbol=symbol)
        if df is not None and not df.empty:
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])
            series = df.set_index("date")["close"].astype(float).sort_index()
    except Exception as e:
        logger.warning("akshare 美股指数历史失败: %s", e)
    if series is not None and len(series) > 0:
        return series

    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           "param=usINX,day,,,%s,qfq" % days)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0",
                          "Referer": "https://gu.qq.com/"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
        data = json.loads(raw)
        node = data.get("data", {}).get("usINX", {})
        items = node.get("day") or node.get("qfqday") or []
        if items:
            df = pd.DataFrame([
                {"date": pd.to_datetime(it[0]), "close": float(it[1])} for it in items
            ])
            series = df.set_index("date")["close"].astype(float).sort_index()
    except Exception as e:
        logger.warning("腾讯美股指数历史失败: %s", e)
    return series

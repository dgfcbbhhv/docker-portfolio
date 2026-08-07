"""基金/REIT/ETF 数据模块"""
import time
import pandas as pd
import akshare as ak
from .cache import retry, load, save, logger

REIT_TYPES = {
    "保障房": ["保障房", "保租房", "安居", "租赁住房", "有巢"],
    "产业园区": ["产业园", "产园", "张江", "蛇口", "联东", "临港", "软件园", "外高桥"],
    "仓储物流": ["仓储", "物流", "普洛斯", "京东", "宝湾", "顺丰", "中外运"],
    "高速公路": ["高速", "沪杭甬", "铁建", "交控", "山东高速", "隧道", "越秀", "招商公路"],
    "能源": ["能源", "电力", "光伏", "风电", "新能源", "清洁能源", "特变电工"],
    "收费公路": ["收费公路", "广河", "南京交通", "宁波交投", "深高速"],
    "消费": ["消费", "奥莱", "购物", "商业REIT", "百联", "物美", "唯品会"],
    "水务/环保": ["水务", "首创", "绿能", "首钢", "水利", "供热"],
    "数据中心": ["数据中心", "IDC"],
    "其他": []
}

def _classify_reit(name):
    for typ, keywords in REIT_TYPES.items():
        if any(k in name for k in keywords):
            return typ
    return "其他"


def fetch_reit_list():
    """获取全部公募REITs列表（含净值）"""
    cached = load("fund", "reit_list")
    if cached is not None:
        return cached
    try:
        df = retry(lambda: ak.fund_name_em(), max_n=3)
    except Exception as e:
        stale = load("fund", "reit_list", allow_expired=True)
        if stale is not None:
            logger.warning("fund_name_em 获取失败，使用过期缓存: %s", e)
            return stale
        raise
    df = df[df["基金类型"] == "Reits"].copy()
    df["类型"] = df["基金简称"].apply(_classify_reit)
    df = df.rename(columns={"基金代码": "code", "基金简称": "name"})

    try:
        spot = retry(lambda: ak.fund_etf_spot_ths(), max_n=3)
        spot = spot.rename(columns={"基金代码": "code", "当前-单位净值": "nav"})
        spot["nav"] = pd.to_numeric(spot["nav"], errors="coerce")
        df = df.merge(spot[["code", "nav"]], on="code", how="left")
    except Exception:
        df["nav"] = None

    result = df[["code", "name", "类型", "nav"]].reset_index(drop=True)
    save("fund", "reit_list", result)
    return result


def fetch_etf_list():
    """获取ETF实时行情（含净值）"""
    df = retry(lambda: ak.fund_etf_spot_ths(), max_n=3)
    df = df.rename(columns={
        "基金代码": "code", "基金名称": "name",
        "当前-单位净值": "nav", "当前-累计净值": "acc_nav",
        "前一日-单位净值": "pre_nav", "增长率": "nav_chg",
        "基金类型": "fund_type"
    })
    df["nav"] = pd.to_numeric(df["nav"], errors="coerce")
    df["pre_nav"] = pd.to_numeric(df["pre_nav"], errors="coerce")
    return df[["code", "name", "nav", "pre_nav", "acc_nav", "nav_chg", "fund_type"]]


def fetch_market_prices(codes):
    """通过腾讯接口获取基金/REIT市场价格"""
    from .market import _fetch_tx_quotes
    return _fetch_tx_quotes(codes)


def fetch_fund_navs(codes):
    """获取基金净值（用于补充REIT净值）"""
    try:
        spot = retry(lambda: ak.fund_etf_spot_ths(), max_n=3)
        spot = spot.rename(columns={"基金代码": "code", "当前-单位净值": "nav"})
        spot["nav"] = pd.to_numeric(spot["nav"], errors="coerce")
        nav_map = {}
        for _, r in spot.iterrows():
            if r["code"] in codes and pd.notna(r["nav"]):
                nav_map[r["code"]] = r["nav"]
        return nav_map
    except Exception:
        return {}


def _parse_dividend_summary(div_df, codes):
    """解析分红数据，计算近一年分红总额"""
    if div_df.empty:
        return {}
    div_df = div_df[div_df["基金代码"].isin(codes)].copy()
    if div_df.empty:
        return {}
    div_df["分红"] = pd.to_numeric(div_df["分红"], errors="coerce")
    div_df["权益登记日"] = pd.to_datetime(div_df["权益登记日"], errors="coerce")
    one_year_ago = pd.Timestamp.now() - pd.DateOffset(years=1)
    result = {}
    for code, grp in div_df.groupby("基金代码"):
        recent = grp[grp["权益登记日"] >= one_year_ago]
        total_div = recent["分红"].sum() if not recent.empty else grp["分红"].sum()
        div_count = len(recent) if not recent.empty else len(grp)
        result[code] = {"total_div": total_div, "div_count": div_count}
    return result


def fetch_fund_dividends(year=None):
    """获取基金分红数据（默认近两年，提高覆盖率避免当年无分红）"""
    if year is not None:
        return retry(lambda: ak.fund_fh_em(year=str(year)), max_n=3)
    this_year = pd.Timestamp.now().year
    frames = []
    for y in [this_year, this_year - 1]:
        try:
            frames.append(retry(lambda: ak.fund_fh_em(year=str(y)), max_n=3))
        except Exception:
            continue
    if not frames:
        raise RuntimeError("fund dividends unavailable")
    return pd.concat(frames, ignore_index=True)


def enrich_with_market(df, mkt):
    """将市场行情合并到数据框"""
    if not mkt:
        return df
    rows = []
    for _, r in df.iterrows():
        code = r["code"]
        q = mkt.get(code, {})
        row = r.to_dict()
        row.update({
            "price": q.get("price"),
            "chg": q.get("chg"),
            "mcap": q.get("mcap", 0),
        })
        rows.append(row)
    return pd.DataFrame(rows)


def enrich_with_premium(df):
    """计算折溢价率"""
    if "price" not in df.columns or "nav" not in df.columns:
        return df
    df["premium"] = df.apply(
        lambda r: round((r["price"] - r["nav"]) / r["nav"] * 100, 2)
        if pd.notna(r.get("price")) and pd.notna(r.get("nav")) and r["nav"] > 0
        else None, axis=1
    )
    return df


def enrich_with_dividend(df, div_summary):
    """合并分红数据"""
    df["total_div"] = df["code"].map(lambda c: div_summary.get(c, {}).get("total_div", 0))
    df["div_count"] = df["code"].map(lambda c: div_summary.get(c, {}).get("div_count", 0))
    df["div_yield"] = df.apply(
        lambda r: round(r["total_div"] / r["price"] * 100, 2)
        if r.get("price") and r["price"] > 0 and r["total_div"] > 0
        else None, axis=1
    )
    return df

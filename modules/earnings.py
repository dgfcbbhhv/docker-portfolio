"""盈利趋势分析 — 营收/净利同比、利润率、盈利评级

数据源: 同花顺 THS 财务摘要(多期, 含同比增长率), 与 fundamental 共用。
口径:
- np_yoy/rev_yoy:  最新一期(季报/年报) 净利润/营业总收入 同比
- annual_*:        最近一个完整财年(12-31) 的同比
- gross/net margin:最近财年毛利率/净利率
- trend:           最近 5 个财年的同比序列(时间升序)
- np_positive_years:最近连续净利正增长的财年数
- grade/flags:     由 analyze_earnings 从上述字段推导
"""
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from .data.cache import load, save

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
        f = float(v)
        return f if f == f else None
    except (ValueError, TypeError):
        return None


def _safe_ths(c):
    """同花顺财务摘要：基金/REIT 等无对应数据时兜底为空 DataFrame。"""
    try:
        import akshare as ak
        d = ak.stock_financial_abstract_ths(symbol=c)
    except Exception:
        return pd.DataFrame()
    if d is None or d.empty:
        return pd.DataFrame()
    return d


def _extract(d):
    """从 THS 多期摘要提取盈利指标序列。"""
    info = {
        "np_yoy": None, "rev_yoy": None,
        "annual_np_yoy": None, "annual_rev_yoy": None,
        "annual_np_ded_yoy": None,
        "gross_margin": None, "net_margin": None,
        "np_trend": [], "rev_trend": [],
        "np_positive_years": 0,
    }
    if "报告期" not in d.columns:
        return info
    d = d.copy()
    d["报告期"] = d["报告期"].astype(str)
    d = d.sort_values("报告期", ascending=True).reset_index(drop=True)
    if d.empty:
        return info

    latest = d.iloc[-1]
    info["np_yoy"] = _pct(latest.get("净利润同比增长率"))
    info["rev_yoy"] = _pct(latest.get("营业总收入同比增长率"))

    ann = d[d["报告期"].str.endswith("12-31")]
    if not ann.empty:
        last_ann = ann.iloc[-1]
        info["annual_np_yoy"] = _pct(last_ann.get("净利润同比增长率"))
        info["annual_rev_yoy"] = _pct(last_ann.get("营业总收入同比增长率"))
        info["annual_np_ded_yoy"] = _pct(last_ann.get("扣非净利润同比增长率"))
        info["gross_margin"] = _pct(last_ann.get("销售毛利率"))
        info["net_margin"] = _pct(last_ann.get("销售净利率"))
        info["np_trend"] = [_pct(r.get("净利润同比增长率")) for _, r in ann.tail(5).iterrows()]
        info["rev_trend"] = [_pct(r.get("营业总收入同比增长率")) for _, r in ann.tail(5).iterrows()]
        pos = 0
        for v in reversed(info["np_trend"]):
            if v is not None and v > 0:
                pos += 1
            else:
                break
        info["np_positive_years"] = pos
    return info


def analyze_earnings(info):
    """从提取的指标推导盈利评级 + 风险标记。纯逻辑，可单测。"""
    info = info or {}
    np_yoy = info.get("np_yoy")
    rev_yoy = info.get("rev_yoy")
    ann_np = info.get("annual_np_yoy")
    ann_ded = info.get("annual_np_ded_yoy")
    flags = []

    if np_yoy is not None:
        if np_yoy < 0 and rev_yoy is not None and rev_yoy < 0:
            flags.append("业绩变脸(营收净利双降)")
        elif np_yoy < 0:
            flags.append("净利同比转负")
        elif np_yoy < 10:
            flags.append("净利低增长")
        if np_yoy < -30:
            flags.append("净利大幅下滑")
    if rev_yoy is not None and rev_yoy < 0:
        flags.append("营收负增长")
    # 扣非依赖: 扣非净利同比明显弱于净利同比 → 利润靠非经常性损益
    if ann_np is not None and ann_ded is not None:
        if ann_ded < ann_np - 15:
            flags.append("扣非净利增速弱于净利")
    if info.get("np_positive_years") and info["np_positive_years"] <= 1 and np_yoy is not None and np_yoy < 0:
        flags.append("盈利连续走弱")

    if np_yoy is None:
        grade = "数据不足"
    elif np_yoy < -30:
        grade = "大幅恶化"
    elif np_yoy < 0:
        grade = "业绩承压"
    elif np_yoy < 15:
        grade = "增长放缓"
    elif np_yoy >= 30:
        grade = "强劲增长"
    else:
        grade = "稳定增长"
    return grade, flags


def _finalize(info):
    """补齐 grade/flags 字段（幂等，老缓存缺字段时也会补）。"""
    grade, flags = analyze_earnings(info)
    info["grade"] = grade
    info["flags"] = flags
    return info


def _fetch_one(c):
    k = str(c)
    df = load("earn", k, allow_expired=True)
    if df is not None and "np_yoy" in df.columns:
        info = df.iloc[0].to_dict()
        info.pop("_ts", None)
        info.pop("_expired", None)
        return c, _finalize(info)
    info = _extract(_safe_ths(c))
    info = _finalize(info)
    try:
        save("earn", k, pd.DataFrame([info]))
    except Exception:
        pass
    return c, info


def fetch_earnings(codes):
    """并行获取多只 A 股盈利分析数据。返回 {code: info}。"""
    res = {}
    codes = list(codes)
    if not codes:
        return res
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        fut = {pool.submit(_fetch_one, c): c for c in codes}
        for f in as_completed(fut):
            try:
                c, info = f.result(timeout=180)
                res[c] = info
            except Exception:
                pass
    return res

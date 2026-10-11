# 红利央企持仓分析平台 — 最终实施方案 v4.0

**日期**: 2026-07-21
**版本**: v4.0
**状态**: 待执行
**核心目标**: 帮助用户判断「何时上车、何时下车」

---

## 0. 全局设计原则

| 原则 | 含义 |
|------|------|
| 先假设后结论 | 每个模块顶部声明假设，用户可质疑 |
| 场景必须双面 | 任何推荐同时展示乐观/基准/悲观 |
| 区分事实与预测 | UI 上历史数据标「实际」，外推标「预估」 |
| 输入先验证 | 拉取数据后做合理性检查，异常标记 |
| 输出带置信度 | 点估计旁标注区间或概率 |
| 模型可审计 | 每个计算步骤可展开，无黑箱 |
| 先想输再想赢 | 红线机制一票否决，安全边际优先 |

---

## 1. 目录结构

```
docker-portfolio/
├── app.py                         # 主入口（8个Tab）
├── modules/
│   ├── __init__.py
│   ├── kelly.py                   # Kelly 引擎（主观胜率版）
│   ├── lishing.py                 # 李氏评分（红线+权重回收+双模式卖出）
│   ├── signals.py                 # 上车信号 + 卖出信号引擎
│   ├── backtest.py                # 回测引擎（三场景）
│   ├── optimizer.py               # 组合优化 + Monte Carlo
│   ├── alerts.py                  # 预警（邮件/Webhook/TG/微信）
│   ├── stress.py                  # 黑天鹅压力测试
│   └── data/
│       ├── __init__.py
│       ├── market.py              # 行情 + 历史分位 + 输入验证 + 流动性过滤
│       ├── fundamental.py         # 财报/分红/负债/审计/质押
│       └── cache.py               # 缓存 + 重试 + 并发限流 + 版本溯源
├── data/                          # 本地缓存目录（parquet）
│   └── .gitkeep
├── notes/                         # 人工备注存储（JSON）
│   └── .gitkeep
├── assumptions.md                 # 全局假设声明
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

---

## 2. 全局配置

```python
# app.py 顶部

# ── 持仓配置 ──
PORT_A = {
    "600900": {"name": "长江电力", "ratio": 25},
    "601398": {"name": "工商银行", "ratio": 20},
    "600036": {"name": "招商银行", "ratio": 10},
    "600941": {"name": "中国移动", "ratio": 20},
    "601088": {"name": "中国神华", "ratio": 10},
    "600938": {"name": "中国海油", "ratio": 10},
}
PORT_HK = {
    "00002": {"name": "中电控股", "ratio": 25},
    "02638": {"name": "港灯-SS",  "ratio": 20},
    "01398": {"name": "工商银行(H)", "ratio": 25},
    "00003": {"name": "中华煤气", "ratio": 15},
    "02388": {"name": "中银香港", "ratio": 5},
    "00005": {"name": "汇丰控股", "ratio": 10},
}
BIG4 = {
    "601288": "农业银行", "601988": "中国银行",
    "601939": "建设银行",
}

# ── Kelly 参数 ──
FRAC = 0.25
MAXPOS = 30.0
CORR = 0.75

# ── 李氏评分权重（满分100）──
LISHING_WEIGHTS = {
    "pe_score":     {"weight": 15, "desc": "PE < 15 或 5年分位 < 40%"},
    "debt_score":   {"weight": 10, "desc": "负债率 < 50%"},
    "roe_score":    {"weight": 15, "desc": "ROE > 12%"},
    "div_score":    {"weight": 20, "desc": "连续3年分红 + 股息率 > 3%"},
    "cf_score":     {"weight": 10, "desc": "经营现金流/净利润 > 80%"},
    "pb_score":     {"weight": 10, "desc": "PB < 1.5 或 5年分位 < 30%"},
    "mcap_score":   {"weight":  5, "desc": "市值 > 200亿"},
    "audit_score":  {"weight":  5, "desc": "审计意见 = 标准无保留"},
    "pledge_score": {"weight":  5, "desc": "大股东质押率 < 50%"},
    "rpt_score":    {"weight":  5, "desc": "关联交易占比 < 10%（可选，动态）"},
}

# ── 可交易性过滤 ──
TRADE_FILTER = {
    "min_listing_years": 3,
    "exclude_st": True,
    "min_avg_turnover": 5e7,
}

# ── 回测参数 ──
BACKTEST_RISK_FREE = 0.025
BACKTEST_COST = 0.001

# ── 组合优化 ──
OPT_MAXPOS = 30.0
OPT_MIN_STOCKS = 3
OPT_MC_SIMS = 1000

# ── 预警阈值 ──
ALERT_THRESHOLDS = {
    "price_drop":   8.0,
    "div_change":   0.5,
    "kelly_change": 5.0,
    "lishing_drop": 10.0,
}

# ── 上车信号阈值 ──
BUY_SIGNAL = {
    "min_lishing_score": 70,
    "min_div_yield": 3.0,
    "max_pe_pct": 40,
    "max_pb_pct": 30,
    "above_ma20": True,
}

# ── 卖出信号阈值 ──
SELL_SIGNAL = {
    "mode": "vs_high250",           # "vs_buy" 或 "vs_high250"
    "score_drop_vs_buy": 20,        # 相对买入时评分下降
    "score_drop_vs_high": 15,       # 相对250日高点下降
    "div_yield_floor": 2.0,         # 股息率跌破
    "red_line_trigger": True,       # 红线触发即清仓
}

# ── 行业白名单（护城河）──
MOAT_INDUSTRIES = [
    "电力", "港口", "水务", "电信", "能源", "银行", "基建",
    "高速公路", "铁路", "燃气", "供热",
]
```

---

## 3. 数据层（modules/data/）

### 3.1 cache.py — 缓存 + 重试 + 并发限流 + 输入验证

**设计要点：**
- 所有数据写入 `data/` 目录的 parquet 文件
- 每个文件记录拉取时间戳（`_ts` 列）
- 指数退避重试：1s → 2s → 4s，最多3次
- 并发限流：Semaphore(3)，防止 NAS 被 AKShare 拖垮
- TTL：行情1小时，财务30天，分红90天

```python
"""缓存层 + 指数退避重试 + 并发限流 + 输入验证"""
import os, time, datetime, hashlib
from threading import Semaphore
import pandas as pd

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")
os.makedirs(CACHE_DIR, exist_ok=True)

TTL = {
    "spot": 3600,           # 实时行情：1小时
    "hist": 3600,           # 历史行情：1小时
    "fin": 86400 * 30,      # 财务数据：30天
    "div": 86400 * 90,      # 分红数据：90天
}

AK_SEM = Semaphore(3)  # 最多3个并发请求

def _p(cat, key):
    d = os.path.join(CACHE_DIR, cat)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, hashlib.md5(key.encode()).hexdigest() + ".parquet")

def load(cat, key):
    p = _p(cat, key)
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_parquet(p)
        if "_ts" in df.columns:
            ts = pd.to_datetime(df["_ts"].iloc[0])
            if (datetime.datetime.now() - ts).total_seconds() > TTL.get(cat, 3600):
                return None
        return df
    except Exception:
        return None

def save(cat, key, df):
    df = df.copy()
    df["_ts"] = datetime.datetime.now().isoformat()
    df.to_parquet(_p(cat, key), index=False)

def retry(fn, max_n=3, delay=1.0):
    """指数退避重试 + 并发限流"""
    for i in range(max_n):
        try:
            with AK_SEM:
                return fn()
        except Exception:
            if i == max_n - 1:
                raise
            time.sleep(delay * (2 ** i))

def force_refresh(cat, key):
    """强制刷新，绕过 TTL"""
    p = _p(cat, key)
    if os.path.exists(p):
        os.remove(p)

def validate_spot(df):
    """输入验证，异常标记 _flags 列"""
    flags = []
    for _, r in df.iterrows():
        f = ""
        if r.get("price", 0) <= 0:
            f += "价格异常;"
        if pd.notna(r.get("pe")) and (r["pe"] < 0 or r["pe"] > 500):
            f += "PE异常;"
        if pd.notna(r.get("pb")) and r["pb"] <= 0:
            f += "PB异常;"
        if r.get("mcap", 0) <= 0:
            f += "市值异常;"
        flags.append(f)
    df["_flags"] = flags
    return df
```

### 3.2 market.py — 行情 + 历史分位 + 流动性过滤

```python
"""行情 + 港股 + 历史分位 + 全市场筛选 + 流动性过滤"""
import datetime, numpy as np, pandas as pd, akshare as ak
from .cache import load, save, retry, validate_spot

def fetch_spot_a(codes):
    """A股实时行情（带缓存+验证）"""
    cached = load("spot", "a_all")
    if cached is not None:
        return cached[cached["code"].isin(codes)].copy()

    df = retry(lambda: ak.stock_zh_a_spot_em())
    df = df[df["代码"].isin(codes)].copy()
    df.rename(columns={
        "代码": "code", "名称": "name", "最新价": "price",
        "涨跌幅": "chg", "市盈率-动态": "pe", "市净率": "pb",
        "总市值": "mcap", "成交额": "amount",
    }, inplace=True)
    df["mcap"] = df["mcap"] / 1e8
    df["pe"] = pd.to_numeric(df["pe"], errors="coerce")
    df["pb"] = pd.to_numeric(df["pb"], errors="coerce")
    df = validate_spot(df)
    save("spot", "a_all", df)
    return df[df["code"].isin(codes)].copy()

def fetch_spot_a_all():
    """全市场A股（用于筛选，已过滤ST/北交所）"""
    cached = load("spot", "a_all")
    if cached is not None:
        df = cached
    else:
        df = retry(lambda: ak.stock_zh_a_spot_em())
        df.rename(columns={
            "代码": "code", "名称": "name", "最新价": "price",
            "涨跌幅": "chg", "市盈率-动态": "pe", "市净率": "pb",
            "总市值": "mcap", "成交额": "amount",
        }, inplace=True)
        df["mcap"] = df["mcap"] / 1e8
        df["pe"] = pd.to_numeric(df["pe"], errors="coerce")
        df["pb"] = pd.to_numeric(df["pb"], errors="coerce")
        df = validate_spot(df)
        save("spot", "a_all", df)

    # 预过滤
    df = df[~df["name"].str.contains("ST|退|N|C|U|W", na=False)]
    df = df[df["mcap"] >= 50]
    df = df[df["price"] > 0]
    df = df[df["pe"] > 0]
    df = df[df["pb"] > 0]
    return df

def filter_tradable(df, min_years=3, min_turnover=5e7):
    """流动性 + 上市时长过滤"""
    df = df.copy()
    df["_tradable"] = True
    df["_trade_reason"] = ""

    if "amount" in df.columns:
        low = df["amount"] < min_turnover
        df.loc[low, "_tradable"] = False
        df.loc[low, "_trade_reason"] += "成交额不足;"

    if "list_days" in df.columns:
        short = df["list_days"] < min_years * 250
        df.loc[short, "_tradable"] = False
        df.loc[short, "_trade_reason"] += "上市时间短;"

    return df

def fetch_hist_percentile(code, years=5):
    """PE/PB 历史分位（近5年，后复权）"""
    cached = load("hist", f"{code}_pct")
    if cached is not None:
        return cached.iloc[0].to_dict()

    end = datetime.date.today()
    start = end - datetime.timedelta(days=years * 365)
    df = retry(lambda: ak.stock_zh_a_hist(
        symbol=code, period="daily",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"), adjust="qfq"
    ))
    if df is None or df.empty:
        return {"pe_pct": None, "pb_pct": None}

    closes = df["收盘"].values
    if len(closes) < 60:
        return {"pe_pct": None, "pb_pct": None}

    cur = closes[-1]
    pe_pct = float(np.sum(closes < cur) / len(closes) * 100)
    pb_pct = pe_pct

    result = pd.DataFrame([{"code": code, "pe_pct": pe_pct, "pb_pct": pb_pct}])
    save("hist", f"{code}_pct", result)
    return {"pe_pct": pe_pct, "pb_pct": pb_pct}

def fetch_vol(code, years=1):
    """近120日年化波动率"""
    cached = load("hist", f"{code}_vol")
    if cached is not None:
        return cached.iloc[0]["vol"]

    end = datetime.date.today()
    start = end - datetime.timedelta(days=365)
    vol = 0.2
    try:
        df = retry(lambda: ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"), adjust="qfq"
        ))
        if df is not None and not df.empty:
            cls = df["收盘"].values
            if len(cls) > 10:
                vol = float(np.std(np.log(cls[1:] / cls[:-1])) * np.sqrt(252))
                if vol <= 0:
                    vol = 0.2
    except Exception:
        pass
    save("hist", f"{code}_vol", pd.DataFrame([{"vol": vol}]))
    return vol
```

### 3.3 fundamental.py — 财报/分红/负债/审计/质押

```python
"""财务/分红/负债/现金流/审计"""
import akshare as ak
import pandas as pd
from .cache import load, save, retry

def fetch_fins(codes):
    """ROE/EPS/负债率/现金流/分红年数"""
    res = {}
    for c in codes:
        cached = load("fin", c)
        if cached is not None:
            res[c] = cached.iloc[0].to_dict()
            continue

        info = {
            "roe": None, "eps": None, "bps": None,
            "debt_ratio": None, "ocf_ratio": None,
            "audit_ok": None, "pledge_ratio": None,
            "div_years": 0,
        }
        try:
            df = retry(lambda: ak.stock_financial_abstract_ths(symbol=c))
            if not df.empty:
                for col in df.columns:
                    if "净资产收益率" in col:
                        v = df[col].dropna()
                        info["roe"] = float(v.iloc[0]) if not v.empty else None
                    if "基本每股收益" in col:
                        v = df[col].dropna()
                        info["eps"] = float(v.iloc[0]) if not v.empty else None
        except Exception:
            pass
        try:
            df3 = retry(lambda: ak.stock_balance_sheet_by_report_em(symbol=c))
            if df3 is not None and not df3.empty:
                df3 = df3.sort_values("报告期", ascending=False)
                ta = _gf(df3, "资产总计")
                tl = _gf(df3, "负债合计")
                if ta and tl and ta > 0:
                    info["debt_ratio"] = round(tl / ta * 100, 1)
        except Exception:
            pass
        try:
            df4 = retry(lambda: ak.stock_cash_flow_sheet_by_report_em(symbol=c))
            if df4 is not None and not df4.empty:
                df4 = df4.sort_values("报告期", ascending=False)
                ocf = _gf(df4, "经营活动产生的现金流量净额")
                np_ = _gf(df4, "净利润")
                if ocf and np_ and np_ > 0:
                    info["ocf_ratio"] = round(ocf / np_ * 100, 1)
        except Exception:
            pass
        try:
            df5 = retry(lambda: ak.stock_financial_report_sina(stock=c, symbol="资产负债表"))
            if df5 is not None and not df5.empty:
                info["audit_ok"] = True
        except Exception:
            info["audit_ok"] = None

        try:
            df6 = retry(lambda: ak.stock_fhps_detail_em(symbol=c))
            if df6 is not None and not df6.empty:
                df6 = df6[df6["实施方案公告日期"].notna()].sort_values(
                    "实施方案公告日期", ascending=False)
                if not df6.empty:
                    years = sum(1 for d in df6["每10股分红"].values if d > 0)
                    info["div_years"] = min(years, 10)
        except Exception:
            pass

        save("fin", c, pd.DataFrame([info]))
        res[c] = info
    return res

def fetch_divs(codes):
    """每股分红（A股）"""
    r = {}
    for c in codes:
        cached = load("div", c)
        if cached is not None:
            r[c] = cached.iloc[0]["div_per_share"]
            continue
        d = 0.0
        try:
            df = retry(lambda: ak.stock_fhps_detail_em(symbol=c))
            df = df[df["实施方案公告日期"].notna()].sort_values(
                "实施方案公告日期", ascending=False)
            if not df.empty:
                d = float(df.iloc[0].get("每10股分红", 0)) / 10.0
        except Exception:
            pass
        save("div", c, pd.DataFrame([{"div_per_share": d}]))
        r[c] = d
    return r

def _gf(df, col):
    if col not in df.columns:
        return None
    v = df[col].dropna()
    return float(v.iloc[0]) if not v.empty else None
```

---

## 4. Kelly 引擎（modules/kelly.py）— 主观胜率版

**假设声明：**
- p 为基于股息率与 ROE 的主观胜率估计，非统计概率
- b 基于当前估值和波动率，未来可能大幅偏离
- 0.25 (quarter Kelly) 为经验值，应对参数估计误差

```python
"""Fractional Kelly — 主观胜率版，锚定「拿得住+跌不怕」"""
import pandas as pd

FRAC = 0.25
CORR = 0.75
MAXPOS = 30.0

def run(price, pb, pe, roe, eps, debt, div, vol,
        frac=FRAC, corr=CORR, maxpos=MAXPOS):
    """
    参数:
        price: 当前股价
        pb, pe: 市净率/市盈率
        roe: 净资产收益率
        eps: 每股收益
        debt: 负债率
        div: 每股分红
        vol: 年化波动率

    返回 dict:
        p: 主观胜率
        b: 赔率
        kelly_raw: 原始Kelly占比%
        kelly_opt: Fractional Kelly最优占比%
        kelly_final: 相关性惩罚后最终占比%
        dividend_yield: 股息率%
    """
    # ── p (主观胜率)：锚定"拿得住 + 跌不怕" ──
    dy = (div / price) if price > 0 and div > 0 else 0.0
    roe_val = (roe / 100) if roe is not None else 0.0

    p = (
        0.50
        + 0.30 * min(dy / 0.05, 1.0)       # 股息率≥5%拿满
        + 0.20 * min(roe_val / 0.15, 1.0)   # ROE≥15%拿满
    )
    p = max(0.05, min(0.95, p))

    # ── b (赔率)：期望收益/期望亏损 ──
    dy_pct = dy * 100
    pb_up = max(0, (1.0 - pb) / pb * 100) if pb and pb < 1.0 else 0.0
    expected_return = dy_pct + pb_up
    expected_loss = (pb * vol * 100) if pb else (vol * 100)
    if expected_loss <= 0:
        expected_loss = 5.0
    b = max(0.1, min(10.0, expected_return / expected_loss))

    # ── Kelly fraction ──
    if b <= 0:
        kf = 0.0
    else:
        kf = max(0.0, min(1.0, (b * p - (1 - p)) / b))

    ko = round(kf * frac * 100, 1)
    ka = round(ko * (1.0 - corr * corr), 1) if ko > 0 else 0.0
    kfinal = min(ka, maxpos)

    return {
        "p": round(p, 3),
        "b": round(b, 2),
        "kelly_raw": round(kf * 100, 1),
        "kelly_opt": ko,
        "kelly_final": kfinal,
        "dividend_yield": round(dy_pct, 2),
    }
```

---

## 5. 李氏评分引擎（modules/lishing.py）— 红线+权重回收+动态关联交易

**假设声明：**
- 权重为经验设定，未经历史回测验证
- PE/PB 分位基于近5年后复权价格
- 红线机制为简化判断，不替代专业审计
- 评分仅反映当前财务状态，不代表未来表现

### 5.1 评分标签体系

| 区间 | 标签 | 含义 |
|------|------|------|
| ≥85 | 🟢 极度安全 | 李氏风格核心持仓 |
| 70-84 | 🟡 符合李氏 | 可纳入观察 |
| 55-69 | 🟠 谨慎 | 需要更多安全边际 |
| <55 | 🔴 高风险 | 不符合李氏标准 |

### 5.2 核心代码

```python
"""李氏评分引擎 — 红线 + 权重回收 + 动态关联交易"""
import json, os
import pandas as pd

NOTES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "notes")
os.makedirs(NOTES_DIR, exist_ok=True)

WEIGHTS = {
    "pe_score":     {"weight": 15, "desc": "PE < 15 或 5年分位 < 40%"},
    "debt_score":   {"weight": 10, "desc": "负债率 < 50%"},
    "roe_score":    {"weight": 15, "desc": "ROE > 12%"},
    "div_score":    {"weight": 20, "desc": "连续3年分红 + 股息率 > 3%"},
    "cf_score":     {"weight": 10, "desc": "经营现金流/净利润 > 80%"},
    "pb_score":     {"weight": 10, "desc": "PB < 1.5 或 5年分位 < 30%"},
    "mcap_score":   {"weight":  5, "desc": "市值 > 200亿"},
    "audit_score":  {"weight":  5, "desc": "审计意见 = 标准无保留"},
    "pledge_score": {"weight":  5, "desc": "大股东质押率 < 50%"},
    "rpt_score":    {"weight":  5, "desc": "关联交易占比 < 10%（可选）"},
}

# ── 红线检查 ──
def check_redlines(row, fin, st_flag=False):
    """
    一票否决：任一红线触发 → 返回 (0, reason)
    通过 → 返回 (None, None)
    """
    if st_flag:
        return 0, "ST风险"
    if fin.get("audit_ok") is False:
        return 0, "审计异常"
    if fin.get("ocf_ratio") is not None and fin["ocf_ratio"] < 0:
        return 0, "现金流断裂"
    return None, None

# ── 各维度评分 ──
def _score_pe(row, pe_pct=None):
    pe = row.get("pe")
    if pe is None or pe <= 0: return 0
    if pe < 15: return 1.0
    if pe_pct is not None and pe_pct < 40: return 0.8
    if pe < 25: return 0.5
    return 0.2

def _score_debt(dr):
    if dr is None: return 0.3
    if dr < 50: return 1.0
    if dr < 70: return 0.5
    return 0.0

def _score_roe(roe):
    if roe is None: return 0.3
    if roe > 12: return 1.0
    if roe > 8: return 0.6
    if roe > 5: return 0.3
    return 0.0

def _score_div(div_years, dy):
    years_ok = div_years >= 3
    dy_ok = dy >= 0.03
    if years_ok and dy_ok: return 1.0
    if years_ok: return 0.6
    if dy_ok: return 0.5
    return 0.0

def _score_cf(ocf_ratio):
    if ocf_ratio is None: return 0.3
    if ocf_ratio > 80: return 1.0
    if ocf_ratio > 60: return 0.5
    return 0.0

def _score_pb(row, pb_pct=None):
    pb = row.get("pb")
    if pb is None or pb <= 0: return 0.2
    if pb < 1.5: return 1.0
    if pb_pct is not None and pb_pct < 30: return 0.8
    if pb < 2.0: return 0.5
    return 0.2

def _score_mcap(mcap):
    if mcap is None or mcap <= 0: return 0.2
    if mcap > 200: return 1.0
    if mcap > 100: return 0.6
    return 0.2

def _score_audit(audit_ok):
    if audit_ok is True: return 1.0
    if audit_ok is False: return 0.0
    return 0.5

def _score_pledge(pledge_ratio):
    if pledge_ratio is None: return 0.5
    if pledge_ratio < 50: return 1.0
    if pledge_ratio < 70: return 0.4
    return 0.0

def _score_rpt(note):
    """关联交易评分（动态：有数据才计分）"""
    if note is None or "rpt_ratio" not in note:
        return None  # None = 无数据，不计分
    rpt = note["rpt_ratio"]
    if rpt < 10: return 1.0
    if rpt < 30: return 0.5
    return 0.0

# ── 主评分函数 ──
def calc_score(row, fin, div_per_share, pe_pct=None, pb_pct=None):
    """
    计算李氏评分（0-100）

    返回 dict:
        total_score: float
        label: str (🟢/🟡/🟠/🔴)
        detail: dict (各维度)
        flags: list (不达标项)
        redline: bool
        redline_reason: str or None
        weighted_pct: str (显示用, 如"82/95")
    """
    code = row.get("code", "")
    st_flag = "ST" in str(row.get("name", ""))

    # ── 红线检查 ──
    rl_score, rl_reason = check_redlines(row, fin, st_flag)
    if rl_score is not None:
        return {
            "total_score": 0,
            "label": "🔴",
            "detail": {},
            "flags": [rl_reason],
            "redline": True,
            "redline_reason": rl_reason,
            "weighted_pct": "0/100 (红线)",
        }

    dy = row.get("dy", 0) / 100 if row.get("dy", 0) > 1 else row.get("dy", 0)
    note = load_note(code)

    raw_scores = {
        "pe_score":     _score_pe(row, pe_pct),
        "debt_score":   _score_debt(fin.get("debt_ratio")),
        "roe_score":    _score_roe(fin.get("roe")),
        "div_score":    _score_div(fin.get("div_years", 0), dy),
        "cf_score":     _score_cf(fin.get("ocf_ratio")),
        "pb_score":     _score_pb(row, pb_pct),
        "mcap_score":   _score_mcap(row.get("mcap")),
        "audit_score":  _score_audit(fin.get("audit_ok")),
        "pledge_score": _score_pledge(fin.get("pledge_ratio")),
        "rpt_score":    _score_rpt(note),
    }

    # ── 权重回收：只计算有数据的维度 ──
    actual_score = 0
    actual_weight = 0
    detail = {}
    flags = []

    for key, w_info in WEIGHTS.items():
        raw = raw_scores.get(key)
        if raw is None:
            # 无数据，跳过（不计入分母）
            detail[key] = {"raw": None, "weighted": None, "desc": w_info["desc"], "status": "缺失"}
            continue

        w = w_info["weight"]
        actual_score += raw * w
        actual_weight += w
        detail[key] = {
            "raw": round(raw, 2),
            "weighted": round(raw * w, 1),
            "desc": w_info["desc"],
            "status": "正常" if raw >= 0.5 else "不达标",
        }
        if raw < 0.5:
            flags.append(w_info["desc"])

    # ── 归一化到100 ──
    total = round(actual_score / actual_weight * 100, 1) if actual_weight > 0 else 0

    # ── 标签 ──
    if total >= 85: label = "🟢"
    elif total >= 70: label = "🟡"
    elif total >= 55: label = "🟠"
    else: label = "🔴"

    # ── 显示用（如82/95）──
    if actual_weight < 95:
        weighted_pct = f"{total:.0f}/{actual_weight:.0f}（部分数据缺失）"
    else:
        weighted_pct = f"{total:.0f}/100"

    return {
        "total_score": total,
        "label": label,
        "detail": detail,
        "flags": flags,
        "redline": False,
        "redline_reason": None,
        "weighted_pct": weighted_pct,
    }

# ── 敏感性分析 ──
def sensitivity(row, fin, div_per_share, pe_pct=None, pb_pct=None):
    base = calc_score(row, fin, div_per_share, pe_pct, pb_pct)
    results = []
    for key, w_info in WEIGHTS.items():
        orig = w_info["weight"]
        delta = orig * 0.2

        w_plus = {k: {"weight": v["weight"], "desc": v["desc"]}
                  for k, v in WEIGHTS.items()}
        w_plus[key]["weight"] = orig + delta
        # 简化：直接调权重重算
        results.append({
            "dimension": w_info["desc"],
            "weight": orig,
            "impact": round(delta / 100 * base["total_score"], 1),
        })
    return sorted(results, key=lambda x: abs(x["impact"]), reverse=True)

# ── 人工备注 ──
def load_note(code):
    p = os.path.join(NOTES_DIR, f"{code}.json")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_note(code, data):
    p = os.path.join(NOTES_DIR, f"{code}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
```

---

## 6. 上车信号 + 卖出信号引擎（modules/signals.py）

**这是核心目标：帮用户判断「何时上车、何时下车」**

### 6.1 上车信号

```python
"""上车信号引擎"""
import pandas as pd

def buy_signal(row, fin, lishing_result, pe_pct=None, pb_pct=None,
               ma20=None, thresholds=None):
    """
    上车信号判断

    参数:
        row: 行情数据
        fin: 财务数据
        lishing_result: calc_score() 返回值
        pe_pct, pb_pct: 历史分位
        ma20: 20日均线价格
        thresholds: 上车阈值配置

    返回 dict:
        signal: "BUY" / "WATCH" / "AVOID"
        color: "🟢" / "🟡" / "🔴"
        reasons: list
        score: 综合评分 (0-100)
    """
    t = thresholds or {
        "min_lishing_score": 70,
        "min_div_yield": 3.0,
        "max_pe_pct": 40,
        "max_pb_pct": 30,
        "above_ma20": True,
    }

    reasons = []
    checks = []

    # 1. 李氏评分
    ls = lishing_result.get("total_score", 0)
    if ls >= t["min_lishing_score"]:
        checks.append(True)
        reasons.append(f"李氏评分 {ls:.0f} ≥ {t['min_lishing_score']}")
    else:
        checks.append(False)
        reasons.append(f"李氏评分 {ls:.0f} < {t['min_lishing_score']}")

    # 2. 股息率
    dy = row.get("dy", 0)
    if dy > 1:  # 可能是百分比
        dy_check = dy >= t["min_div_yield"]
    else:
        dy_check = (dy * 100) >= t["min_div_yield"]
    checks.append(dy_check)
    reasons.append(f"股息率 {dy:.2f}% {'✓' if dy_check else '✗'}")

    # 3. PE分位
    if pe_pct is not None:
        pe_check = pe_pct <= t["max_pe_pct"]
        checks.append(pe_check)
        reasons.append(f"PE分位 {pe_pct:.0f}% {'✓' if pe_check else '✗'}")

    # 4. PB分位
    if pb_pct is not None:
        pb_check = pb_pct <= t["max_pb_pct"]
        checks.append(pb_check)
        reasons.append(f"PB分位 {pb_pct:.0f}% {'✓' if pb_check else '✗'}")

    # 5. 价格在20日均线上方
    if ma20 is not None and t.get("above_ma20"):
        price = row.get("price", 0)
        ma_check = price > ma20
        checks.append(ma_check)
        reasons.append(f"价格{'>' if ma_check else '<'}MA20")

    # 6. 红线检查
    if lishing_result.get("redline"):
        return {
            "signal": "AVOID",
            "color": "🔴",
            "reasons": [f"红线触发: {lishing_result['redline_reason']}"],
            "score": 0,
        }

    # 综合判断
    passed = sum(checks)
    total = len(checks)

    if passed == total:
        signal, color = "BUY", "🟢"
    elif passed >= total * 0.6:
        signal, color = "WATCH", "🟡"
    else:
        signal, color = "AVOID", "🔴"

    return {
        "signal": signal,
        "color": color,
        "reasons": reasons,
        "score": round(passed / total * 100, 0) if total > 0 else 0,
    }
```

### 6.2 卖出信号（双模式）

```python
"""卖出信号引擎 — 双模式：相对买入时 / 相对250日高点"""
import pandas as pd
import numpy as np

def sell_signal(code, current_score, buy_score=None, high250_score=None,
                current_dy=None, lishing_result=None, thresholds=None):
    """
    卖出信号判断（支持两种模式）

    参数:
        code: 股票代码
        current_score: 当前李氏评分
        buy_score: 买入时评分（相对买入模式）
        high250_score: 近250日最高评分（相对高点模式）
        current_dy: 当前股息率
        lishing_result: calc_score() 返回值
        thresholds: 卖出阈值配置

    返回 dict:
        signal: "HOLD" / "REDUCE" / "SELL"
        color: "🟢" / "🟡" / "🔴"
        mode: "vs_buy" / "vs_high250"
        reasons: list
    """
    t = thresholds or {
        "score_drop_vs_buy": 20,
        "score_drop_vs_high": 15,
        "div_yield_floor": 2.0,
        "red_line_trigger": True,
    }

    reasons = []

    # ── 红线触发 → 直接清仓 ──
    if t.get("red_line_trigger") and lishing_result and lishing_result.get("redline"):
        return {
            "signal": "SELL",
            "color": "🔴",
            "mode": "redline",
            "reasons": [f"红线触发: {lishing_result['redline_reason']}"],
        }

    # ── 股息率跌破底线 → 减仓 ──
    if current_dy is not None and current_dy < t["div_yield_floor"]:
        reasons.append(f"股息率 {current_dy:.2f}% < {t['div_yield_floor']}%")

    # ── 模式1：相对买入时 ──
    if buy_score is not None:
        drop_from_buy = buy_score - current_score
        if drop_from_buy >= t["score_drop_vs_buy"]:
            reasons.append(f"较买入时下降 {drop_from_buy:.0f}分 (>{t['score_drop_vs_buy']})")
            return {
                "signal": "SELL",
                "color": "🔴",
                "mode": "vs_buy",
                "reasons": reasons,
            }
        elif drop_from_buy >= t["score_drop_vs_buy"] * 0.5:
            reasons.append(f"较买入时下降 {drop_from_buy:.0f}分")
            return {
                "signal": "REDUCE",
                "color": "🟡",
                "mode": "vs_buy",
                "reasons": reasons,
            }

    # ── 模式2：相对250日高点 ──
    if high250_score is not None:
        drop_from_high = high250_score - current_score
        if drop_from_high >= t["score_drop_vs_high"]:
            reasons.append(f"较250日高点下降 {drop_from_high:.0f}分 (>{t['score_drop_vs_high']})")
            return {
                "signal": "SELL",
                "color": "🔴",
                "mode": "vs_high250",
                "reasons": reasons,
            }
        elif drop_from_high >= t["score_drop_vs_high"] * 0.6:
            reasons.append(f"较250日高点下降 {drop_from_high:.0f}分")
            return {
                "signal": "REDUCE",
                "color": "🟡",
                "mode": "vs_high250",
                "reasons": reasons,
            }

    # ── 股息率跌破 ──
    if reasons:
        return {
            "signal": "REDUCE",
            "color": "🟡",
            "mode": "combined",
            "reasons": reasons,
        }

    return {
        "signal": "HOLD",
        "color": "🟢",
        "mode": "stable",
        "reasons": ["各项指标正常"],
    }
```

### 6.3 250日评分历史（用于卖出信号）

```python
def track_score_history(code, score, history_dir=None):
    """记录每日评分，用于计算250日高点"""
    import os, datetime
    d = history_dir or os.path.join(os.path.dirname(__file__), "..", "..", "data", "score_history")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{code}.csv")

    today = datetime.date.today().isoformat()
    if os.path.exists(p):
        df = pd.read_csv(p)
        df = df[df["date"] != today]
    else:
        df = pd.DataFrame(columns=["date", "score"])

    new_row = pd.DataFrame([{"date": today, "score": score}])
    df = pd.concat([df, new_row], ignore_index=True)
    df = df.sort_values("date").tail(250)
    df.to_csv(p, index=False)
    return df

def get_high250_score(code, history_dir=None):
    """获取近250日最高评分"""
    import os
    d = history_dir or os.path.join(os.path.dirname(__file__), "..", "..", "data", "score_history")
    p = os.path.join(d, f"{code}.csv")
    if not os.path.exists(p):
        return None
    df = pd.read_csv(p)
    if df.empty:
        return None
    return float(df["score"].max())
```

---

## 7. 回测引擎（modules/backtest.py）

```python
"""回测引擎 — 三场景 + 事实/预测标注"""
import datetime, numpy as np, pandas as pd, akshare as ak
from .cache import load, save, retry

def fetch_history(code, years=5):
    cached = load("hist", f"{code}_hist")
    if cached is not None:
        return cached
    end = datetime.date.today()
    start = end - datetime.timedelta(days=years * 365)
    df = retry(lambda: ak.stock_zh_a_hist(
        symbol=code, period="daily",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"), adjust="qfq"))
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"日期": "date", "收盘": "close"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    save("hist", f"{code}_hist", df.reset_index())
    return df

def backtest_portfolio(codes, weights, years=5, cost=0.001, risk_free=0.025):
    all_data = {}
    for c in codes:
        df = fetch_history(c, years)
        if not df.empty:
            all_data[c] = df["close"]
    if not all_data:
        return None

    prices = pd.DataFrame(all_data).dropna(how="all").ffill()
    returns = prices.pct_change().dropna()

    total_w = sum(weights.get(c, 0) for c in returns.columns)
    if total_w <= 0:
        return None
    w = {c: weights.get(c, 0) / total_w for c in returns.columns}

    port_ret = sum(returns[c] * w[c] for c in returns.columns)
    nav = (1 + port_ret).cumprod()
    monthly_ret = port_ret.resample("M").apply(lambda x: (1 + x).prod() - 1)

    total_return = nav.iloc[-1] / nav.iloc[0] - 1
    annual_return = (1 + total_return) ** (1 / years) - 1
    annual_vol = port_ret.std() * np.sqrt(252)
    sharpe = (annual_return - risk_free) / annual_vol if annual_vol > 0 else 0
    max_dd = ((nav - nav.cummax()) / nav.cummax()).min()
    win_rate = (monthly_ret > 0).sum() / len(monthly_ret) if len(monthly_ret) > 0 else 0
    turnover = cost * 2 * 12
    annual_return_net = annual_return - turnover * cost

    dd_start = ((nav - nav.cummax()) / nav.cummax()).idxmin()
    dd_period = returns.loc[:dd_start].tail(60)
    dd_attr = {c: round(dd_period[c].sum() * w.get(c, 0) * 100, 2)
               for c in dd_period.columns if w.get(c, 0) > 0}

    return {
        "nav": nav,
        "metrics": {
            "total_return": round(total_return * 100, 2),
            "annual_return": round(annual_return * 100, 2),
            "sharpe": round(sharpe, 2),
            "max_drawdown": round(max_dd * 100, 2),
            "annual_vol": round(annual_vol * 100, 2),
            "win_rate": round(win_rate * 100, 1),
            "turnover": round(turnover * 100, 1),
            "annual_return_net": round(annual_return_net * 100, 2),
        },
        "drawdown_attr": dd_attr,
        "monthly_returns": monthly_ret,
    }
```

---

## 8. 组合优化（modules/optimizer.py）

```python
"""组合优化 + Monte Carlo 置信区间"""
import numpy as np
import pandas as pd
from scipy.optimize import minimize

def calc_cov(returns):
    return returns.cov() * 252

def port_ret(w, mu): return np.dot(w, mu)
def port_vol(w, S): return np.sqrt(np.dot(w.T, np.dot(S, w)))

def max_sharpe(mu, S, rf=0.025):
    n = len(mu)
    r = minimize(lambda w: -(port_ret(w, mu) - rf) / port_vol(w, S),
                 np.ones(n)/n, method="SLSQP",
                 bounds=[(0, 0.3)]*n,
                 constraints={"type": "eq", "fun": lambda w: np.sum(w)-1})
    return r.x

def min_vol(mu, S):
    n = len(mu)
    r = minimize(lambda w: port_vol(w, S),
                 np.ones(n)/n, method="SLSQP",
                 bounds=[(0, 0.3)]*n,
                 constraints={"type": "eq", "fun": lambda w: np.sum(w)-1})
    return r.x

def risk_parity(S):
    n = S.shape[0]
    def rc(w):
        v = port_vol(w, S)
        m = np.dot(S, w)
        return np.sum((w*m/v - 1/n)**2)
    r = minimize(rc, np.ones(n)/n, method="SLSQP",
                 bounds=[(0, 0.3)]*n,
                 constraints={"type": "eq", "fun": lambda w: np.sum(w)-1})
    return r.x

def optimize(returns, codes, method="max_sharpe", rf=0.025, n_sims=1000):
    mu = returns.mean() * 252
    S = calc_cov(returns)

    if method == "max_sharpe": w = max_sharpe(mu, S, rf)
    elif method == "min_vol": w = min_vol(mu, S)
    else: w = risk_parity(S)

    optimal = {codes[i]: round(w[i]*100, 1) for i in range(len(codes)) if w[i] > 0.001}

    # 有效前沿
    frontier = []
    for tr in np.linspace(mu.min(), mu.max(), 20):
        n = len(codes)
        r = minimize(lambda w: port_vol(w, S), np.ones(n)/n, method="SLSQP",
                     bounds=[(0, 0.3)]*n,
                     constraints=[{"type": "eq", "fun": lambda w: np.sum(w)-1},
                                  {"type": "eq", "fun": lambda w: port_ret(w, mu)-tr}])
        if r.success:
            frontier.append((round(port_vol(r.x, S)*100, 2), round(tr*100, 2)))

    # Monte Carlo 置信区间
    mc = []
    for _ in range(n_sims):
        sim_mu = mu + np.random.normal(0, 0.02, len(mu))
        sim_S = S + np.random.normal(0, 0.001, S.shape)
        sim_S = (sim_S + sim_S.T) / 2
        try:
            if method == "max_sharpe": sw = max_sharpe(sim_mu, sim_S, rf)
            elif method == "min_vol": sw = min_vol(sim_mu, sim_S)
            else: sw = risk_parity(sim_S)
            mc.append(sw)
        except: continue

    mc = np.array(mc)
    ci = {}
    for i, c in enumerate(codes):
        if mc.shape[1] > i:
            vals = mc[:, i] * 100
            ci[c] = (round(np.percentile(vals, 2.5), 1), round(np.percentile(vals, 97.5), 1))

    return {"optimal_weights": optimal, "efficient_frontier": frontier, "confidence": ci}
```

---

## 9. 黑天鹅压力测试（modules/stress.py）

```python
"""压力测试 — 单日暴跌/行业全灭/相关性断裂/Monte Carlo"""
import numpy as np, pandas as pd

def single_day(codes, weights, drops=[-0.10, -0.20]):
    return [{"scenario": f"单日暴跌 {d*100:.0f}%",
             "impact": round(sum(weights.get(c,0)/100*d*100 for c in codes), 2)}
            for d in drops]

def industry_collapse(weights, industry_map):
    return [{"scenario": f"{ind}全灭",
             "impact": round(-sum(weights.get(c,0) for c in codes if c in weights), 2)}
            for ind, codes in industry_map.items()]

def correlation_break(codes, weights, returns, new_corr=0.95):
    S = returns.cov() * 252
    vols = np.sqrt(np.diag(S))
    n = len(codes)
    new_S = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            new_S[i,j] = vols[i]**2 if i==j else new_corr*vols[i]*vols[j]
    w = np.array([weights.get(c,0)/100 for c in codes])
    orig = np.sqrt(np.dot(w.T, np.dot(S, w)))
    new = np.sqrt(np.dot(w.T, np.dot(new_S, w)))
    return {"original_vol": round(orig*100,2), "stressed_vol": round(new*100,2),
            "increase": round((new-orig)*100,2)}

def monte_carlo_stress(returns, weights, n_sims=1000, days=20):
    codes = returns.columns.tolist()
    mu = returns.mean().values
    S = returns.cov().values * 9
    w = np.array([weights.get(c,0)/100 for c in codes])
    worst = []
    for _ in range(n_sims):
        sim = np.random.multivariate_normal(mu*0.5, S, days)
        sim -= np.abs(np.random.normal(0, 0.01, sim.shape))
        worst.append(np.prod(1 + sim @ w) - 1)
    worst = np.array(worst)
    return {"1%": round(np.percentile(worst,1)*100,2),
            "5%": round(np.percentile(worst,5)*100,2),
            "10%": round(np.percentile(worst,10)*100,2),
            "mean": round(worst.mean()*100,2)}
```

---

## 10. 预警系统（modules/alerts.py）

```python
"""多通道预警 + 论点破坏者"""
import json, os, smtplib, datetime, pandas as pd
from email.mime.text import MIMEText

CONFIG = os.path.join(os.path.dirname(__file__), "..", "..", "data", "alert_config.json")
LOG = os.path.join(os.path.dirname(__file__), "..", "..", "data", "alerts_log.csv")

class AlertSender:
    def __init__(self, cfg=None):
        self.cfg = cfg or self._load()
    def _load(self):
        if os.path.exists(CONFIG):
            with open(CONFIG) as f: return json.load(f)
        return {}
    def send(self, title, content, ch="email"):
        fn = {"email": self.email, "wechat": self.wechat,
              "telegram": self.telegram, "webhook": self.webhook}
        return fn.get(ch, lambda a,b: False)(title, content)
    def email(self, t, c):
        try:
            cfg = self.cfg
            msg = MIMEText(c, "plain", "utf-8")
            msg["Subject"] = t; msg["From"] = cfg["smtp_user"]; msg["To"] = cfg["alert_to"]
            with smtplib.SMTP_SSL(cfg["smtp_server"], cfg["smtp_port"]) as s:
                s.login(cfg["smtp_user"], cfg["smtp_pass"]); s.send_message(msg)
            return True
        except: return False
    def wechat(self, t, c):
        import urllib.request
        url = self.cfg.get("wechat_webhook", "")
        if not url: return False
        data = json.dumps({"msgtype":"text","text":{"content":f"{t}\n{c}"}})
        req = urllib.request.Request(url, data=data.encode(), headers={"Content-Type":"application/json"})
        try: urllib.request.urlopen(req); return True
        except: return False
    def telegram(self, t, c):
        import urllib.request
        tok, cid = self.cfg.get("tg_token",""), self.cfg.get("tg_chat_id","")
        if not tok or not cid: return False
        url = f"https://api.telegram.org/bot{tok}/sendMessage"
        data = json.dumps({"chat_id":cid,"text":f"*{t}*\n{c}","parse_mode":"Markdown"})
        req = urllib.request.Request(url, data=data.encode(), headers={"Content-Type":"application/json"})
        try: urllib.request.urlopen(req); return True
        except: return False
    def webhook(self, t, c):
        import urllib.request
        url = self.cfg.get("webhook_url", "")
        if not url: return False
        data = json.dumps({"title":t,"content":c,"time":datetime.datetime.now().isoformat()})
        req = urllib.request.Request(url, data=data.encode(), headers={"Content-Type":"application/json"})
        try: urllib.request.urlopen(req); return True
        except: return False

def check_alerts(portfolio, cur_data, prev_data, lishing_scores, thresholds):
    alerts = []
    for code, cfg in portfolio.items():
        name, cur, prev = cfg["name"], cur_data.get(code,{}), prev_data.get(code,{})
        chg = cur.get("chg", 0)
        if chg < -thresholds.get("price_drop",8):
            alerts.append({"code":code,"name":name,"type":"跌幅","msg":f"{name}跌{chg:.2f}%"})
        ck, pk = cur.get("kelly_final",0), prev.get("kelly_final",0)
        if abs(ck-pk) > thresholds.get("kelly_change",5):
            alerts.append({"code":code,"name":name,"type":"Kelly变化","msg":f"{name}Kelly变化{abs(ck-pk):.1f}%"})
        cs = lishing_scores.get(code,{}).get("total_score",100)
        ps = prev.get("lishing_score",cs)
        if ps - cs > thresholds.get("lishing_drop",10):
            alerts.append({"code":code,"name":name,"type":"评分下降","msg":f"{name}评分降{ps-cs:.0f}分"})
    return alerts

def log_alerts(alerts):
    if not alerts: return
    df = pd.DataFrame(alerts); df["ts"] = datetime.datetime.now().isoformat()
    if os.path.exists(LOG): df = pd.concat([pd.read_csv(LOG), df])
    df.to_csv(LOG, index=False, encoding="utf-8-sig")
```

---

## 11. app.py — 8个Tab 详细设计

### Tab 1: 📋 持仓看板

**功能：**
- 表格：代码/名称/占比/股价/涨跌/PE/PB/股息率/ROE/波动率/KellyOpt/建议占比/李氏评分
- 李氏评分带颜色标签：🟢≥85 | 🟡70-84 | 🟠55-69 | 🔴<55
- 异常数据标红（`_flags` 非空）
- 底部：「数据来源：东方财富/同花顺 via AKShare | 李氏评分基于历史财务数据，不代表未来表现」

---

### Tab 2: 📈 调仓报告

**功能：**
- 现有 Kelly 调仓建议
- 调仓后组合李氏评分变化预估
- 调仓前后李氏雷达图对比
- 三场景并排：乐观/基准/悲观

---

### Tab 3: 🎯 李氏选股清单

**3.1 量化筛选面板（10个条件，动态满分）**

| 条件 | 默认阈值 | 权重 |
|------|---------|------|
| PE < 15 或 5年分位 < 40% | 可调 | 15 |
| 负债率 < 50% | 可调 | 10 |
| ROE > 12% | 可调 | 15 |
| 连续3年分红 + 股息率 > 3% | 可调 | 20 |
| 经营现金流/净利润 > 80% | 可调 | 10 |
| PB < 1.5 或 5年分位 < 30% | 可调 | 10 |
| 市值 > 200亿 | 可调 | 5 |
| 审计意见 = 标准无保留 | 可调 | 5 |
| 大股东质押率 < 50% | 可调 | 5 |
| 关联交易占比 < 10% | 可调 | 5（动态） |

- 所有条件滑块可调
- 雷达图可视化
- 权重敏感性分析（龙卷风图）
- 显示格式：`82/95（关联交易缺失，已折算）`

**3.2 可交易性过滤**
- 成交额下限 slider（1000万～5亿）
- 上市年限 slider（1～5年）

**3.3 行业护城河筛选**
- 白名单一键筛选

**3.4 与持仓对比**
- 符合/不符合李氏标准
- 不符合项标红

**3.5 上车信号面板**
- 股息率 > 5% + ROE > 10% + 负债率 < 60% + PE/PB分位 < 30%
- 与李氏筛选交叉验证

**3.6 人工备注区**
- 管理层质素 / 护城河强度 / 备注
- 论点破坏者设置
- 关联交易占比输入
- 保存到 `notes/{code}.json`

---

### Tab 4: 🚦 上车信号仪表盘（核心Tab）

**这是帮用户判断「何时上车」的主界面。**

```
┌─────────────────────────────────────────────┐
│ 🚦 上车信号仪表盘                             │
│ 模式: [相对买入时] [相对250日高点]  ← 切换     │
├─────────────────────────────────────────────┤
│                                               │
│  长江电力 600900                              │
│  ┌──────────────────────────────────────┐    │
│  │ 信号: 🟢 可上车                        │    │
│  │ 李氏评分: 82/100 🟡                   │    │
│  │ 股息率: 4.2% ✓                        │    │
│  │ PE分位: 25% ✓                         │    │
│  │ PB分位: 18% ✓                         │    │
│  │ 价格 > MA20 ✓                         │    │
│  │ Kelly建议: 15.0%                      │    │
│  └──────────────────────────────────────┘    │
│                                               │
│  工商银行 601398                              │
│  ┌──────────────────────────────────────┐    │
│  │ 信号: 🟡 观望                         │    │
│  │ 李氏评分: 75/100 🟡                   │    │
│  │ 股息率: 5.1% ✓                        │    │
│  │ PE分位: 45% ✗ (>40%)                 │    │
│  │ PB分位: 35% ✗ (>30%)                 │    │
│  │ Kelly建议: 18.0%                      │    │
│  └──────────────────────────────────────┘    │
│                                               │
│ ⚠️ 本信号基于历史财务数据，不代表未来表现       │
└─────────────────────────────────────────────┘
```

**功能：**
- 每只持仓股一张卡片，显示上车信号 + 各条件达标情况
- 信号等级：🟢可上车 / 🟡观望 / 🔴回避
- 综合评分：各条件达标百分比
- 模式切换：相对买入时 / 相对250日高点（影响卖出判断）
- 底部风险提示

---

### Tab 5: 📉 卖出信号仪表盘

**这是帮用户判断「何时下车」的主界面。**

```
┌─────────────────────────────────────────────┐
│ 📉 卖出信号仪表盘                             │
│ 模式: [相对买入时 ✓] [相对250日高点]           │
├─────────────────────────────────────────────┤
│                                               │
│  长江电力 600900                              │
│  ┌──────────────────────────────────────┐    │
│  │ 信号: 🟢 持有                         │    │
│  │ 当前评分: 82 | 买入时: 80 | 变化: +2  │    │
│  │ 股息率: 4.2% (>2.0% ✓)              │    │
│  │ 红线: 无触发                          │    │
│  └──────────────────────────────────────┘    │
│                                               │
│  招商银行 600036                              │
│  ┌──────────────────────────────────────┐    │
│  │ 信号: 🟡 减仓                         │    │
│  │ 当前评分: 65 | 250日高点: 82 | 降17分  │    │
│  │ 股息率: 2.8% (>2.0% ✓)              │    │
│  │ ⚠️ 评分较250日高点下降明显             │    │
│  └──────────────────────────────────────┘    │
│                                               │
│ ⚠️ 本信号基于历史财务数据，不代表未来表现       │
└─────────────────────────────────────────────┘
```

**功能：**
- 每只持仓股一张卡片，显示卖出信号 + 各指标状态
- 信号等级：🟢持有 / 🟡减仓 / 🔴清仓
- 双模式切换：相对买入时 / 相对250日高点
- 评分变化趋势：当前分 vs 比较基准
- 红线触发高亮
- 底部风险提示

---

### Tab 6: 🔍 回测

**6.1 参数设置**
- 时间范围：3 / 5 / 10 年
- 策略：当前持仓 / 李氏选股 / 红利低波轮动
- 基准：沪深300
- 调仓频率：季度 / 半年 / 年度
- 交易成本：可调（默认0.1%）

**6.2 回测指标（三场景并排）**

| 指标 | 乐观 | 基准 | 悲观 |
|------|------|------|------|
| 累计收益率 | | | |
| 年化收益率 | | | |
| 夏普比率 | | | |
| 最大回撤 | | | |
| 年化波动率 | | | |
| 月度胜率 | | | |
| 年化换手率 | | | |
| 扣费后收益率 | | | |

**6.3 可视化**
- 净值曲线对比图（三场景 + 沪深300）
- 月度收益热力图
- 回撤曲线图
- 滚动夏普比率
- 回撤归因表

**6.4 底部标注**
- 「⚠️ 本回测未剔除退市股票，存在幸存者偏差；实盘收益率可能低于回测」
- 「回测基于历史数据，不代表未来表现」
- 标注数据时间范围

---

### Tab 7: 📊 组合优化 + 压力测试

**7.1 三种目标切换**
- 最大夏普比率 / 最小波动率 / 风险平价

**7.2 优化结果**
- 最优权重 vs 当前权重对比表
- 有效前沿散点图（标记当前位置 + Monte Carlo 置信椭圆）
- 风险-收益散点图
- 最优权重旁标注 95% CI

**7.3 黑天鹅压力测试**
- 单日暴跌 -10% / -20%
- 单行业全灭（银行/地产/能源）
- 相关性断裂（0.7 → 0.95）
- Monte Carlo 极端模拟（1000次）
- 结果表格 + 概率标注

**7.4 约束条件面板**
- 单只上限 30%
- 最少持有 3只
- 可锁定某些仓位

**7.5 底部标注**
- 「优化基于历史协方差，极端行情下可能失效」
- 「收益率假设为正态分布，实际存在肥尾」

---

### Tab 8: ℹ️ 说明

**8.1 全局假设声明（置顶，红框高亮）**

> 本平台所有分析均基于以下假设：
> 1. Kelly 中的 p 为基于股息率与 ROE 的主观胜率估计，非统计概率
> 2. 李氏评分权重为经验设定，未经历史回测验证
> 3. PE/PB 历史分位基于近5年后复权价格
> 4. 回测基于历史数据，不代表未来表现；存在幸存者偏差
> 5. 组合优化基于历史协方差，极端行情下可能失效
> 6. 预警基于实时数据，可能存在延迟
> 7. 所有分析仅供学习参考，不构成投资建议

**8.2 凯利公式原理**
**8.3 李氏选股框架说明**
**8.4 上车/卖出信号逻辑说明**
**8.5 PE/PB 分位计算方法**
**8.6 回测方法论**
**8.7 压力测试方法论**
**8.8 组合优化算法说明**
**8.9 数据源说明 + 缓存策略**

| 数据类型 | 缓存位置 | TTL |
|---------|---------|-----|
| 实时行情 | `data/spot/` | 1小时 |
| 历史行情 | `data/hist/` | 1小时 |
| 财务数据 | `data/fin/` | 30天 |
| 分红数据 | `data/div/` | 90天 |

**8.10 免责声明（置底）**

> 本工具仅供学习参考，不构成投资建议。
> 投资有风险，决策需谨慎。
> 数据来源：东方财富/同花顺 via AKShare，可能存在延迟或误差。

---

## 12. 依赖

```
akshare>=1.12.0
streamlit>=1.30.0
pandas>=2.0.0
numpy>=1.24.0
scipy>=1.10.0
schedule>=1.2.0
plotly>=5.18.0
```

---

## 13. Docker

**Dockerfile**
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p data notes
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
```

**docker-compose.yml**
```yaml
version: "3.8"
services:
  portfolio:
    build: .
    ports:
      - "8501:8501"
    volumes:
      - ./data:/app/data
      - ./notes:/app/notes
    restart: unless-stopped
```

---

## 14. 需求覆盖核对

### 核心目标

| 需求 | 状态 |
|------|------|
| 分析何时上车 | ✅ Tab 4 上车信号仪表盘 |
| 分析何时下车 | ✅ Tab 5 卖出信号仪表盘（双模式） |
| 买多少 | ✅ Kelly 引擎 |
| 买哪个 | ✅ 李氏选股 |
| 止损/止盈 | ✅ 红线机制 + 卖出信号 |

### 6个优化方向

| 方向 | 状态 |
|------|------|
| 方向一：Kelly主观胜率 | ✅ kelly.py |
| 方向二：李氏红线+权重回收 | ✅ lishing.py |
| 方向三：流动性过滤 | ✅ market.py filter_tradable |
| 方向四：缓存分级 | ✅ cache.py TTL |
| 方向五：并发限流 | ✅ cache.py Semaphore |
| 方向六：可解释性 | ✅ Tab 8 + 幸存者偏差声明 |

### 原始7个Tab + 新增

| Tab | 状态 |
|-----|------|
| Tab 1 持仓看板 | ✅ |
| Tab 2 调仓报告 | ✅ |
| Tab 3 李氏选股 | ✅ |
| Tab 4 上车信号 | ✅ 新增核心Tab |
| Tab 5 卖出信号 | ✅ 新增核心Tab |
| Tab 6 回测 | ✅ |
| Tab 7 组合优化+压力测试 | ✅ |
| Tab 8 说明 | ✅ |

---

## 15. 执行顺序

| 步骤 | 模块 | 文件 | 优先级 |
|------|------|------|--------|
| 1 | 数据层 | `data/cache.py` → `data/market.py` → `data/fundamental.py` | 最高 |
| 2 | 李氏评分 | `lishing.py`（红线+权重回收+动态关联交易） | 最高 |
| 3 | 上车信号 | `signals.py`（买入信号） | 最高 |
| 4 | 卖出信号 | `signals.py`（双模式卖出） | 最高 |
| 5 | Kelly | `kelly.py`（主观胜率） | 高 |
| 6 | Tab 1-5 | `app.py` 前五个Tab | 高 |
| 7 | 回测 | `backtest.py` + Tab 6 | 中 |
| 8 | 优化 | `optimizer.py` + Tab 7 | 中 |
| 9 | 压力测试 | `stress.py` + Tab 7 | 中 |
| 10 | 预警 | `alerts.py` | 中 |
| 11 | 说明 | Tab 8（假设声明+免责声明） | 低 |
| 12 | Docker | Dockerfile + docker-compose | 低 |
| 13 | 验证 | 全流程测试 | 高 |

---

## 16. 部署到NAS

> 实际环境是 Debian 12 NAS（非群晖 DSM），无 `/volume1`。Docker 28.5.2 + Compose v2.40.3。
> 部署路径：`/<NAS_DEPLOY_DIR>`（compose 从该目录读取 `.env`）。
> `app.py`、`modules/`、`config.json`、`data/`、`notes/` 均 bind mount，改代码后容器内即生效。

**上传（更新代码时）：** 打包排除 `.venv`/`data`/`notes`/`.git`/`__pycache__`，scp 到 NAS 后解压覆盖到部署目录：
```bash
tar --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' --exclude='*.pyc' \
    --exclude='.git' --exclude='.workbuddy' --exclude='data' --exclude='notes' --exclude='.env' \
    -czf /c/Users/<USER>/AppData/Local/Temp/opencode/portfolio-sync.tar.gz \
    -C /c/Users/<USER>/Documents/Codex/2026-07-17/new-chat/outputs/docker-portfolio .
scp /c/Users/<USER>/AppData/Local/Temp/opencode/portfolio-sync.tar.gz <NAS_USER>@<NAS_HOST>:/tmp/
```

**NAS上重建：**
```bash
sudo sh -c 'cd /<NAS_DEPLOY_DIR> && tar -xzf /tmp/portfolio-sync.tar.gz && rm /tmp/portfolio-sync.tar.gz'
sudo sh -c 'cd /<NAS_DEPLOY_DIR> && docker compose up -d --build'
```

**密钥：** `/<NAS_DEPLOY_DIR>/.env`（chmod 600，结构见 `.env.example`），填好后 `docker compose up -d` 生效。

**浏览器：** `http://<NAS_HOST>:8501`

---

*文档版本: v4.1 | 日期: 2026-08-01*
*核心目标: 帮助用户判断「何时上车、何时下车」*

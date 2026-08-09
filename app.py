#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import logging
import datetime, json, os, threading, time
from modules.netfix import enable_ipv4_preference
logger = logging.getLogger(__name__)
enable_ipv4_preference()
from modules.emfix import enable_em_http
enable_em_http()
import streamlit as st
import pandas as pd
from modules.data.market import fetch_spot_a, fetch_spot_hk, fetch_vols
from modules.data.fundamental import fetch_fins, fetch_fins_hk, fetch_divs, fetch_divs_hk, fetch_divs_us
from modules.portfolio_engine import build_rows, build_reit_rows
from modules.portfolio_engine import apply_layer_caps
from modules.lishing import calc as lishing_calc, load_note, save_note, DIMENSIONS, grade_label
from modules.data.cache import clear_cache, gc_stale, gc_orphans, active_keys
from modules.signals import evaluate_buy, evaluate_sell, load_score_history, save_score_snapshot, get_high_250d, set_buy_score, get_buy_score, get_buy_date, format_fix_summary
from modules.regime import detect_regime, fetch_macro_signals, get_factor_regime
from modules.data.market import fetch_smas, fetch_price_percentiles
from modules.cost_tier import (load_all as ct_load, get_stock as ct_stock,
    calc_overall_tier, add_batch, update_dividend, delete_batch,
    get_dividend_source, set_dividend_source, batch_import,
    init_batch as ct_init_batch)
from modules.backtest import backtest_scenarios, fetch_histories
from modules.alpha_beta import decompose_portfolio
from modules.optimizer import optimize
from modules.stress import single_day, industry_collapse, correlation_break, monte_carlo_stress
from modules.alerts import AlertSender, check_alerts, log_alerts, load_alert_history
from modules.data.fund import (fetch_reit_list, fetch_etf_list, fetch_market_prices,
    fetch_fund_dividends, enrich_with_market, enrich_with_premium,
    enrich_with_dividend, _parse_dividend_summary, fetch_fund_navs)
from modules.data.fallback import fetch_spot_with_fallback, fetch_hist_with_fallback, fetch_full_market_with_fallback

from modules.factors import calculate_factors_for_stock, get_factor_descriptions, get_factor_types, filter_factors_by_type
from modules.factor_selector import recommend_factors, get_factor_dynamic_bonus, STOCK_CONFIDENCE, STOCK_TIER, get_industry
from modules.learner import get_learner, FactorLearner

# ── Streamlit cache wrappers（下沉至数据缝 modules/data/app_cache）──
from modules.data.app_cache import (
    fetch_divs_cached as _cached_fetch_divs,
    fetch_divs_hk_cached as _cached_fetch_divs_hk,
    fetch_hist_em_or_tx_cached as _cached_fetch_hist_em_or_tx,
    fetch_factor_history_cached as _cached_fetch_factor_history,
    fetch_spot_a_cached as _cached_fetch_spot_a,
    fetch_spot_hk_cached as _cached_fetch_spot_hk,
    fetch_fins_cached as _cached_fetch_fins,
    fetch_fins_hk_cached as _cached_fetch_fins_hk,
    fetch_fins_us_cached as _cached_fetch_fins_us,
    fetch_vols_cached as _cached_fetch_vols,
    fetch_divs_us_cached as _cached_fetch_divs_us,
    fetch_market_prices_cached as _cached_fetch_market_prices,
    fetch_divs_reit_cached as _cached_fetch_divs_reit,
    load_snapshots_cached as _cached_load_snapshots,
    get_cache_ttl as _get_cache_ttl,
)

st.set_page_config(page_title="红利央企持仓分析", layout="wide", page_icon="\U0001F4CA")
st.markdown("""<style>
.kelly-card{background:#0e1117;border:1px solid #333;border-radius:8px;padding:12px;margin:4px 0}
.metric-up{color:#00ff88;font-weight:bold}
.metric-down{color:#ff4444;font-weight:bold}
.title{font-size:1.5rem;font-weight:bold;margin-bottom:0}
.subtitle{color:#888;font-size:0.85rem}
</style>""", unsafe_allow_html=True)

from modules.config import (PORT_A, PORT_HK, PORT_REIT, PORT_US,
                            MANUAL_DIV_YIELDS, MANUAL_PRICES, MANUAL_US_GROWTH,
                            WATCH_LIST, FRAC, MAXPOS, ALERT, MAX_TOTAL)


_FACTOR_MULT_CACHE = {}
_FACTOR_MULT_CACHE_KEY = None
def _get_factor_multiplier(code):
    """获取因子 IR 对应的 Kelly 仓位乘数（缓存版，因子重训练后自动失效）"""
    global _FACTOR_MULT_CACHE_KEY
    # 用 Alpha batch 结果的时间戳作为版本号，训练后自动失效
    batch = st.session_state.get("factor_batch_results", {})
    cur_key = batch.get("_ts") if isinstance(batch, dict) else None
    if cur_key != _FACTOR_MULT_CACHE_KEY:
        _FACTOR_MULT_CACHE.clear()
        _FACTOR_MULT_CACHE_KEY = cur_key
    if code in _FACTOR_MULT_CACHE:
        return _FACTOR_MULT_CACHE[code]
    val = 1.0
    try:
        from modules.factor_selector import get_kelly_multiplier as _gkm
        val = _gkm(code)
    except Exception as _e:
            logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    _FACTOR_MULT_CACHE[code] = val
    return val

def build(portfolio, fins, divs, vols, spot, label, earnings=None):
    """App 包装：委托给纯计算引擎，保持同一套口径。"""
    return build_rows(portfolio, fins, divs, vols, spot, label,
                      MANUAL_DIV_YIELDS, MANUAL_PRICES, MANUAL_US_GROWTH,
                      FRAC, MAXPOS, factor_multiplier_fn=_get_factor_multiplier,
                      earnings=earnings)


def build_reit(portfolio, spot, label, divs=None, vols=None):
    """简化版 build，REIT 不走 Kelly/LiShing，只用行情"""
    return build_reit_rows(portfolio, spot, label, MANUAL_DIV_YIELDS, divs, vols)

def run_portfolio():
    """主入口：动态TTL缓存，force=True时强制刷新"""
    import time
    cache = st.session_state.get("_portfolio_cache")
    force = st.session_state.pop("_force_cache_key", None)
    now = time.time()
    if cache and not force:
        if now - cache["ts"] < _get_cache_ttl():
            st.session_state["layer_summaries"] = cache.get("summaries", [])
            return cache["df"]
    df, layer_summaries = _run_portfolio_impl()
    st.session_state["_portfolio_cache"] = {"df": df, "summaries": layer_summaries, "ts": now}
    st.session_state["layer_summaries"] = layer_summaries
    return df

def _run_portfolio_impl():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from modules.data.market_us import fetch_us_spot
    from modules.data.fundamental import fetch_divs_reit
    from modules.earnings import fetch_earnings
    ca=list(PORT_A.keys()); chk=list(PORT_HK.keys())
    rows=[]
    with ThreadPoolExecutor(max_workers=6) as pool:
        f_spot_a=pool.submit(_cached_fetch_spot_a, tuple(ca))
        f_fins=pool.submit(_cached_fetch_fins, tuple(ca))
        f_earn=pool.submit(fetch_earnings, list(ca))
        f_divs=pool.submit(_cached_fetch_divs, tuple(ca))
        f_vols=pool.submit(_cached_fetch_vols, tuple(ca), "A")
        f_spot_hk=pool.submit(_cached_fetch_spot_hk, tuple(chk))
        f_fins_hk=pool.submit(_cached_fetch_fins_hk, tuple(chk))
        f_divs_hk=pool.submit(_cached_fetch_divs_hk, tuple(chk))
        f_vols_hk=pool.submit(_cached_fetch_vols, tuple(chk), "H")
        f_spot_us=pool.submit(fetch_us_spot, list(PORT_US.keys()))
        f_fins_us=pool.submit(_cached_fetch_fins_us, tuple(list(PORT_US.keys())))
        f_divs_us=pool.submit(_cached_fetch_divs_us, tuple(list(PORT_US.keys())))
        f_vols_us=pool.submit(_cached_fetch_vols, tuple(list(PORT_US.keys())), "US")
        sa=f_spot_a.result(); fa=f_fins.result(); ea=f_earn.result(); da=f_divs.result(); va=f_vols.result()
        sh=f_spot_hk.result(); fh=f_fins_hk.result(); dh=f_divs_hk.result(); vh=f_vols_hk.result()
        su=f_spot_us.result(); fu=f_fins_us.result(); du=f_divs_us.result(); vu=f_vols_us.result()
    if not sa.empty:
        rows+=build(PORT_A,fa,da,va,sa,"A股持仓",earnings=ea)
    if not sh.empty:
        rows+=build(PORT_HK,fh,dh,vh,sh,"港股持仓")
    if not su.empty:
        rows+=build(PORT_US,fu,du,vu,su,"美股")
    # REIT
    reit_codes = list(PORT_REIT.keys())
    if reit_codes:
        try:
            reit_mkt = _cached_fetch_market_prices(tuple(reit_codes))
            reit_divs = _cached_fetch_divs_reit(tuple(reit_codes))
            reit_vols = _cached_fetch_vols(tuple(reit_codes), "A")
            reit_df = pd.DataFrame([
                {"c": c, "price": q.get("price", 0), "chg": q.get("chg", 0)}
                for c, q in reit_mkt.items() if q
            ])
            if not reit_df.empty:
                rows += build_reit(PORT_REIT, reit_df, "REIT持仓", reit_divs, reit_vols)
        except Exception as _e:
            logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    # 组合级总仓位约束（Kelly 输出仅作参考区间）
    rows, layer_summaries = apply_layer_caps(rows)
    df=pd.DataFrame(rows)
    if df.empty:
        return df, layer_summaries
    o={"A股持仓":0,"港股持仓":2,"美股":3,"REIT持仓":4}
    df["_s"]=df["group"].map(o)
    return df.sort_values("_s").drop(columns="_s"), layer_summaries
# ═══════════════════════════════════════ UI ═══════════════════════════════════
st.markdown("<p class=title>\U0001F4CA 红利央企持仓分析</p>", unsafe_allow_html=True)
st.markdown("<p class=subtitle>数据源: 东方财富/同花顺(akshare) | Kelly + 李氏评分 | "
            + datetime.datetime.now().strftime("%Y-%m-%d") + "</p>", unsafe_allow_html=True)

# 初始化session state
if "auto_refresh" not in st.session_state:
    from modules.scheduler import is_trading_hours
    st.session_state.auto_refresh = is_trading_hours()
if "last_refresh" not in st.session_state:
    st.session_state.last_refresh = None

# 启动时清理过期/孤儿缓存：数据互通，避免陈旧数据干扰门禁与回测
if "cache_gc_done" not in st.session_state:
    try:
        gc_stale(ttl_multiplier=2.0)
        gc_orphans(active_keys())
    except Exception as _e:
            logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    st.session_state.cache_gc_done = True

c1,c2,c3=st.columns([1,3,1])
with c1:
    refresh=st.button("\U0001F504 刷新数据", type="primary")
    force_fin=st.button("\U0001F4C5 强制刷新财务数据")
    do_validate=st.button("\U0001F50D 跨源数据校验")
    if do_validate:
        st.session_state["_run_validate"]=True
with c2:
    auto_refresh=st.checkbox("交易时段自动刷新", value=st.session_state.auto_refresh)
    st.session_state.auto_refresh = auto_refresh
    if auto_refresh:
        from modules.scheduler import is_trading_hours
        if is_trading_hours():
            st.info("交易时段内，每5分钟自动刷新")
        else:
            st.warning("当前非交易时段")
with c3:
    if st.session_state.last_refresh:
        st.caption(f"上次刷新: {st.session_state.last_refresh}")
with st.container():
    status_text=st.empty()
if force_fin:
    clear_cache("fin"); clear_cache("div")
    st.session_state.pop("_portfolio_cache", None)
    st.session_state["_force_cache_key"] = datetime.datetime.now().isoformat()
    with st.spinner("强制刷新财务数据并拉取持仓..."):
        df=run_portfolio()
        st.session_state.df=df
elif refresh or "df" not in st.session_state:
    with st.spinner("拉取持仓数据..."):
        df=run_portfolio()
        st.session_state.df=df

df=st.session_state.get("df")
layer_summaries = st.session_state.get("layer_summaries", [])
try:
    from modules.config_validation import validate_config, validate_env
    _cfg_errors, _cfg_warnings = validate_config()
    _cfg_warnings += validate_env()
    for _w in _cfg_warnings:
        st.warning("配置警告: " + _w)
    for _e in _cfg_errors:
        st.error("配置错误: " + _e)
except Exception as _e:
            logger.warning(f"[{__name__}] %s", _e, exc_info=True)
if df is not None and df.empty:
    st.warning("⚠️ 无法获取市场数据（API连接失败且无缓存），请检查网络连接后重试")

# 数据新鲜度门禁：缓存过期或使用手动数据时显式提示，不做静默降级
try:
    from modules.data.freshness import flag_rows, freshness_issues, save_status, summary_line
    _issues, _status = freshness_issues()
    save_status(_status)
    if df is not None and not df.empty:
        df = flag_rows(df)
        st.session_state.df = df
    if _issues:
        st.warning("⚠️ 数据新鲜度异常：" + "；".join(_issues))
    else:
        st.caption("✅ " + summary_line(_status))
except Exception as _e:
            logger.warning(f"[{__name__}] %s", _e, exc_info=True)
if df is not None and not df.empty:
    _manual = df[df["data_flags"].astype(str).str.contains("manual", na=False)]
    if not _manual.empty:
        st.warning("⚠️ 以下标的正在使用手动股息率/股价兜底，请确认数据未过期：" + "、".join(_manual["name"].tolist()))
if layer_summaries:
    _layer_kelly = " | ".join(
        f"{s['layer']}: 投入上限 {s['invested_cap']:.0f}%, Kelly建议 {s['kelly_before_cap']:.1f}%"
        for s in layer_summaries if s.get("enabled")
    )
    st.caption("分层 Kelly 参考（各层独立上限）: " + _layer_kelly)
if st.session_state.pop("_run_validate", False):
    with st.spinner("跨源数据校验中..."):
        from modules.data.validate import validate_spot_by_market
        try:
            _report = validate_spot_by_market({
                "A": list(PORT_A.keys()),
                "HK": list(PORT_HK.keys()),
                "US": list(PORT_US.keys()),
            })
            st.session_state["_validate_report"] = _report
        except Exception as _ve:
            st.error("跨源校验失败: " + str(_ve))
_report = st.session_state.get("_validate_report")
if _report:
    _s = _report["summary"]
    _ok_re = sum(1 for r in _report.get("rows", []) if r.get("status") == "ok(recheck)")
    _n_issue = _s.get("mismatch", 0) + _s.get("missing_s1", 0)
    _re_note = f" | 复检通过 {_ok_re}" if _ok_re else ""
    with st.expander(f"跨源数据校验（共 {_s.get('total',0)} 只 | OK {_s.get('ok',0)} | 偏差 {_s.get('mismatch',0)} | 覆盖率 {_s.get('coverage',0):.0%}{_re_note}）"):
        _vr = pd.DataFrame(_report["rows"])
        if not _vr.empty:
            _cand = [c for c in ["mkt", "code", "name", "src1", "src2", "p1", "p2", "diff_pct", "status", "authority", "ap", "suspect"] if c in _vr.columns]
            _vr = _vr[_cand].copy()
            _vr.rename(columns={"ap": "权威价", "authority": "权威源", "suspect": "嫌疑源"}, inplace=True)
            st.dataframe(_vr, use_container_width=True, height=min(40 * len(_vr) + 38, 420))
        if _n_issue:
            _sus = [f"{r['code']}({r.get('suspect') or '待判定'})" for r in _report["rows"] if r.get("status") == "mismatch"]
            st.error(f"发现 {_n_issue} 只标的跨源偏差：{'、'.join(_sus)}")
        else:
            st.success("本次校验全部标的跨源一致")
t1,t2,t3,t4,t5,t6,t7,t8,t9,t10,t11,t12,t13,t14,t15,t16,t17,t18=st.tabs([
    "\U0001F4CB 持仓看板",
    "\U0001F4C8 调仓报告",
    "\U0001F4CA 买卖信号",
    "\U0001F3AF 回测",
    "\u2699\uFE0F 组合优化",
    "\u2139\uFE0F 说明",
    "\U0001F4B9 REIT/ETF",
    "\U0001F4B0 成本分层",
    "\U0001F4B0 Alpha因子",
    "🧠 多Agent共识",
    "📈 历史对比",
    "\U0001F50D 影子账户",
    "\U0001F9EA 模拟盘验证",
    "🧾 真实成交对账",
    "🧭 仓位管理",
    "🧠 芒格清单",
    "🧭 主营业务估值",
    "🌏 亚洲多空监测",
])
# ═══════════════════ 渲染模块（拆分自原 render_t1..t18）═══════════════════
# 渲染边界防御补丁：在 import render 前应用，保证任一 DataFrame 渲染安全
from render import _state as _rst
_rst.apply_st_safe()

from render import t1 as _rt1, t2 as _rt2, t3 as _rt3, t4 as _rt4, t5 as _rt5
from render import t6 as _rt6, t7 as _rt7, t8 as _rt8, t9 as _rt9, t10 as _rt10
from render import t11 as _rt11, t12 as _rt12, t13 as _rt13, t14 as _rt14, t15 as _rt15
from render import t16 as _rt16, t17 as _rt17, t18 as _rt18

# dispatch
_rt1.render(t1, df, layer_summaries)
_rt2.render(t2, df, layer_summaries)
_rt3.render(t3, df, layer_summaries)
_rt4.render(t4, df, layer_summaries)
_rt5.render(t5, df, layer_summaries)
_rt6.render(t6, df, layer_summaries)
_rt7.render(t7, df, layer_summaries)
_rt8.render(t8, df, layer_summaries)
_rt9.render(t9, df, layer_summaries)
_rt10.render(t10, df, layer_summaries)
_rt11.render(t11, df, layer_summaries)
_rt12.render(t12, df, layer_summaries)
_rt13.render(t13, df, layer_summaries)
_rt14.render(t14, df, layer_summaries)
_rt15.render(t15, df, layer_summaries)
_rt16.render(t16, df, layer_summaries)
_rt17.render(t17, df, layer_summaries)
_rt18.render(t18, df, layer_summaries)

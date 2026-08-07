#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import datetime, json, os, threading, time
from modules.netfix import enable_ipv4_preference
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
from modules.data.cache import retry, clear_cache, gc_stale, gc_orphans, active_keys
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
    except Exception:
        pass
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
        f_divs_us=pool.submit(_cached_fetch_divs_us, tuple(list(PORT_US.keys())))
        f_vols_us=pool.submit(_cached_fetch_vols, tuple(list(PORT_US.keys())), "US")
        sa=f_spot_a.result(); fa=f_fins.result(); ea=f_earn.result(); da=f_divs.result(); va=f_vols.result()
        sh=f_spot_hk.result(); fh=f_fins_hk.result(); dh=f_divs_hk.result(); vh=f_vols_hk.result()
        su=f_spot_us.result(); du=f_divs_us.result(); vu=f_vols_us.result()
    if not sa.empty:
        rows+=build(PORT_A,fa,da,va,sa,"A股持仓",earnings=ea)
    if not sh.empty:
        rows+=build(PORT_HK,fh,dh,vh,sh,"港股持仓")
    if not su.empty:
        rows+=build(PORT_US,{},du,vu,su,"美股")
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
        except Exception:
            pass
    # 组合级总仓位约束（Kelly 输出仅作参考区间）
    rows, layer_summaries = apply_layer_caps(rows)
    df=pd.DataFrame(rows)
    if df.empty:
        return df, layer_summaries
    o={"A股持仓":0,"港股持仓":2,"美股":3,"REIT持仓":4}
    df["_s"]=df["group"].map(o)
    return df.sort_values("_s").drop(columns="_s"), layer_summaries

# ── Alpha 因子批量训练后台线程（避免阻塞 Streamlit 脚本线程）─────────────────
# 训练/进化计算放后台线程执行，脚本侧轮询进度；线程内不调用任何 st.* API。
_FACTOR_BATCH = {"running": False, "done": False, "total": 0, "i": 0,
                 "results": {}, "ts": None, "error": None}
_FACTOR_BATCH_LOCK = threading.Lock()

def _factor_batch_run(watch_codes, do_train):
    def _run():
        with _FACTOR_BATCH_LOCK:
            _FACTOR_BATCH.update(running=True, done=False, total=len(watch_codes),
                                 i=0, results={}, ts=None, error=None)
        try:
            learner = get_learner()
            for idx, code in enumerate(watch_codes):
                with _FACTOR_BATCH_LOCK:
                    _FACTOR_BATCH["i"] = idx + 1
                name = WATCH_LIST[code]["name"]
                market = WATCH_LIST[code]["market"]
                try:
                    hist_df = _cached_fetch_factor_history(code, market)
                    if hist_df is None or hist_df.empty:
                        with _FACTOR_BATCH_LOCK:
                            _FACTOR_BATCH["results"][code] = {"name": name, "error": "无历史数据", "hist_df": None}
                        continue
                    if code.startswith(("007", "AAPL", "NVDA", "MSFT")):
                        pass  # 港股/美股可能数据不足
                    if do_train or not learner.is_trained(code):
                        learner.train(code, hist_df)
                    else:
                        learner.update(code, hist_df)
                    result = learner.get_learned_recommendation(code, hist_df)
                    train_info = learner.get_training_info(code)
                    bonus, _ = get_factor_dynamic_bonus(code, hist_df)
                    evolution = learner.compute_evolution(code, hist_df)
                    evo_summary = learner.get_evolution_summary(evolution) if evolution else None
                    with _FACTOR_BATCH_LOCK:
                        _FACTOR_BATCH["results"][code] = {
                            "name": name, "market": market, "hist_df": hist_df,
                            "result": result, "train_info": train_info, "bonus": bonus,
                            "evolution": evolution, "evo_summary": evo_summary}
                except Exception as e:
                    with _FACTOR_BATCH_LOCK:
                        _FACTOR_BATCH["results"][code] = {"name": name, "error": str(e), "hist_df": None}
            with _FACTOR_BATCH_LOCK:
                _FACTOR_BATCH["ts"] = datetime.datetime.now().isoformat()
        except Exception as e:
            with _FACTOR_BATCH_LOCK:
                _FACTOR_BATCH["error"] = str(e)
        finally:
            with _FACTOR_BATCH_LOCK:
                _FACTOR_BATCH["done"] = True
                _FACTOR_BATCH["running"] = False
                _FACTOR_BATCH["i"] = _FACTOR_BATCH["total"]
    threading.Thread(target=_run, daemon=True, name="factor-batch").start()

def _start_factor_batch(watch_codes, do_train):
    if _FACTOR_BATCH["running"]:
        return
    _factor_batch_run(watch_codes, do_train)

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
    except Exception:
        pass
    st.session_state.cache_gc_done = True

c1,c2,c3=st.columns([1,3,1])
with c1:
    refresh=st.button("\U0001F504 刷新数据", type="primary")
    force_fin=st.button("\U0001F4C5 强制刷新财务数据")
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
except Exception:
    pass
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
except Exception:
    pass
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

def highlight_ls(v):
    if v is None: return ""
    if v>=80: return "lishing-green"
    if v>=60: return "lishing-yellow"
    return "lishing-red"

def render_t1(t1):
    with t1:
        cost_data = ct_load()
        try:
            from modules.layer_risk import build_layer_risk_report
            from modules.config import LAYERS
            risk_report = build_layer_risk_report(df, LAYERS) if df is not None and not df.empty else []
        except Exception:
            risk_report = []
        if risk_report:
            st.subheader("分层回撤验收")
            rdf = pd.DataFrame(risk_report)
            rdf["单日-10%"] = rdf["single_day_10"].apply(lambda v: "%.1f%%" % v if v is not None else "-")
            rdf["单日-20%"] = rdf["single_day_20"].apply(lambda v: "%.1f%%" % v if v is not None else "-")
            rdf["行业最坏%"] = rdf["worst_industry"].apply(lambda v: "%.1f%%" % v if v is not None else "-")
            rdf["P95回撤%"] = rdf["mc_p95_drawdown"].apply(lambda v: "%.1f%%" % v if v is not None else "-")
            rdf = rdf.rename(columns={
                "layer": "层级", "sequence": "建仓顺序", "invested_cap": "投入上限%", "max_drawdown": "回撤红线%", "verdict": "结论", "note": "说明"
            })
            st.dataframe(rdf[["层级", "建仓顺序", "投入上限%", "回撤红线%", "单日-10%", "单日-20%", "行业最坏%", "P95回撤%", "结论", "说明"]], width="stretch", hide_index=True)
        if layer_summaries:
            st.subheader("分层概览")
            ldf = pd.DataFrame(layer_summaries)
            ldf = ldf.rename(columns={
                "layer": "层级", "sequence": "建仓顺序", "invested_cap": "投入上限%", "max_drawdown": "回撤红线%", "kelly_before_cap": "Kelly建议合计%", "cash": "现金缓冲%", "enabled": "启用"
            })
            st.dataframe(ldf[["层级", "建仓顺序", "投入上限%", "Kelly建议合计%", "现金缓冲%", "回撤红线%", "启用"]], width="stretch", hide_index=True)
        for grp in ["A股持仓","港股持仓","美股","REIT持仓"]:

            sub=df[df["group"]==grp] if df is not None and not df.empty else pd.DataFrame()
            if sub.empty: continue
            ic={"A股持仓":"\U0001F1E6\U0001F1F0","港股持仓":"\U0001F1ED\U0001F1F0","美股":"\U0001F1FA\U0001F1F8"}.get(grp,"")
            st.subheader(ic+" "+grp)
            sub2=sub.copy()
            sub2["占比"]=sub2.apply(lambda r: "%.0f%%"%r["weight"] if pd.notna(r.get("weight")) else ("%.0f%%"%r["ratio"] if pd.notna(r.get("ratio")) else "-"),axis=1)
            sub2["涨跌"]=sub2["chg"].apply(lambda x: "%.2f%%"%x)
            sub2["PE"]=sub2["pe"].apply(lambda x: "%.1f"%x if pd.notna(x) else "-")
            sub2["PB"]=sub2["pb"].apply(lambda x: "%.2f"%x if pd.notna(x) else "-")
            sub2["ROE"]=sub2["roe"].apply(lambda x: "%.1f%%"%x if pd.notna(x) else "-")
            sub2["净利同比"]=sub2["earn_np_yoy"].apply(lambda x: "%.1f%%"%x if pd.notna(x) else "-")
            sub2["营收同比"]=sub2["earn_rev_yoy"].apply(lambda x: "%.1f%%"%x if pd.notna(x) else "-")
            sub2["盈利评级"]=sub2["earn_grade"].fillna("-")
            sub2["李氏"]=sub2.apply(lambda r: "%.0f %s"%(r["lishing"],grade_label(r["lishing"])) if pd.notna(r["lishing"]) else "-", axis=1)
            def _tier_label(code, price):
                c = cost_data.get(code)
                if not c or not c.get("batches") or not price:
                    return "-"
                ot = calc_overall_tier(code, float(price))
                return ot["tier"]
            tier_map = {"黄金": "🟡", "白银": "⚪", "警戒": "🔴"}
            sub2["档位"] = sub2.apply(lambda r: tier_map.get(_tier_label(r["code"], r["price"]), "-"), axis=1)
            def _action_label(r):
                ls = r.get("lishing", 0)
                kf = r.get("kelly_portfolio_adj", r.get("kelly_vol_adj", r.get("kelly_final", 0)))
                grp = r.get("group", "")
                if "美股" in grp:
                    if pd.notna(ls) and ls >= 70 and pd.notna(kf) and kf > 0:
                        return "🟢可加仓"
                    if pd.notna(ls) and ls >= 50:
                        return "🟡观望"
                    return "⚪不加"
                if pd.notna(ls) and ls >= 70 and pd.notna(kf) and kf > 5:
                    return "🟢可加仓"
                if pd.notna(ls) and ls >= 60:
                    return "🟡观望"
                return "⚪不加"
            sub2["\u64cd\u4f5c"] = sub2.apply(_action_label, axis=1)
            if "港股" in grp:
                cols=["code","name","占比","price","dy_ttm","dy_lfy","档位","李氏","操作","kelly_vol_adj","kelly_factor_adj","kelly_portfolio_adj","vol_adj_factor"]
                d2=sub2[cols]
                d2.columns=["代码","名称","占比","股价","股息率(TTM)","股息率(LFY)","档位","李氏评分","操作","波动校准","因子仓位","组合约束","校准因子"]
                st.dataframe(d2,width="stretch",hide_index=True,column_config={
                    "股价":st.column_config.NumberColumn(format="%.2f"),
                    "股息率(TTM)":st.column_config.NumberColumn(format="%.2f%%"),
                    "股息率(LFY)":st.column_config.NumberColumn(format="%.2f%%"),
                    "波动校准":st.column_config.NumberColumn(format="%.1f%%"),
                    "校准因子":st.column_config.NumberColumn(format="%.2f"),
                    "组合约束":st.column_config.NumberColumn(format="%.1f%%"),
                    "李氏评分":st.column_config.NumberColumn(format="%.0f")})
            elif "美股" in grp:
                cols=["code","name","占比","price","dy","档位","李氏","操作","kelly_vol_adj","kelly_factor_adj","kelly_portfolio_adj","vol_adj_factor"]
                d2=sub2[cols]
                d2.columns=["代码","名称","占比","股价","股息率","档位","成长评分","操作","波动校准","因子仓位","组合约束","校准因子"]
                st.dataframe(d2,width="stretch",hide_index=True,column_config={
                    "股价":st.column_config.NumberColumn(format="%.2f"),
                    "股息率":st.column_config.NumberColumn(format="%.2f%%"),
                    "波动校准":st.column_config.NumberColumn(format="%.1f%%"),
                    "校准因子":st.column_config.NumberColumn(format="%.2f"),
                    "组合约束":st.column_config.NumberColumn(format="%.1f%%"),
                    "成长评分":st.column_config.NumberColumn(format="%.0f")})
            else:
                cols=["code","name","占比","price","dy","档位","李氏","净利同比","营收同比","盈利评级","操作","kelly_vol_adj","kelly_factor_adj","kelly_portfolio_adj","vol_adj_factor"]
                d2=sub2[cols]
                d2.columns=["代码","名称","占比","股价","股息率","档位","李氏评分","净利同比","营收同比","盈利评级","操作","波动校准","因子仓位","组合约束","校准因子"]
                st.dataframe(d2,width="stretch",hide_index=True,column_config={
                    "股价":st.column_config.NumberColumn(format="%.2f"),
                    "股息率":st.column_config.NumberColumn(format="%.2f%%"),
                    "净利同比":st.column_config.NumberColumn(format="%.1f%%"),
                    "营收同比":st.column_config.NumberColumn(format="%.1f%%"),
                    "波动校准":st.column_config.NumberColumn(format="%.1f%%"),
                    "校准因子":st.column_config.NumberColumn(format="%.2f"),
                    "组合约束":st.column_config.NumberColumn(format="%.1f%%"),
                    "李氏评分":st.column_config.NumberColumn(format="%.0f")})
            for _,r in sub.iterrows():
                flags=r.get("lishing_flags","")
                if flags:
                    st.caption("\u26A0 "+r["name"]+": "+flags+" 点击[李氏选股清单]Tab查看详情")
                eflags=r.get("earn_flags","")
                if eflags is not None and not (isinstance(eflags,float) and eflags!=eflags):
                    eflags=str(eflags)
                    if eflags:
                        st.caption("\U0001F4C9 "+r["name"]+" 盈利预警: "+eflags)

        # 导出功能（在循环外部，确保只创建一次）
        st.markdown("---")
        exp_col1, exp_col2 = st.columns([1, 2])
        with exp_col1:
            st.markdown("### 📥 数据导出")
        with exp_col2:
            from modules.exporter import export_portfolio_excel, EXPORT_DIR
            batch_res = st.session_state.get("factor_batch_results", {})
            if st.button("📊 导出完整分析报告 (Excel)", key="btn_export_excel"):
                with st.spinner("生成报告..."):
                    try:
                        filepath = export_portfolio_excel(
                            df, 
                            factor_batch_results=batch_res if batch_res else None,
                            cost_data=ct_load()
                        )
                        st.success(f"✅ 报告已生成: {filepath}")
                    except Exception as e:
                        st.error(f"导出失败: {e}")
            st.caption(f"导出路径: {EXPORT_DIR}")

        # 自动保存快照
        from modules.exporter import save_snapshot
        try:
            save_snapshot(df)
        except Exception:
            pass


def render_t2(t2):
    with t2:
        ca,cb=st.columns([2,1])
        with ca:
            st.subheader("\U0001F4CC Kelly调仓路线")
            has=False
            if df is not None and not df.empty:
                for _,r in df[df["group"].isin(["A股持仓","港股持仓","美股"])].iterrows():
                    cur=r.get("weight") if pd.notna(r.get("weight")) else (r["ratio"] if pd.notna(r.get("ratio")) else 0)
                    fa=r.get("kelly_portfolio_adj", r.get("kelly_factor_adj", r.get("kelly_vol_adj", r.get("kelly_final", 0))))
                    diff=fa-cur
                    if diff>2:
                        st.markdown("<p class=metric-up>"+chr(9650)+" 加仓 <b>"+r["name"]+"</b>: %.0f%% -> %.0f%% (+%.0f%%)</p>"%(cur,fa,diff),unsafe_allow_html=True)
                        has=True
                    elif diff<-2:
                        st.markdown("<p class=metric-down>"+chr(9660)+" 减仓 <b>"+r["name"]+"</b>: %.0f%% -> %.0f%% (%.0f%%)</p>"%(cur,fa,diff),unsafe_allow_html=True)
                        has=True
                if not has: st.info("\u2705 组合与Kelly建议基本一致")
            st.subheader("\U0001F50D 持仓体检")
            if df is not None and not df.empty:
                for _,r in df[df["group"].isin(["A股持仓","港股持仓","美股"])].iterrows():
                    iss=[]
                    is_us = "美股" in r.get("group","")
                    if not is_us and r["dy"]>0 and r["dy"]<1.5: iss.append("股息率%.2f%%"%r["dy"])
                    if pd.notna(r["pe"]) and r["pe"]>30: iss.append("PE %s"%r["pe"])
                    if pd.notna(r["chg"]) and r["chg"]<-ALERT: iss.append("回撤%.1f%%"%r["chg"])
                    if is_us:
                        if r["lishing"]<40: iss.append("成长评分%.0f"%r["lishing"])
                    else:
                        if r["lishing"]<60: iss.append("李氏评分%.0f"%r["lishing"])
                    if iss: st.error("**"+r["name"]+"**: "+"; ".join(iss))
                    else: st.success("**"+r["name"]+"**: \u2705 正常")

def render_t3(t3):
    with t3:
        st.markdown("### \U0001F4CA 买卖信号仪表盘")
        st.caption("加权多因子评分(0-100) | 买入: 🟢强烈买入 ≥80 / 🟢可买入 ≥65 / 🟡观望 ≥45 / ⚪回避 | 卖出: 🟢持有 ≥75 / 🟡减仓 ≥55 / 🔴卖出")
        if df is not None and not df.empty:
            c1,c2=st.columns(2)
            with c1:
                ls_th=st.number_input("评分阈值",0,100,70,5,key="bt",
                    help="≥80强烈买入 | ≥65可买入 | ≥45观望 | <45回避。当前70是李氏评分\"符合李氏标准\"的及格线")
            with c2:
                cm=st.selectbox("卖出基准",["买入时","250日高点"],0,
                    help="买入时: 对比你的持仓成本, 适合长持 | 250日高点: 对比近期最高价, 适合短线止盈")
            ck={"买入时":"buy","250日高点":"high_250d"}[cm]
            refresh_sig=st.button("🔄 刷新信号数据",key="refresh_sig")
            st.divider()
            sub=df[df["group"].isin(["A股持仓","港股持仓","美股"])]
            st.caption("港股数据源：东方财富，PE/PB/ROE 可能有时延；美股数据源：akshare 东方财富")
            codes_t5=sub["code"].tolist()

            if refresh_sig or "sig_pctls" not in st.session_state:
                with st.spinner("获取行情数据..."):
                    _mkt_map = {c: WATCH_LIST.get(c, {}).get("market", "A") for c in codes_t5}
                    pctls = {}
                    for _m in ("A", "HK", "US"):
                        _cs = [c for c, m in _mkt_map.items() if m == _m]
                        if _cs:
                            pctls.update(fetch_price_percentiles(_cs, mkt=_m))
                    st.session_state.sig_pctls = pctls
                    smas = {}
                    for _m in ("A", "HK", "US"):
                        _cs = [c for c, m in _mkt_map.items() if m == _m]
                        if _cs:
                            smas.update(fetch_smas(_cs, mkt=_m))
                    st.session_state.sig_smas = smas
            pctls = st.session_state.sig_pctls
            smas = st.session_state.sig_smas

            pe_pctls_all={c:pctls.get(c,50) for c in codes_t5}
            pb_pctls_all={c:pctls.get(c,50) for c in codes_t5}
            macro_signals = fetch_macro_signals()
            regime=detect_regime(pe_pctls_all,pb_pctls_all,macro_signals=macro_signals)
            rcolor={"undervalued":"#00ff88","neutral":"#ffaa00","overvalued":"#ff4444"}.get(regime["regime"],"#888")
            st.markdown(f"<div style='border:1px solid {rcolor};border-radius:8px;padding:10px;margin:8px 0'>",unsafe_allow_html=True)
            st.markdown(f"<b>市场诊断:</b> {regime['label']} | PE中位 {regime['pe_median']} | PB中位 {regime['pb_median']} | 阈值调整 {regime['threshold_adjust']:+d}")
            if regime["risk_warning"]: st.warning(regime["risk_warning"])
            if regime.get("macro") and "error" not in regime["macro"]:
                m = regime["macro"]
                parts = []
                if m.get("china_us_spread") is not None:
                    parts.append(f"中美利差 {m['china_us_spread']:.1f}%")
                if m.get("vix") is not None:
                    parts.append(f"VIX {m['vix']}")
                if parts:
                    st.caption(" | ".join(parts) + f"  ({m.get('updated_at', '')})")
            st.markdown("</div>",unsafe_allow_html=True)

            # --- 因子有效性状态 ---
            codes_for_regime = [c for c in sub["code"].tolist()]
            fr_label, fr_details = get_factor_regime(codes_for_regime)
            fr_color = {"momentum_active": "#00ff88", "mixed": "#ffaa00", "factor_weak": "#ff4444", "neutral": "#888"}.get(fr_label, "#888")
            fr_emoji = {"momentum_active": "🟢", "mixed": "🟡", "factor_weak": "🔴", "neutral": "⚪"}.get(fr_label, "⚪")
            st.markdown(f"<span style='color:{fr_color}'>{fr_emoji} 因子市场: {fr_label}</span>", unsafe_allow_html=True)
            if fr_details.get("all_types"):
                parts_ft = []
                for ft, fv in fr_details["all_types"].items():
                    parts_ft.append(f"{ft} IR={fv:.3f}")
                st.caption(" | ".join(parts_ft))

            # --- 汇总操作建议 ---
            buy_names, watch_names, avoid_count = [], [], 0
            for _,rr in sub.iterrows():
                ls_=rr.get("lishing",0); kf_=rr.get("kelly_final",0)
                is_us = "美股" in rr.get("group","")
                if is_us:
                    # 美股: 使用成长评分判断
                    if pd.notna(ls_) and ls_ >= 60:
                        buy_names.append(rr["name"])
                    elif pd.notna(ls_) and ls_ >= 40:
                        watch_names.append(rr["name"])
                    else:
                        avoid_count += 1
                elif pd.notna(ls_) and ls_ >= 70 and pd.notna(kf_) and kf_ > 5:
                    buy_names.append(rr["name"])
                elif pd.notna(ls_) and ls_ >= 60:
                    watch_names.append(rr["name"])
                else:
                    avoid_count += 1
            parts=["" for _ in range(3)]
            if buy_names: parts[0]=f"\U0001f7e2 可加仓: {', '.join(buy_names)}"
            if watch_names: parts[1]=f"\U0001f7e1 观望: {', '.join(watch_names)}"
            if avoid_count: parts[2]=f"\u26aa 不加: {avoid_count}只"
            summary = " | ".join(p for p in parts if p)
            if summary: st.info(summary)
            st.divider()

            for _,rr in sub.iterrows():
                c=rr["code"]; nm=rr["name"]; ls=rr.get("lishing",0)
                pp=pctls.get(c); sm=smas.get(c)
                cp=float(rr.get("price",0)); am=cp>sm if sm and cp>0 else None
                dy=float(rr.get("dy",0)); rf=pd.notna(ls) and ls==0
                kf=rr.get("kelly_final"); psig=rr.get("p_signal"); vaf=rr.get("vol_adj_factor")
                ma20_val = sm if isinstance(sm, (int, float)) else None
                ma60_val = None
                ma20_gt_ma60 = None
                if isinstance(smas, dict):
                    s60 = smas.get(c + "_ma60")
                    if s60 is not None:
                        ma60_val = float(s60)
                        if ma20_val is not None:
                            ma20_gt_ma60 = ma20_val > ma60_val

                factor_bonus = st.session_state.get("fbonus_" + c, 0)
                bs=evaluate_buy(ls,pp,pp,dy,am,ma20_gt_ma60,kf,psig,vaf,rf,ls_th,factor_dynamic_bonus=factor_bonus,regime=regime)
                bsc=get_buy_score(c); h250=get_high_250d(c)
                ss=evaluate_sell(ls,bsc,h250,dy,kf,psig,am,ma20_gt_ma60,rf,factor_dynamic_bonus=factor_bonus)
                if pd.notna(ls): save_score_snapshot(c,ls)

                buy_color = "#00ff88" if bs["score"] >= 80 else "#aaff00" if bs["score"] >= 65 else "#ffaa00" if bs["score"] >= 45 else "#ff4444"
                sell_color = "#00ff88" if ss["score"] >= 75 else "#ffaa00" if ss["score"] >= 55 else "#ff4444"
                expander_label = f"{nm} | 🟢买{bs['score']} {bs['label']} | 🔴卖{ss['score']} {ss['label']}"
                with st.expander(expander_label, False):
                    cols = st.columns([1, 1])
                    with cols[0]:
                        st.markdown(f"<div style='border:1px solid {buy_color};border-radius:6px;padding:8px'>", unsafe_allow_html=True)
                        st.markdown(f"**买入评分: {bs['score']}/{bs['max_score']}**")
                        for f in bs["factors"]:
                            pct = f["score"] / f["max"] * 100 if f["max"] > 0 else 0
                            bar_w = max(pct, 5)
                            ic = "🟢" if pct >= 70 else "🟡" if pct >= 40 else "🔴"
                            st.markdown(f"{ic} **{f['name']}**: {f['score']}/{f['max']} — {f['detail']}")
                        st.markdown(f"**结论:** {bs['reason']}")
                        st.markdown("</div>", unsafe_allow_html=True)
                        fix_txt=format_fix_summary(bs["factors"])
                        if fix_txt: st.warning(fix_txt)
                    with cols[1]:
                        st.markdown(f"<div style='border:1px solid {sell_color};border-radius:6px;padding:8px'>", unsafe_allow_html=True)
                        st.markdown(f"**卖出评分: {ss['score']}/{ss['max_score']}**")
                        for f in ss["factors"]:
                            pct = f["score"] / f["max"] * 100 if f["max"] > 0 else 0
                            ic = "🟢" if pct >= 70 else "🟡" if pct >= 40 else "🔴"
                            st.markdown(f"{ic} **{f['name']}**: {f['score']}/{f['max']} — {f['detail']}")
                        st.markdown(f"**结论:** {ss['reason']}")
                        st.markdown("</div>", unsafe_allow_html=True)
                        if bsc or h250:
                            st.caption(f"买入基准:{bsc or '无'} | 250日高:{h250 or '无'}")

            st.markdown("---")
            st.markdown("### 🌑 成长股独立评估")
            st.caption("适用: 腾讯/苹果/英伟达/微软 —— 替代李氏评分+股息率，专用于低股息高增长标的")
            _growth_codes = ["00700", "AAPL", "NVDA", "MSFT"]
            _gsub = df[df["code"].isin(_growth_codes)] if df is not None and not df.empty else pd.DataFrame()
            if not _gsub.empty:
                for _, _gr in _gsub.iterrows():
                    _gc = _gr["code"]; _gnm = _gr["name"]
                    _gs_score = _gr.get("lishing", 0) or 0
                    _gs_factors = _gr.get("growth_factors", [])

                    _has_data = any(f.get("score", 0) > 0 for f in _gs_factors)

                    _gc_ = "#00ff88" if _gs_score >= 60 else "#ffaa00" if _gs_score >= 40 else "#ff4444"
                    _ge = "🟢" if _gs_score >= 60 else "🟡" if _gs_score >= 40 else "🔴"
                    _gl = "强势买入" if _gs_score >= 80 else "可持有" if _gs_score >= 60 else "观望" if _gs_score >= 40 else "回避"

                    with st.expander(f"{_ge} {_gnm} | 成长评估: {_gs_score:.0f}/100 {_gl}", False):
                        _ca, _cb = st.columns([1, 1])
                        with _ca:
                            st.markdown(f"<div style='border:1px solid {_gc_};border-radius:6px;padding:8px'>", unsafe_allow_html=True)
                            st.markdown(f"**成长股评分: {_gs_score:.0f}/100**")
                            if _gs_factors:
                                for _f in _gs_factors:
                                    _p = _f["score"] / _f["max"] * 100 if _f["max"] > 0 else 0
                                    _e = "🟢" if _p >= 60 else "🟡" if _p >= 40 else "🔴"
                                    st.markdown(f"{_e} **{_f['name']}**: {_f['score']}/{_f['max']} — {_f.get('detail', '')}")
                            elif not _has_data:
                                st.caption("⚠️ 历史数据不足，评分基于手动兜底数据")
                            st.markdown("</div>", unsafe_allow_html=True)
                        with _cb:
                            _ga = "🟢 强势买入" if _gs_score >= 80 else "🟢 可持有" if _gs_score >= 60 else "🟡 观望" if _gs_score >= 40 else "🔴 回避"
                            st.info(f"**建议**: {_ga}")
                            if not _has_data:
                                st.caption("数据源不可用时使用手动估值分位+趋势代理")
            else:
                st.caption("无成长股持仓")



def render_t4(t4):
    with t4:
        st.markdown("### \U0001F3AF 三场景回测")
        st.caption("⚠️ 本回测基于历史后复权数据, 存在幸存者偏差; 单边交易成本0.1%")
        c1,c2=st.columns(2)
        with c1: bt_years=st.slider("回测年数",1,10,5,1,key="bt_years")
        with c2: bt_cap=st.slider("单只上限",10,50,30,5,key="bt_cap")
        bt_btn=st.button("\U0001F680 开始回测",type="primary",key="bt_run")

        if bt_btn:
            with st.spinner("回测中(约30秒)..."):
                fins=fetch_fins(list(PORT_A.keys()))
                scenarios=backtest_scenarios(PORT_A,PORT_HK,fins,years=bt_years)
                st.session_state.bt_result=scenarios

        res=st.session_state.get("bt_result",{})
        if res:
            for sc in ["bear","base","bull"]:
                if sc not in res: continue
                m=res[sc]["metrics"]
                label={"bull":"📈 乐观(高ROE加权)","base":"📊 基准(当前持仓)","bear":"📉 悲观(剔除低ROE)"}[sc]
                st.subheader(label)
                k1,k2,k3,k4,k5=st.columns(5)
                k1.metric("总收益","%.1f%%"%m["total_return"])
                k2.metric("年化","%.1f%%"%m["annual_return_net"])
                k3.metric("夏普","%.2f"%m["sharpe"])
                k4.metric("最大回撤","%.1f%%"%m["max_drawdown"])
                k5.metric("月度胜率","%.0f%%"%m["win_rate"])
                nav=res[sc]["nav"]
                # Alpha/Beta 收益归因拆解
                if sc == "base":
                    st.divider()
                    st.markdown("#### \U0001F52C Alpha/Beta 收益归因")
                    st.caption("个股选股能力 vs 市场敞口分离检验 — 参考 frank-quant 视频③ 方法论")
                    bt_codes = list(PORT_A.keys())
                    bt_weights = {c: PORT_A.get(c, {}).get("ratio", 0) for c in bt_codes}
                    bt_hist = fetch_histories(bt_codes, bt_years)
                    if bt_hist:
                        bt_returns = pd.DataFrame({c: bt_hist[c]["close"].pct_change() for c in bt_hist}).dropna()
                        ab = decompose_portfolio(bt_returns, bt_weights)
                        if ab:
                            k_alpha, k_beta, k_info = st.columns(3)
                            k_alpha.metric("Alpha 贡献", f'{ab["alpha_contribution_pct"]:.1f}%',
                                delta=f'{ab["alpha_ratio_pct"]}%占比', delta_color="normal")
                            k_beta.metric("Beta 贡献(剩余)", f'{ab["beta_ratio_pct"]}%',
                                delta="市场敞口", delta_color="off")
                            total_annual = ab["portfolio_annualized_ret"]
                            k_info.caption(f"组合年化: {total_annual}%")
                            if total_annual < 0:
                                k_info.caption("⚠️ 收益为负, Alpha/Beta 占比按贡献绝对值口径展示")
                            st.markdown("**Alpha 排名(年化%)**")
                            ab_rows = []
                            for s in ab["stocks"]:
                                ab_rows.append({
                                    "代码": s["code"],
                                    "权重%": s.get("weight_pct"),
                                    "Alpha年化%": s["alpha_annualized"],
                                    "Alpha贡献%": s.get("alpha_contribution_annual_pct"),
                                    "Beta系数": s["beta"],
                                    "R²": s["r_squared"],
                                    "特质波动%": s["vol_idiosyncratic"],
                                })
                            st.dataframe(pd.DataFrame(ab_rows), width="stretch", hide_index=True, column_config={
                                "权重%": st.column_config.NumberColumn(format="%.1f%%"),
                                "Alpha年化%": st.column_config.NumberColumn(format="%+.1f%%"),
                                "Alpha贡献%": st.column_config.NumberColumn(format="%+.2f%%"),
                                "Beta系数": st.column_config.NumberColumn(format="%.3f"),
                                "R²": st.column_config.NumberColumn(format="%.3f"),
                                "特质波动%": st.column_config.NumberColumn(format="%.1f%%"),
                            })
                            alphas = [s["alpha_annualized"] for s in ab["stocks"]]
                            betas = [s["beta"] for s in ab["stocks"]]
                            alpha_pos = sum(1 for a in alphas if a > 0)
                            alpha_neg = sum(1 for a in alphas if a < 0)
                            st.caption(f"共 {len(alphas)} 只持仓: " +
                                f"选股正贡献 {alpha_pos} 只、负贡献 {alpha_neg} 只; " +
                                f"Beta 平均 {sum(betas)/len(betas):.2f}")
                st.line_chart(nav)


def render_t5(t5):
    with t5:
        st.markdown("### ⚙️ 组合优化 + 压力测试")
        if df is not None and not df.empty:
            codes_a=df[df["group"].isin(["A股持仓"])]["code"].tolist()
            if len(codes_a)>=2:
                opt_btn=st.button("🔄 刷新数据", key="opt_refresh")
                if opt_btn or "opt_returns" not in st.session_state:
                    if opt_btn or st.session_state.get("opt_returns") is None:
                        with st.spinner("获取历史数据..."):
                            hist_dict=fetch_histories(codes_a, 5)
                            returns=pd.DataFrame({c:hist_dict[c]["close"].pct_change() for c in hist_dict}).dropna()
                            weights={c:PORT_A.get(c,{}).get("ratio",0) for c in codes_a}
                            st.session_state.opt_returns = returns
                            st.session_state.opt_weights = weights
                returns=st.session_state.get("opt_returns")
                weights=st.session_state.get("opt_weights")
                if returns is not None and not returns.empty and weights:
                    import numpy as np
                    from modules.optimizer import port_ret, port_vol, calc_cov
                    codes_used=[c for c in codes_a if c in returns.columns]
                    st.subheader("🎯 组合优化")
                    use_factor_tilt = st.checkbox("🎯 因子动量模式", value=True, help="根据因子IC/IR倾斜期望收益")
                    factor_ir_dict = {}
                    if use_factor_tilt:
                        from modules.learner import get_learner
                        _l_ = get_learner()
                        for _c_ in codes_used:
                            _m_ = _l_.load(_c_)
                            if _m_ and _m_.get("factors"):
                                irs = [f["ir"] for f in _m_["factors"].values() if isinstance(f.get("ir"), (int, float))]
                                factor_ir_dict[_c_] = sum(irs)/len(irs) if irs else 0.0
                    m_map={"最大夏普":"max_sharpe","最小波动率":"min_vol","风险平价":"risk_parity"}
                    with st.spinner("计算三种优化目标..."):
                        res_all={}
                        for _label,_m in m_map.items():
                            res_all[_label]=optimize(returns,codes_used,method=_m,factor_ir=factor_ir_dict if factor_ir_dict else None)

                    mu=returns.mean().values*252
                    S=calc_cov(returns)
                    def _metrics(w):
                        r=float(port_ret(w,mu)); v=float(port_vol(w,S))
                        sh=(r-0.02)/v if v>1e-8 else 0.0
                        return r,v,sh
                    w_now=np.array([weights.get(c,0)/100 for c in codes_used])
                    rn,vn,sn=_metrics(w_now)

                    meth_meta={}
                    for _label in m_map:
                        ow=res_all[_label].get("optimal_weights",{})
                        w_arr=np.array([ow.get(c,0)/100 for c in codes_used])
                        r,v,sh=_metrics(w_arr)
                        top3=sorted(ow.items(),key=lambda x:-x[1])[:3]
                        top_str=", ".join(f"{PORT_A.get(c,{}).get('name',c)}{w:.0f}%" for c,w in top3)
                        meth_meta[_label]=(ow,res_all[_label].get("confidence",{}),r,v,sh,top_str)

                    st.markdown("#### 📊 三种目标对比")
                    meth_rows=[{"优化目标":_label,"年化收益%":round(meth_meta[_label][2]*100,2),
                                "年化波动%":round(meth_meta[_label][3]*100,2),"夏普":round(meth_meta[_label][4],2),
                                "前三大重仓":meth_meta[_label][5]} for _label in m_map]
                    st.dataframe(pd.DataFrame(meth_rows),width="stretch",hide_index=True,column_config={
                        "年化收益%":st.column_config.NumberColumn(format="%.2f%%"),
                        "年化波动%":st.column_config.NumberColumn(format="%.2f%%"),
                        "夏普":st.column_config.NumberColumn(format="%.2f"),
                    })
                    st.caption("约束: 单只上限30%, 组合权重和=100%; 95%CI 来自 Monte Carlo 1000次权重分布")
                    k1,k2,k3=st.columns(3)
                    k1.metric("当前组合年化收益",f"{rn*100:.2f}%")
                    k2.metric("当前组合波动率",f"{vn*100:.2f}%")
                    k3.metric("当前组合夏普",f"{sn:.2f}")

                    st.divider()
                    for _label in m_map:
                        ow,ci,r,v,sh,_=meth_meta[_label]
                        st.markdown(f"#### 🎯 {_label}（年化 {r*100:.2f}% · 波动 {v*100:.2f}% · 夏普 {sh:.2f}）")
                        wrows=[]
                        for c in codes_used:
                            cilo,cihi=(ci.get(c) or (None,None))
                            wrows.append({
                                "代码":c,"名称":PORT_A.get(c,{}).get("name",c),
                                "当前权重%":round(float(weights.get(c,0)),1),
                                "权重%":ow.get(c),
                                "95%CI低%":cilo,"95%CI高%":cihi,
                                "因子IR":round(float(factor_ir_dict.get(c,0)),3) if factor_ir_dict else None,
                            })
                        st.dataframe(pd.DataFrame(wrows),width="stretch",hide_index=True,column_config={
                            "当前权重%":st.column_config.NumberColumn(format="%.1f%%"),
                            "权重%":st.column_config.NumberColumn(format="%.1f%%"),
                            "95%CI低%":st.column_config.NumberColumn(format="%.1f%%"),
                            "95%CI高%":st.column_config.NumberColumn(format="%.1f%%"),
                            "因子IR":st.column_config.NumberColumn(format="%.3f"),
                        })

                    ef=res_all["最大夏普"].get("efficient_frontier",[])
                    st.markdown("#### 📈 有效前沿")
                    if ef:
                        ef_df=pd.DataFrame(ef,columns=["波动率%","年化收益%"]).drop_duplicates().sort_values("波动率%").set_index("波动率%")
                        st.line_chart(ef_df)
                        pts=" · ".join(f"{_label}: {meth_meta[_label][3]*100:.1f}%/{meth_meta[_label][2]*100:.1f}%" for _label in m_map)
                        st.caption(f"前沿 {len(ef_df)} 个点 (单只≤30%, 三种目标共用一条前沿); 各目标最优点(波动/收益): {pts}")
                    else:
                        st.info("有效前沿无可行解")

                    st.markdown("#### 🧮 个股统计（5年年化）")
                    stat_rows=[]
                    for c in codes_used:
                        r_=returns[c].dropna()
                        ar=float(r_.mean())*252*100
                        av=float(r_.std()*np.sqrt(252))*100
                        as_=(ar-2.5)/av if av>0 else 0
                        row={"代码":c,"名称":PORT_A.get(c,{}).get("name",c),
                             "年化收益%":round(ar,2),"年化波动%":round(av,2),"夏普":round(as_,2),
                             "当前权重%":float(weights.get(c,0))}
                        for _label in m_map:
                            row[_label]=meth_meta[_label][0].get(c)
                        stat_rows.append(row)
                    st.dataframe(pd.DataFrame(stat_rows),width="stretch",hide_index=True,column_config={
                        "年化收益%":st.column_config.NumberColumn(format="%.2f%%"),
                        "年化波动%":st.column_config.NumberColumn(format="%.2f%%"),
                        "夏普":st.column_config.NumberColumn(format="%.2f"),
                        "当前权重%":st.column_config.NumberColumn(format="%.1f%%"),
                    })

                    st.markdown("#### 🔗 相关系数矩阵")
                    corr=returns.corr().round(2)
                    corr_disp=corr.copy()
                    name_of=lambda c: PORT_A.get(c,{}).get("name",c)
                    corr_disp.columns=[name_of(c) for c in corr.columns]
                    corr_disp.index=[name_of(c) for c in corr.index]
                    st.dataframe(corr_disp,width="stretch")

                    st.divider()
                    st.subheader("🌪️ 黑天鹅压力测试")
                    st.caption("基于当前持仓权重")
                    sd=single_day(codes_used,weights)
                    for s in sd:
                        st.error(f"**{s['scenario']}**: 组合影响 {s['impact']:.2f}%")
                    ic=industry_collapse(weights)
                    for s in ic:
                        st.warning(f"**{s['scenario']}**: 组合影响 {s['impact']:.2f}%")
                    cb=correlation_break(codes_used,weights,returns)
                    if "error" not in cb:
                        st.info(f"**{cb['scenario']}**: 原波动率 {cb['original_vol']:.2f}% → 压力波动率 {cb['stressed_vol']:.2f}% (+{cb['increase']:.2f}pp)")
                    mc=monte_carlo_stress(returns,weights)
                    if "error" not in mc:
                        st.write(f"**Monte Carlo ({mc['scenario']})**")
                        m1,m2,m3,m4,m5=st.columns(5)
                        m1.metric("1%分位",f"{mc['1pct']:.2f}%")
                        m2.metric("5%分位",f"{mc['5pct']:.2f}%")
                        m3.metric("10%分位",f"{mc['10pct']:.2f}%")
                        m4.metric("均值",f"{mc['mean']:.2f}%")
                        m5.metric("中位数",f"{mc['median']:.2f}%")
                else:
                    st.info("点击上方按钮获取数据并运行分析")
            else:
                st.info("A股持仓不足2只, 无法优化")
        else:
            st.info("请先刷新持仓数据")


def render_t6(t6):
    with t6:
        st.markdown("### \u2139\uFE0F 全局假设声明")
        st.warning("""
        1. PE/PB: 动态TTM, 非静态年报值
        2. 波动率: 120日对数收益率年化标准差, 非已实现波动率
        3. 美股/港股股息率: 使用手动验证数据, API作为兜底
        """)
        st.markdown("""
        **数据源**: 东方财富/同花顺 via akshare, 港股 via 腾讯接口, 美股 via yfinance
        **李氏评分**: 9维度(PE/负债率/ROE/分红/现金流/PB/市值/审计/质押)+ 可选关联交易
        **Kelly**: Fractional Kelly Criterion, 继承FinceptTerminal实现
        **成长评分**: 5维度(估值分位/趋势结构/Kelly信号/波动率校准/趋势稳定性), 专用于美股
        **红利低波轮动**: 参考EasyXT视频策略, DuckDB缓存加速
        **网格交易**: 固定网格 + ATR动态网格, 支持历史回测
        **Alpha因子**: WorldQuant Alpha101 核心因子, 自动训练+进化轨迹分析
        **回测**: 基于历史后复权, 单边0.1%交易成本, 三场景(乐观/基准/悲观)
        **组合优化**: scipy.optimize, 最大夏普/最小波动率/风险平价, Monte Carlo 95%置信区间
        **压力测试**: 单日暴跌/行业全灭/相关性断裂/Monte Carlo极端模拟
        **缓存**: 行情1h / 财务30d / 分红90d, 存储在data/目录
        **并发限制**: AKShare最多3并发, 指数退避重试
        """)
        st.markdown("""
        **生产使用说明（个人实盘）**:
        1. Kelly/信号均为参考区间，不是精确仓位，不构成投资建议
        2. A/HK/US/REIT 按独立分层管理，各层投入上限与回撤红线在 `config.json` 的 `layers` 中配置
        3. 数据过期或使用手动兜底时，页面顶部会显式提示，请勿忽略
        4. 后台预警由独立进程 `python -m modules.alert_worker` 运行
        """)

        st.divider()
        st.subheader("🔔 预警设置")
        st.caption("配置多通道预警, 实时监控组合风险")
        sender=AlertSender()
        ch=st.selectbox("通知渠道",["邮件","企业微信","Telegram","Bark","Webhook"],0,key="alert_ch")
        ch_map={"邮件":"email","企业微信":"wechat","Telegram":"telegram","Bark":"bark","Webhook":"webhook"}
        with st.form("alert_cfg"):
            if ch=="邮件":
                st.caption("发件邮箱用来登录SMTP发信, 收件邮箱接收预警。可以填同一个。推荐用QQ邮箱的SMTP授权码")
                st.text_input("SMTP服务器",value=sender.cfg.get("smtp_server",""),key="smtp_s",placeholder="smtp.qq.com")
                st.text_input("SMTP端口",value=sender.cfg.get("smtp_port","465"),key="smtp_p",placeholder="465")
                st.text_input("发件邮箱(SMTP登录)",value=sender.cfg.get("smtp_user",""),key="smtp_u",placeholder="your@qq.com")
                st.text_input("SMTP授权码",value=sender.cfg.get("smtp_pass",""),type="password",key="smtp_pw",placeholder="16位授权码")
                st.text_input("收件邮箱(接收预警)",value=sender.cfg.get("alert_to",""),key="smtp_to",placeholder="your@qq.com")
            elif ch=="企业微信":
                st.text_input("Webhook URL",value=sender.cfg.get("wechat_webhook",""),key="wc_url")
            elif ch=="Telegram":
                st.text_input("Bot Token",value=sender.cfg.get("tg_token",""),key="tg_tok")
                st.text_input("Chat ID",value=sender.cfg.get("tg_cid",""),key="tg_cid")
            elif ch=="Bark":
                st.caption("推送地址: 公共 api.day.app 或自建服务器 http://NAS_IP:8080/设备Key/ ; 若填写16位加密密钥则启用AES加密推送")
                st.text_input("Bark URL",value=sender.cfg.get("bark_url",""),key="bk_url",placeholder="http://192.168.31.24:8080/设备Key/")
                st.text_input("加密密钥KEY(16字节, 可选)",value=sender.cfg.get("bark_key",""),key="bk_key",placeholder="1234567890123456",type="password")
                st.text_input("加密IV(16字符, 与App设置一致, 可选)",value=sender.cfg.get("bark_iv",""),key="bk_iv",placeholder="1234567890123456")
                if sender.cfg.get("bark_key"):
                    st.success("🔒 已启用 AES-128-CBC 加密推送, 内容对服务器不可见")
            else:
                st.text_input("Webhook URL",value=sender.cfg.get("webhook_url",""),key="wh_url")
            st.number_input("股价跌幅阈值(%)",0.0,20.0,8.0,0.5,key="th_drop")
            st.number_input("股息率变动阈值(%)",0.0,5.0,0.5,0.1,key="th_dy")
            st.number_input("Kelly变化阈值(%)",0.0,20.0,5.0,1.0,key="th_kelly")
            st.number_input("评分下降阈值",0.0,30.0,10.0,1.0,key="th_ls")
            if st.form_submit_button("保存配置"):
                sender.update_cfg(
                    smtp_server=st.session_state.get("smtp_s",""),smtp_port=st.session_state.get("smtp_p","465"),
                    smtp_user=st.session_state.get("smtp_u",""),smtp_pass=st.session_state.get("smtp_pw",""),
                    alert_to=st.session_state.get("smtp_to",""),
                    wechat_webhook=st.session_state.get("wc_url",""),
                    tg_token=st.session_state.get("tg_tok",""),tg_chat_id=st.session_state.get("tg_cid",""),
                    webhook_url=st.session_state.get("wh_url",""),
                    bark_url=st.session_state.get("bk_url",""),
                    bark_key=st.session_state.get("bk_key",""),
                    bark_iv=st.session_state.get("bk_iv",""),
                )
                st.success("✅ 配置已保存")
        if st.button("发送测试邮件",key="test_alert"):
            ok=sender.send("测试预警","这是一条测试预警消息",ch_map[ch])
            st.success("✅ 已发送") if ok else st.error("❌ 发送失败, 请检查配置")
        # 预警历史
        hist=load_alert_history()
        if not hist.empty:
            st.subheader("📋 预警历史")
            st.dataframe(hist.tail(50),width="stretch",hide_index=True)
        else:
                st.info("暂无预警记录")

    # ── REIT/ETF 分析 tab ──
    # 注意：内部全部使用 fund_df 变量，避免覆盖全局 df

def render_t7(t7):
    with t7:
        st.markdown("### \U0001F4B9 REIT / ETF 分析")
        mode = st.radio("分析模式", ["REITs 公募REITs", "ETF 场内基金"], horizontal=True, key="fund_mode")

        should_auto_load = "fund_result" not in st.session_state

        with st.expander("添加代码", expanded=True):
            codes_input = st.text_input("输入代码（逗号分隔）", placeholder="508068, 508098, 518600", key="fund_codes")
            col_a, col_b, col_c = st.columns(3)
            with col_a:
                if st.button("\U0001F50D 查询", key="fund_query"):
                    if not codes_input.strip():
                        st.warning("请输入代码")
                        st.stop()
                    raw = [c.strip() for c in codes_input.split(",") if c.strip()]
                    with st.spinner("获取数据..."):
                        mkt = fetch_market_prices(raw)
                        if mode.startswith("REIT"):
                            reits = fetch_reit_list()
                            fund_df = reits[reits["code"].isin(raw)].copy()
                            navs = fetch_fund_navs(raw)
                            for c in navs:
                                fund_df.loc[fund_df["code"] == c, "nav"] = navs[c]
                        else:
                            etfs = fetch_etf_list()
                            fund_df = etfs[etfs["code"].isin(raw)].copy()
                        fund_df = enrich_with_market(fund_df, mkt)
                        fund_df = enrich_with_premium(fund_df)
                        try:
                            div_all = fetch_fund_dividends()
                            div_sum = _parse_dividend_summary(div_all, raw)
                            fund_df = enrich_with_dividend(fund_df, div_sum)
                        except Exception:
                            fund_df["total_div"] = 0; fund_df["div_count"] = 0; fund_df["div_yield"] = None
                    st.session_state["fund_result"] = fund_df
            with col_b:
                if st.button("\U0001F4C2 加载全部REITs", key="load_reits"):
                    with st.spinner("加载REITs列表..."):
                        reits = fetch_reit_list()
                        codes_list = reits["code"].tolist()
                        mkt = fetch_market_prices(codes_list)
                        fund_df = reits.copy()
                        fund_df = enrich_with_market(fund_df, mkt)
                        fund_df = enrich_with_premium(fund_df)
                        try:
                            div_all = fetch_fund_dividends()
                            div_sum = _parse_dividend_summary(div_all, codes_list)
                            fund_df = enrich_with_dividend(fund_df, div_sum)
                        except Exception:
                            fund_df["total_div"] = 0; fund_df["div_count"] = 0; fund_df["div_yield"] = None
                    st.session_state["fund_result"] = fund_df
            with col_c:
                if st.button("\U0001F4C2 加载全部ETF", key="load_etfs"):
                    with st.spinner("加载ETF列表..."):
                        etfs = fetch_etf_list()
                        codes_list = etfs["code"].tolist()
                        mkt = fetch_market_prices(codes_list)
                        fund_df = etfs.copy()
                        fund_df = enrich_with_market(fund_df, mkt)
                        fund_df = enrich_with_premium(fund_df)
                    st.session_state["fund_result"] = fund_df

        if should_auto_load:
            if mode.startswith("REIT"):
                with st.spinner("加载REITs..."):
                    target_codes = ["508098", "508068"]
                    reits = fetch_reit_list()
                    df_auto = reits[reits["code"].isin(target_codes)].copy()
                    mkt = fetch_market_prices(target_codes)
                    navs = fetch_fund_navs(target_codes)
                    for c in navs:
                        df_auto.loc[df_auto["code"] == c, "nav"] = navs[c]
                    df_auto = enrich_with_market(df_auto, mkt)
                    df_auto = enrich_with_premium(df_auto)
                    try:
                        div_all = fetch_fund_dividends()
                        div_sum = _parse_dividend_summary(div_all, target_codes)
                        df_auto = enrich_with_dividend(df_auto, div_sum)
                    except Exception:
                        df_auto["total_div"] = 0; df_auto["div_count"] = 0; df_auto["div_yield"] = None
                st.session_state["fund_result"] = df_auto
            else:
                with st.spinner("加载ETF列表..."):
                    etfs = fetch_etf_list()
                    codes_list = etfs["code"].tolist()
                    mkt = fetch_market_prices(codes_list)
                    df_auto = etfs.copy()
                    df_auto = enrich_with_market(df_auto, mkt)
                    df_auto = enrich_with_premium(df_auto)
                st.session_state["fund_result"] = df_auto

        fund_df = st.session_state.get("fund_result")
        if fund_df is not None and not fund_df.empty:
            col_d, col_e = st.columns([3, 1])
            with col_e:
                show_all = st.toggle("显示全部", False, key="fund_show_all")
                if "premium" in fund_df.columns:
                    filter_opt = st.selectbox("筛选", ["全部", "仅溢价", "仅折价"], key="fund_filter")
                    if filter_opt == "仅溢价":
                        fund_df = fund_df[fund_df["premium"] > 0].copy()
                    elif filter_opt == "仅折价":
                        fund_df = fund_df[fund_df["premium"] < 0].copy()
                sort_by = st.selectbox("排序", ["代码", "价格", "折溢价率", "股息率"],
                                       key="fund_sort")
                ascending = st.checkbox("升序", False, key="fund_asc")
            display = fund_df.copy()
            if "类型" in display.columns and not show_all:
                grp = display.groupby("类型").apply(lambda x: x.head(3)).reset_index(drop=True)
                display = grp
            cols = ["code", "name"]
            if "类型" in display.columns:
                cols.append("类型")
            cols += ["price", "chg"]
            if "nav" in display.columns and "premium" in display.columns:
                cols += ["nav", "premium"]
            if "mcap" in display.columns:
                cols.append("mcap")
            if "div_yield" in display.columns:
                cols.append("div_yield")
            if "fund_type" in display.columns:
                cols.append("fund_type")
            cols = [c for c in cols if c in display.columns]
            sort_map = {"代码": "code", "价格": "price", "折溢价率": "premium", "股息率": "div_yield"}
            s_col = sort_map.get(sort_by, "code")
            if s_col in display.columns:
                display = display.sort_values(s_col, ascending=ascending)
            d = display[cols].copy()
            d.columns = ["代码", "名称"] + [c for c in d.columns[2:]]
            # None -> \"-\"
            for col in d.columns:
                if d[col].dtype == 'float64' or d[col].dtype == 'object':
                    d[col] = d[col].apply(lambda x: "-" if pd.isna(x) else x)
            st.dataframe(d, width="stretch", hide_index=True)
            if not display.empty:
                avg_premium = display["premium"].dropna().mean() if "premium" in display.columns else None
                avg_div = display["div_yield"].dropna().mean() if "div_yield" in display.columns else None
                c1, c2 = st.columns(2)
                if avg_premium is not None and pd.notna(avg_premium):
                    c1.metric("平均折溢价率", f"{avg_premium:.2f}%")
                if avg_div is not None and pd.notna(avg_div):
                    c2.metric("平均股息率", f"{avg_div:.2f}%")
        else:
            if "fund_result" in st.session_state:
                st.info("未匹配到数据，请检查代码是否正确")


def render_t8(t8):
    with t8:
        st.markdown("### \U0001F4B0 持有成本分层管理")
        st.caption("按成本股息率 vs 当前股息率的比值，将每只标的分为三档：🟡黄金(≥1.2) → 可加仓 · ⚪白银(0.9~1.2) → 持有 · 🔴警戒(<0.9) → 暂停加仓")
        st.caption("⚠️ 成本数据需手动录入你的实际建仓/加仓记录（日期+股数+买入价）")

        cost_data = ct_load()
        if df is not None and not df.empty:
            all_codes = df["code"].tolist()
            if "group" not in df.columns:
                groups = [""]
            else:
                groups = ["A股持仓", "港股持仓"]
            for grp in groups:
                if grp:
                    sub = df[df["group"] == grp]
                    if sub.empty:
                        continue
                else:
                    sub = df
                ic = {"A股持仓": "\U0001F1E6\U0001F1F0", "港股持仓": "\U0001F1ED\U0001F1F0"}.get(grp, "\U0001F4CA")
                if grp:
                    st.subheader(ic + " " + grp)
                for _, rr in sub.iterrows():
                    code = rr["code"]
                    name = rr["name"]
                    if "price" not in rr or pd.isna(rr["price"]):
                        continue
                    price = float(rr["price"])
                    stock = cost_data.get(code, {"batches": [], "dividend_per_share": 0.0})
                    overall = calc_overall_tier(code, price)
                    has_data = bool(stock.get("batches")) and stock.get("dividend_per_share", 0) > 0
                    if not has_data:
                        with st.expander(f"{name} ({code}) — ⚪ 请录入建仓/加仓数据", expanded=False):
                            st.markdown(f"**当前股价**: {price:.2f}")
                            st.markdown(f"**手动股息率**: {MANUAL_DIV_YIELDS.get(code, {}).get('ttm', MANUAL_DIV_YIELDS.get(code, {}).get('lfy', 0)):.2f}%")
                            st.caption("录入你的实际买入批次：日期、股数、买入价")
                            with st.form(f"quick_init_{code}"):
                                q1, q2, q3 = st.columns(3)
                                with q1: q_date = st.text_input("买入日期", placeholder="2025-01-15", value="", key=f"qi_d_{code}")
                                with q2: q_shares = st.number_input("股数", min_value=0, step=100, value=1000, key=f"qi_s_{code}")
                                with q3: q_cost = st.number_input("买入价", min_value=0.0, step=0.01, value=price, key=f"qi_c_{code}")
                                q_note = st.text_input("备注", placeholder="建仓/加仓", key=f"qi_n_{code}")
                                if st.form_submit_button("✅ 确认录入"):
                                    if q_date and q_shares > 0 and q_cost > 0:
                                        add_batch(code, q_date, int(q_shares), float(q_cost), q_note)
                                        dps = MANUAL_DIV_YIELDS.get(code, {}).get("ttm", MANUAL_DIV_YIELDS.get(code, {}).get("lfy", 0)) / 100 * q_cost
                                        update_dividend(code, dps)
                                        set_dividend_source(code, "manual")
                                        st.success(f"✅ 已录入: {q_shares}股 @ {q_cost:.2f}，每股分红 {dps:.4f}")
                                        st.rerun()
                                    else:
                                        st.error("请填写完整的日期、股数、买入价")
                        continue
                    tier_color = {"黄金": "#ffd700", "白银": "#cccccc", "警戒": "#ff4444"}.get(overall["tier"], "#888")
                    tier_icon = {"黄金": "\U0001F7E1", "白银": "\u26AA", "警戒": "\U0001F534"}.get(overall["tier"], "⚪")
                    with st.expander(
                        f"{tier_icon} {name} | 整体：{overall['tier']} | 均价 {overall['avg_cost']} | "
                        f"成本息率 {overall['cost_dy']}% | 比值 {overall['ratio']}",
                        expanded=False
                    ):
                        col_a, col_b, col_c, col_d = st.columns(4)
                        col_a.metric("整体档位", overall["tier"], delta_color="off")
                        col_b.metric("加权均价", f"{overall['avg_cost']:.2f}")
                        col_c.metric("成本股息率", f"{overall['cost_dy']:.2f}%")
                        col_d.metric("比值", f"{overall['ratio']:.2f}")
                        st.markdown(
                            f"<div style='border-left:4px solid {tier_color};padding:8px 12px;margin:8px 0'>"
                            f"<b>建议</b>：{ '✅ 整体处于黄金档，新增资金可加仓' if overall['tier']=='黄金' else '⏸️ 整体处于白银档，持有不动' if overall['tier']=='白银' else '⛔ 整体处于警戒档，暂不加仓，等待成本摊薄' }"
                            f"</div>", unsafe_allow_html=True
                        )
                        div_source = stock.get("dividend_source", "auto")
                        auto_div = 0
                        if div_source == "auto":
                            if grp == "港股持仓":
                                div_map = _cached_fetch_divs_hk(tuple(all_codes))
                            else:
                                div_map = _cached_fetch_divs(tuple(all_codes))
                            _div_info = div_map.get(code, 0); auto_div = _div_info.get("ttm", 0) if isinstance(_div_info, dict) else (_div_info or 0)
                        cur_div = stock.get("dividend_per_share", auto_div)
                        st.caption(f"每股分红：{'自动' if div_source=='auto' else '手动'} {cur_div:.4f}")
                        if div_source == "auto" and auto_div > 0 and cur_div != auto_div:
                            update_dividend(code, auto_div)
                            st.rerun()
                        col_e, col_f = st.columns([1, 1])
                        with col_e:
                            new_div = st.number_input(
                                "每股分红（手动覆盖）", value=float(cur_div), format="%.4f",
                                key=f"div_{code}", step=0.01
                            )
                            if st.button("应用分红", key=f"set_div_{code}"):
                                update_dividend(code, new_div)
                                set_dividend_source(code, "manual" if new_div != auto_div else "auto")
                                st.rerun()
                        with col_f:
                            if st.button("\U0001F504 恢复自动读取", key=f"auto_div_{code}"):
                                update_dividend(code, auto_div)
                                set_dividend_source(code, "auto")
                                st.rerun()
                        st.markdown("**批次明细**")
                        bt_data = []
                        for b in overall.get("batches", []):
                            bt_data.append({
                                "日期": b.get("date", ""),
                                "股数": b["shares"],
                                "成本": b["cost"],
                                "成本息率": f"{b['cost_dy']:.2f}%",
                                "比值": f"{b['ratio']:.2f}",
                                "档位": b["tier"],
                                "备注": b.get("note", ""),
                            })
                        if bt_data:
                            df_batches = pd.DataFrame(bt_data)
                            st.dataframe(df_batches, width="stretch", hide_index=True)
                        with st.expander("新增批次 / 管理"):
                            c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
                            with c1:
                                b_date = st.text_input("买入日期", placeholder="2025-01-15", key=f"bd_{code}")
                            with c2:
                                b_shares = st.number_input("股数", min_value=0, step=100, key=f"bs_{code}")
                            with c3:
                                b_cost = st.number_input("买入成本(元/股)", min_value=0.0, step=0.01, key=f"bc_{code}")
                            with c4:
                                b_note = st.text_input("备注", placeholder="建仓/加仓", key=f"bn_{code}")
                            if st.button("\U0001F4E4 添加批次", key=f"add_batch_{code}"):
                                add_batch(code, b_date, int(b_shares), float(b_cost), b_note)
                                st.success(f"已添加 {name} 批次: {b_shares}股 @ {b_cost}")
                                st.rerun()
                            if overall.get("batches"):
                                del_idx = st.number_input(
                                    "删除批次序号（从0开始）", min_value=0,
                                    max_value=len(overall["batches"]) - 1, value=0,
                                    key=f"del_{code}"
                                )
                                if st.button("\U0001F5D1 删除批次", key=f"del_btn_{code}"):
                                    delete_batch(code, int(del_idx))
                                    st.rerun()

        # 批量导入功能
        st.divider()
        st.markdown("### \U0001F4E5 批量导入成本数据")

        col1, col2 = st.columns(2)
        with col1:
            st.caption("方式一: 粘贴CSV数据")
            st.code("""格式: 代码,日期,股数,成本,备注
    示例:
    600900,2025-01-15,1000,12.50,建仓
    600900,2025-06-20,500,11.80,加仓
    601398,2025-03-10,2000,5.20,首次买入""")
            with st.form("batch_import"):
                import_text = st.text_area("粘贴CSV数据", height=150, key="import_text")
                if st.form_submit_button("导入"):
                    if import_text.strip():
                        lines = import_text.strip().split("\n")
                        success_count = 0
                        error_lines = []
                        for i, line in enumerate(lines, 1):
                            parts = line.split(",")
                            if len(parts) < 4:
                                error_lines.append(f"第{i}行: 格式错误")
                                continue
                            try:
                                code = parts[0].strip()
                                date = parts[1].strip()
                                shares = int(parts[2].strip())
                                cost = float(parts[3].strip())
                                note = parts[4].strip() if len(parts) > 4 else ""
                                add_batch(code, date, shares, cost, note)
                                success_count += 1
                            except Exception as e:
                                error_lines.append(f"第{i}行: {str(e)}")

                        if success_count > 0:
                            st.success(f"成功导入 {success_count} 条记录")
                        if error_lines:
                            st.warning(f"以下行导入失败:\n" + "\n".join(error_lines))
                    else:
                        st.warning("请输入数据")

        with col2:
            st.caption("方式二: 上传券商对账单 (自动识别买入记录)")
            uploaded_file = st.file_uploader("上传CSV/XLSX文件", type=["csv", "xlsx"])
            if uploaded_file is not None:
                try:
                    if uploaded_file.name.endswith(".xlsx"):
                        df_upload = pd.read_excel(uploaded_file)
                    else:
                        df_upload = pd.read_csv(uploaded_file)
                    if df_upload is None or df_upload.empty:
                        st.warning("文件为空或无数据")
                    else:
                        cols = [str(c) for c in df_upload.columns]
                        df_upload.columns = cols
                        def _find(*keys):
                            for c in cols:
                                low = str(c).lower()
                                if any(k in low for k in keys):
                                    return c
                            return None
                        def_code = _find("代码") or _find("code")
                        def_date = _find("成交日期") or _find("日期") or _find("date")
                        def_shares = _find("成交数量") or _find("数量") or _find("shares")
                        def_cost = _find("成交均价") or _find("成交价格") or _find("价格") or _find("cost")
                        def_dir = _find("委托方向") or _find("买卖") or _find("操作") or _find("方向") or _find("交易类型")

                        st.subheader("识别到的交易记录")
                        st.dataframe(df_upload.head(10))

                        st.caption("请确认列映射（已自动识别，可调整）")
                        col_options = cols
                        idx_map = {}
                        for key, d in [("code", def_code), ("date", def_date),
                                       ("shares", def_shares), ("cost", def_cost)]:
                            idx_map[key] = col_options.index(d) if d in col_options else 0
                        col_map = {}
                        col_map["code"] = st.selectbox("股票代码列", col_options, index=idx_map["code"], key="map_code")
                        col_map["date"] = st.selectbox("成交日期列", col_options, index=idx_map["date"], key="map_date")
                        col_map["shares"] = st.selectbox("成交数量列", col_options, index=idx_map["shares"], key="map_shares")
                        col_map["cost"] = st.selectbox("成交价格列", col_options, index=idx_map["cost"], key="map_cost")

                        st.caption("买入→成本分层批次；卖出→影子账户交易记录")
                        import_sells = st.checkbox("同时导入卖出记录（进入影子账户）", value=True,
                                                    help="需要已识别买卖方向列; 取消则只导入买入")
                        dir_col = st.selectbox("买卖方向列(可选)", [""] + col_options,
                                               index=(col_options.index(def_dir) + 1) if def_dir in col_options else 0,
                                               key="map_dir")

                        def _norm_code(v):
                            s = str(v).strip().upper()
                            if s.endswith(".0") and s[:-2].isdigit():
                                s = s[:-2]
                            for suf in (".SH", ".SZ", ".HK", ".US"):
                                if s.endswith(suf):
                                    s = s[:-3]
                            for pref in ("SH", "SZ", "HK"):
                                if s.startswith(pref) and s[2:].isdigit():
                                    s = s[2:]
                            return s.zfill(6) if s.isdigit() else s

                        if st.button("开始导入", key="file_import"):
                            from modules.shadow_account import add_sell
                            buy_success = 0
                            sell_success = 0
                            dup = 0
                            skipped_other = 0
                            errors = []
                            buys_net = {}  # code -> 本次导入净买入(股)
                            rows = []
                            for i, row in df_upload.iterrows():
                                try:
                                    dval = str(row.get(dir_col, "") or "") if dir_col else "买"
                                    is_buy = "买" in dval or "B" in dval.upper()
                                    is_sell = "卖" in dval or "S" in dval.upper()
                                    code = _norm_code(row[col_map["code"]])
                                    date = str(row[col_map["date"]]).strip()[:10]
                                    shares = int(float(row[col_map["shares"]]))
                                    price = float(row[col_map["cost"]])
                                    if not code or shares <= 0 or price <= 0:
                                        errors.append(f"第{i + 2}行: 代码/股数/价格无效")
                                        continue
                                    rows.append((is_buy, is_sell, code, date, shares, price))
                                except Exception as e:
                                    errors.append(f"第{i + 2}行: {e}")

                            for is_buy, is_sell, code, date, shares, price in rows:
                                if is_buy:
                                    buys_net[code] = buys_net.get(code, 0) + shares
                                if is_sell:
                                    buys_net[code] = buys_net.get(code, 0) - shares

                            for is_buy, is_sell, code, date, shares, price in rows:
                                try:
                                    if is_buy:
                                        if add_batch(code, date, shares, price, "导入"):
                                            buy_success += 1
                                        else:
                                            dup += 1
                                    elif is_sell and import_sells:
                                        if add_sell(code, date, shares, price, "导入"):
                                            sell_success += 1
                                        else:
                                            dup += 1
                                    else:
                                        skipped_other += 1
                                except Exception as e:
                                    errors.append(f"{code}: {e}")

                            oversells = {c: -n for c, n in buys_net.items() if n < 0}
                            if buy_success > 0:
                                st.success(f"成功导入 {buy_success} 条买入记录 → 成本分层")
                            if sell_success > 0:
                                st.success(f"成功导入 {sell_success} 条卖出记录 → 影子账户")
                            if dup > 0:
                                st.info(f"跳过 {dup} 条重复记录(与已有数据一致)")
                            if skipped_other > 0:
                                st.info(f"已跳过 {skipped_other} 条其他记录(红股入账等)")
                            if oversells:
                                st.warning("以下代码卖出超过本次导入的买入量, 请核对是否漏导买入:\n"
                                           + "\n".join(f"{c} 超卖 {v} 股" for c, v in oversells.items()))
                            if errors:
                                st.warning(f"以下行导入失败 ({len(errors)} 条):\n" + "\n".join(errors[:20]))
                except Exception as e:
                    st.error(f"读取文件失败: {e}")

        if not cost_data:
            st.info("还没有成本数据。在上方持仓列表中点击「初始化」开始录入。")
        else:
            st.divider()
            st.markdown("### \U0001F4CA 整体分配建议")
            st.caption("基于所有已录入标的的整体档位，给出新增资金分配方向")
            gold = []
            silver = []
            warning = []
            for code in cost_data:
                if df is not None and code in df["code"].values:
                    price = float(df[df["code"] == code]["price"].iloc[0])
                    ot = calc_overall_tier(code, price)
                    name = df[df["code"] == code]["name"].iloc[0]
                    item = f"**{name}** ({code}) — {ot['tier']} | 比值 {ot['ratio']}"
                    if ot["tier"] == "黄金":
                        gold.append(item)
                    elif ot["tier"] == "白银":
                        silver.append(item)
                    else:
                        warning.append(item)
            g, s, w = st.columns(3)
            with g:
                st.markdown("\U0001F7E1 **黄金档（可加仓）**")
                if gold:
                    for it in gold:
                        st.markdown(f"<div style='border-left:3px solid #ffd700;padding:4px 8px;margin:4px 0'>{it}</div>", unsafe_allow_html=True)
                else:
                    st.info("无")
            with s:
                st.markdown("\u26AA **白银档（持有）**")
                if silver:
                    for it in silver:
                        st.markdown(f"<div style='border-left:3px solid #ccc;padding:4px 8px;margin:4px 0'>{it}</div>", unsafe_allow_html=True)
                else:
                    st.info("无")
            with w:
                st.markdown("\U0001F534 **警戒档（暂停加仓）**")
                if warning:
                    for it in warning:
                        st.markdown(f"<div style='border-left:3px solid #ff4444;padding:4px 8px;margin:4px 0'>{it}</div>", unsafe_allow_html=True)
                else:
                    st.info("无")
            st.divider()
            st.markdown(
                "<div style='background:#1a1a2e;border:1px solid #333;border-radius:8px;padding:12px'>"
                "<b>\U0001F4A1 资金分配原则</b><br>"
                "1. 优先加仓 <b>黄金档</b> 标的<br>"
                "2. 所有标的均为 <b>白银档</b> → 持有现金等待<br>"
                "3. 出现 <b>警戒档</b> → 对该标的暂停一切加仓操作"
                "</div>", unsafe_allow_html=True
            )


def render_t9(t9):
    with t9:
        st.markdown("### \U0001F4B0 Alpha因子智能选择 \U0001F9E0")
        st.caption("基于个股时序 IC/IR 自动推荐最佳因子 | 自动扫描所有股票 | 自动计算进化轨迹")

        watch_codes = list(WATCH_LIST.keys())
        col1, col2 = st.columns([4, 1])
        with col1:
            st.markdown(f"**扫描范围**: {len(watch_codes)} 只股票")
        with col2:
            retrain = st.button("\U0001F504 重新训练全部", key="retrain_all")

        # 批量处理所有股票 → 后台线程；进度用 st.fragment 片段级自刷，绝不重建整页
        batch_key = "factor_batch_results"

        # 「重新训练全部」清空既有结果，强制重算
        if retrain and batch_key in st.session_state:
            st.session_state.pop(batch_key, None)

        # 尚无结果且后台未跑 → 只启动一次任务
        if batch_key not in st.session_state and not _FACTOR_BATCH["running"] and not _FACTOR_BATCH["done"]:
            _start_factor_batch(watch_codes, do_train=retrain)

        @st.fragment(run_every=2)
        def _factor_batch_ui():
            if batch_key in st.session_state:
                return
            if _FACTOR_BATCH["done"]:
                st.session_state[batch_key] = _FACTOR_BATCH["results"]
                st.session_state[batch_key + "_ts"] = _FACTOR_BATCH["ts"]
                st.rerun()
                return
            if _FACTOR_BATCH["running"]:
                _t = _FACTOR_BATCH["total"] or 1
                st.progress(min(_FACTOR_BATCH["i"] / _t, 1.0),
                            text=f"后台批量训练中 {_FACTOR_BATCH['i']}/{_FACTOR_BATCH['total']} ...")
                st.caption("计算在后台进行，本页可继续操作；完成后自动展示结果")

        _factor_batch_ui()

        batch_results = st.session_state.get(batch_key, {})
        if _FACTOR_BATCH["error"]:
            st.error("后台批量任务异常: " + str(_FACTOR_BATCH["error"]))

        # ── 全局汇总 ──
        st.subheader("📊 因子全景汇总")
        _stocks = {k: v for k, v in batch_results.items() if not k.startswith("_")}
        total_trained = sum(1 for v in _stocks.values() if v.get("result"))
        total_evolving = sum(1 for v in _stocks.values() if v.get("evo_summary"))
        total_errors = sum(1 for v in _stocks.values() if v.get("error"))
        g1, g2, g3, g4 = st.columns(4)
        g1.metric("已处理", f"{len(_stocks)}只")
        g2.metric("已训练", f"{total_trained}只")
        g3.metric("进化轨迹", f"{total_evolving}只")
        g4.metric("数据不足", f"{total_errors}只")

        # 全部股票因子明细表
        summary_rows = []
        for code, data in batch_results.items():
            if not isinstance(data, dict):
                continue
            if data.get("result") and data["result"].get("recommended"):
                top1 = data["result"]["recommended"][0] if data["result"]["recommended"] else {}
                evo = data.get("evo_summary") or {}
                summary_rows.append({
                    "代码": code,
                    "名称": data["name"],
                    "行业": data["result"].get("industry", "-"),
                    "置信度": data["result"].get("confidence", "-"),
                    "Top因子": top1.get("name", "-"),
                    "IR": f"{top1.get('ir', 0):.3f}",
                    "提升中": len(evo.get("improving", [])),
                    "衰退中": len(evo.get("declining", [])),
                    "最佳因子": evo.get("best_factor", "-"),
                })
        if summary_rows:
            st.dataframe(pd.DataFrame(summary_rows), width="stretch", hide_index=True)

        # ── 每只股票详情（全部展开显示） ──
        with st.expander("📋 各股因子详情（默认折叠，点击展开）", expanded=False):
            for code, data in sorted(batch_results.items(), key=lambda x: WATCH_LIST.get(x[0], {}).get("market", "Z")):
                if not isinstance(data, dict):
                    continue
                name = data["name"]

                if data.get("error"):
                    with st.expander(f"⚠️ {code} {name} - {data['error']}", expanded=False):
                        st.warning(data["error"])
                    continue

                if not data.get("result"):
                    continue

                result = data["result"]
                train_info = data["train_info"]
                evolution = data.get("evolution")
                evo_summary = data.get("evo_summary")
                bonus = data.get("bonus", 0)

                conf_color = {"high": "#00ff88", "medium": "#ffaa00", "low": "#888"}.get(result.get("confidence", "low"), "#888")
                conf_label = {"high": "高置信度", "medium": "中置信度", "low": "低置信度"}.get(result.get("confidence", "?"), "?")
                mkt_label = {"A": "A股", "HK": "港股", "US": "美股"}.get(data.get("market", "?"), "?")

                st.markdown(f"<div style='border:2px solid {conf_color};border-radius:12px;padding:16px;margin:12px 0'>", unsafe_allow_html=True)

                # 标题行
                hdr = f"## {code} {name} [{mkt_label}]"
                hdr += f" | <span style='color:{conf_color}'>{conf_label}</span>"
                if evo_summary:
                    hdr += f" | 📈{len(evo_summary.get('improving',[]))} ➡️{len(evo_summary.get('stable',[]))} 📉{len(evo_summary.get('declining',[]))}"
                st.markdown(hdr, unsafe_allow_html=True)

                # 行业/样本/奖励分
                st.markdown(f"**行业**: {result.get('industry', '-')} | **样本**: {train_info.get('n_days', 0)}天 | **因子数**: {train_info.get('n_factors', 0)} | **动态奖励**: +{bonus:.1f}")

                if result.get("recommended"):
                    # 推荐因子卡片
                    rec_cols = st.columns(3)
                    for i, rec in enumerate(result["recommended"]):
                        with rec_cols[i]:
                            ir_color = "#00ff88" if rec["ir"] > 0.5 else "#ffaa00" if rec["ir"] > 0.2 else "#888"
                            st.markdown(f"<div class=kelly-card style='border-color:{ir_color}'>", unsafe_allow_html=True)
                            st.markdown(f"**{rec['name']}** ({rec['name_cn']})")
                            st.markdown(f"<span style='color:{ir_color};font-size:1.2rem'>**IR: {rec['ir']:.3f}**</span>", unsafe_allow_html=True)
                            st.markdown(f"IC均值: {rec['ic_mean']:.4f} | IC标准差: {rec.get('ic_std', 0):.4f}")
                            st.markdown(f"IC>0.03比例: {rec['positive_ratio']:.0%} | 样本: {rec['n_samples']}天")
                            st.markdown("</div>", unsafe_allow_html=True)

                # ── 因子进化轨迹（详细） ──
                if evolution and evo_summary:
                    st.markdown("---")
                    st.markdown("### 📈 因子进化轨迹")

                    e_cols = st.columns(5)
                    with e_cols[0]:
                        st.metric("因子总数", evo_summary["total"])
                    with e_cols[1]:
                        st.metric("📈 提升中", len(evo_summary["improving"]))
                    with e_cols[2]:
                        st.metric("➡️ 稳定", len(evo_summary["stable"]))
                    with e_cols[3]:
                        st.metric("📉 衰退中", len(evo_summary["declining"]))
                    with e_cols[4]:
                        st.metric("最佳因子", evo_summary.get("best_factor", "?"))

                    # 提升因子列表
                    if evo_summary.get("improving"):
                        st.success("📈 提升中: " + ", ".join(evo_summary["improving"]))
                    if evo_summary.get("stable"):
                        st.info("➡️ 稳定: " + ", ".join(evo_summary["stable"]))
                    if evo_summary.get("declining"):
                        st.warning("📉 衰退中: " + ", ".join(evo_summary["declining"]))

                    # 进化明细完整表格
                    evo_rows = []
                    for fname, edata in sorted(evolution.items(), key=lambda x: -abs(x[1]["mean_ic"])):
                        desc = get_factor_descriptions().get(fname, {})
                        trend_map = {"improving": "📈 提升", "declining": "📉 衰退", "stable": "➡️ 稳定"}
                        # 计算最近30天IC
                        recent_30 = edata["history"][-30:] if len(edata["history"]) >= 30 else edata["history"]
                        recent_ic = sum(h["ic"] for h in recent_30) / len(recent_30) if recent_30 else 0
                        evo_rows.append({
                            "因子": fname,
                            "中文名": desc.get("name", ""),
                            "趋势": trend_map.get(edata["trend"], "?"),
                            "均值IC": f"{edata['mean_ic']:.4f}",
                            "最新IC": f"{edata['latest_ic']:.4f}",
                            "近30天IC": f"{recent_ic:.4f}",
                            "趋势强度": f"{edata['trend_strength']:+.4f}",
                            "IC轨迹图": "查看下方",
                        })
                    if evo_rows:
                        st.dataframe(pd.DataFrame(evo_rows), width="stretch", hide_index=True)

                    # 每个因子的 IC 轨迹折线图（合并为单个多线图，避免图表过多卡死浏览器）
                    st.markdown("#### 📊 各因子 IC 时间序列")
                    ic_merged = None
                    for fname, edata in sorted(evolution.items(), key=lambda x: -abs(x[1]["mean_ic"])):
                        ic_df = pd.DataFrame(edata["history"])
                        if len(ic_df) >= 3:
                            s = ic_df.copy()
                            s["date"] = pd.to_datetime(s["date"])
                            s = s.set_index("date")["ic"].rename(fname)
                            if ic_merged is None:
                                ic_merged = s.to_frame()
                            else:
                                ic_merged = ic_merged.join(s, how="outer")
                    if ic_merged is not None:
                        st.line_chart(ic_merged, use_container_width=True, height=220)

                # ── 全部因子 IC/IR 明细 ──
                if result.get("all_results"):
                    st.markdown("---")
                    st.markdown("### 📋 全部因子 IC/IR 明细")
                    ic_data = []
                    for fname, fdata in sorted(result["all_results"].items(), key=lambda x: -abs(x[1].get("ir", 0))):
                        desc = get_factor_descriptions().get(fname, {})
                        ir_val = fdata.get("ir", 0)
                        ir_color = "🟢" if ir_val > 0.5 else "🟡" if ir_val > 0.2 else "🔴"
                        ic_data.append({
                            "因子": fname,
                            "名称": desc.get("name", ""),
                            "IC均值": f"{fdata['ic_mean']:.4f}",
                            "IC标准差": f"{fdata['ic_std']:.4f}",
                            "IR": f"{ir_val:.3f}",
                            "IC>0.03比例": f"{fdata['positive_ratio']:.1%}",
                            "样本数": fdata.get("n_samples", 0),
                        })
                    if ic_data:
                        st.dataframe(pd.DataFrame(ic_data), width="stretch", hide_index=True)

                # ── 因子值时序图（全部因子） ──
                if data.get("hist_df") is not None:
                    st.markdown("---")
                    st.markdown("### 📈 因子值时序图")
                    try:
                        factor_vals = calculate_factors_for_stock(data["hist_df"])
                        if factor_vals:
                            fv_df = pd.DataFrame(factor_vals)
                            fv_df["date"] = data["hist_df"]["date"].values[:len(fv_df)]
                            fv_df = fv_df.set_index("date")
                            # 只显示Top因子的值（合并为单个多线图）
                            top3_names = [r["name"] for r in result.get("recommended", [])]
                            top3_names = [fn for fn in top3_names if fn in fv_df.columns]
                            if top3_names:
                                st.markdown(f"**Top因子值时序**: {', '.join(top3_names)}")
                                st.line_chart(fv_df[top3_names], use_container_width=True, height=180)
                    except Exception:
                        st.caption("因子值计算失败")

                st.markdown("</div>", unsafe_allow_html=True)

                # 存入 session_state 供其他Tab使用
                st.session_state["frec_" + code] = result.get("recommended", [])
                st.session_state["fbonus_" + code] = bonus

            st.divider()
            st.markdown("### \U0001F4CA 因子模型概览")
            st.caption("已训练的个股因子模型 (data/factor_models/)")
            import glob, json, os
            model_dir = os.path.join(os.path.dirname(__file__), "data", "factor_models")
            model_files = sorted(glob.glob(os.path.join(model_dir, "*.json")))
            if model_files and any(os.path.basename(f) not in ("_placeholder_") for f in model_files):
                model_data = []
                for mf in model_files:
                    bname = os.path.basename(mf)
                    if "_history" in bname or "fbonus" in bname or bname == "_placeholder_":
                        continue
                    try:
                        with open(mf, "r", encoding="utf-8") as fh:
                            m = json.load(fh)
                        code = m.get("code", bname.replace(".json", ""))
                        factors = m.get("factors", {})
                        if factors:
                            sf = sorted(factors.items(), key=lambda x: -abs(x[1].get("ir", 0)))
                            top3 = ", ".join([f"{n}({d.get('ir', 0):.2f})" for n, d in sf[:3]])
                        else:
                            top3 = "-"
                        model_data.append({
                            "股票": f"{code} {WATCH_LIST.get(code, {}).get('name', '')}",
                            "训练日期": m.get("trained_at", "")[:10],
                            "样本": f"{m.get('n_days', 0)}天",
                            "因子": f"{len(factors)}个",
                            "Top-3 IR": top3,
                        })
                    except Exception:
                        pass
                if model_data:
                    st.dataframe(pd.DataFrame(model_data), width="stretch", hide_index=True)
                else:
                    st.caption("无已训练模型")
            else:
                st.caption("无已训练模型")

            st.divider()
            st.markdown("### 🔧 因子类型浏览")
            st.caption("以下为因子类型说明（自动全选）")
            old_types = get_factor_types()
            selected_types = st.multiselect("选择因子类型", old_types, default=old_types, key="factor_types_all")
            factor_names = {}
            for t in selected_types:
                factor_names.update(filter_factors_by_type(t))
            if factor_names:
                cols = st.columns(2)
                for idx, (name, desc) in enumerate(factor_names.items()):
                    with cols[idx % 2]:
                        st.markdown(f"<div class=kelly-card><b>{name}</b>: {desc['name']}<br><span style='color:#888'>{desc['description']}</span></div>", unsafe_allow_html=True)

            st.divider()
            st.markdown("### 📈 因子进化仪表盘（全股自动）")
            st.caption("一键计算所有股票的因子进化轨迹 | 自动判断提升/稳定/衰退")

            evo_batch_key = "factor_evolution_batch"
            need_evo_batch = evo_batch_key not in st.session_state or retrain

            if need_evo_batch:
                learner = get_learner()
                evo_batch = {}
                evo_progress = st.progress(0, text="准备计算进化轨迹...")
                for idx, code in enumerate(watch_codes):
                    name = WATCH_LIST[code]["name"]
                    evo_progress.progress(idx / len(watch_codes), text=f"计算中 ({idx+1}/{len(watch_codes)}): {code} {name}")
                    try:
                        # 优先使用已缓存的 hist_df
                        cached_hist = st.session_state.get("factor_data_" + code, {})
                        hist_df = cached_hist.get("hist_df") if isinstance(cached_hist, dict) else None
                        if hist_df is None or hist_df.empty:
                            hist_df = _cached_fetch_factor_history(code, WATCH_LIST[code]["market"])

                        if hist_df is not None and not hist_df.empty:
                            evolution = learner.compute_evolution(code, hist_df)
                            summary = learner.get_evolution_summary(evolution) if evolution else None
                            evo_batch[code] = {"name": name, "market": WATCH_LIST[code]["market"], "evolution": evolution, "summary": summary}
                        else:
                            evo_batch[code] = {"name": name, "error": "无历史数据"}
                    except Exception as e:
                        evo_batch[code] = {"name": name, "error": str(e)}

                st.session_state[evo_batch_key] = evo_batch
                evo_progress.progress(1.0, text="进化轨迹计算完成!")
                st.success(f"已计算 {len(evo_batch)} 只股票的因子进化轨迹")

            evo_batch = st.session_state.get(evo_batch_key, {})

            # 汇总统计
            total_improving = 0
            total_declining = 0
            total_stable = 0
            best_factors = {}
            for code, data in evo_batch.items():
                if data.get("summary"):
                    total_improving += len(data["summary"].get("improving", []))
                    total_declining += len(data["summary"].get("declining", []))
                    total_stable += len(data["summary"].get("stable", []))
                    bf = data["summary"].get("best_factor")
                    if bf:
                        best_factors[bf] = best_factors.get(bf, 0) + 1

            st.markdown("#### 📊 全局统计")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("提升中因子", total_improving)
            c2.metric("稳定因子", total_stable)
            c3.metric("衰退中因子", total_declining)
            top_bf = max(best_factors, key=best_factors.get) if best_factors else "-"
            c4.metric("出现最多的最佳因子", top_bf)

            # 全局汇总表
            evo_summary_rows = []
            for code, data in evo_batch.items():
                if data.get("summary"):
                    s = data["summary"]
                    evo_summary_rows.append({
                        "代码": code,
                        "名称": data["name"],
                        "提升": len(s.get("improving", [])),
                        "稳定": len(s.get("stable", [])),
                        "衰退": len(s.get("declining", [])),
                        "提升因子": ", ".join(s.get("improving", [])),
                        "衰退因子": ", ".join(s.get("declining", [])),
                        "最佳因子": s.get("best_factor", "-"),
                    })
            if evo_summary_rows:
                st.dataframe(pd.DataFrame(evo_summary_rows), width="stretch", hide_index=True)

            # 每只股票的进化详情
            st.markdown("#### 📋 各股因子进化详情（全部展开）")
            for code, data in sorted(evo_batch.items(), key=lambda x: WATCH_LIST.get(x[0], {}).get("market", "Z")):
                name = data["name"]

                if data.get("error"):
                    continue
                if not data.get("summary"):
                    continue

                summary = data["summary"]
                evolution = data["evolution"]
                mkt_label = {"A": "A股", "HK": "港股", "US": "美股"}.get(data.get("market", "?"), "?")

                st.markdown(f"<div style='border:1px solid #444;border-radius:10px;padding:12px;margin:10px 0'>", unsafe_allow_html=True)
                st.markdown(f"**{code} {name} [{mkt_label}]** | 📈{len(summary.get('improving',[]))} ➡️{len(summary.get('stable',[]))} 📉{len(summary.get('declining',[]))} | 最佳: {summary.get('best_factor', '-')}")

                if summary.get("improving"):
                    st.success("📈 提升中: " + ", ".join(summary["improving"]))
                if summary.get("stable"):
                    st.info("➡️ 稳定: " + ", ".join(summary["stable"]))
                if summary.get("declining"):
                    st.warning("📉 衰退中: " + ", ".join(summary["declining"]))

                # 进化明细表
                if evolution:
                    evo_rows = []
                    for fname, edata in sorted(evolution.items(), key=lambda x: -abs(x[1]["mean_ic"])):
                        desc = get_factor_descriptions().get(fname, {})
                        trend_map = {"improving": "📈 提升", "declining": "📉 衰退", "stable": "➡️ 稳定"}
                        recent_30 = edata["history"][-30:] if len(edata["history"]) >= 30 else edata["history"]
                        recent_ic = sum(h["ic"] for h in recent_30) / len(recent_30) if recent_30 else 0
                        evo_rows.append({
                            "因子": fname, "中文名": desc.get("name", ""),
                            "趋势": trend_map.get(edata["trend"], "?"),
                            "均值IC": f"{edata['mean_ic']:.4f}",
                            "最新IC": f"{edata['latest_ic']:.4f}",
                            "近30天IC": f"{recent_ic:.4f}",
                            "趋势强度": f"{edata['trend_strength']:+.4f}",
                        })
                    if evo_rows:
                        st.dataframe(pd.DataFrame(evo_rows), width="stretch", hide_index=True)

                        # IC轨迹图（合并为单个多线图）
                        ic_merged = None
                        for fname, edata in sorted(evolution.items(), key=lambda x: -abs(x[1]["mean_ic"])):
                            ic_df = pd.DataFrame(edata["history"])
                            if len(ic_df) >= 3:
                                s = ic_df.copy()
                                s["date"] = pd.to_datetime(s["date"])
                                s = s.set_index("date")["ic"].rename(fname)
                                if ic_merged is None:
                                    ic_merged = s.to_frame()
                                else:
                                    ic_merged = ic_merged.join(s, how="outer")
                        if ic_merged is not None:
                            st.line_chart(ic_merged, use_container_width=True, height=200)

                st.markdown("</div>", unsafe_allow_html=True)


def render_t10(t10):
    with t10:
        st.markdown("### 🧠 Multi-Agent 共识决策看板")
        st.caption("每只股票的所有模块意见汇总")

        def _sc_score(v, tg, ty):
            if pd.notna(v) and v >= tg: return "#00ff88"
            if pd.notna(v) and v >= ty: return "#ffaa00"
            return "#ff4444"

        def _sc_us_score(v):
            if pd.notna(v) and v >= 70: return "#00ff88"
            if pd.notna(v) and v >= 50: return "#ffaa00"
            return "#ff4444"

        if df is not None and not df.empty:
            codes_consensus = df[df["group"].isin(["A股持仓","港股持仓","美股"])]["code"].tolist()
            if codes_consensus:
                # 因子IR：优先使用Alpha因子batch结果，其次用learner模型
                factor_cache = {}
                batch_res = st.session_state.get("factor_batch_results", {})
                if batch_res:
                    for _c, _d in batch_res.items():
                        if _c.startswith("_") or not isinstance(_d, dict): continue
                        if _d.get("result") and _d["result"].get("recommended"):
                            irs = [r["ir"] for r in _d["result"]["recommended"] if isinstance(r.get("ir"), (int, float))]
                            factor_cache[_c] = sum(irs)/len(irs) if irs else 0.0
                        elif _d.get("evolution"):
                            evo = _d.get("evolution", {})
                            irs = [ed.get("mean_ic", 0) for ed in evo.values() if isinstance(ed, dict)]
                            factor_cache[_c] = sum(irs)/len(irs) if irs else 0.0

                # 备用：从learner模型加载
                from modules.learner import get_learner
                _l_ = get_learner()
                for _c_ in codes_consensus:
                    if _c_ not in factor_cache:
                        _m_ = _l_.load(_c_)
                        if _m_ and _m_.get("factors"):
                            irs = [f["ir"] for f in _m_["factors"].values() if isinstance(f.get("ir"), (int, float))]
                            factor_cache[_c_] = sum(irs)/len(irs) if irs else 0.0

                for _, _rr in df[df["code"].isin(codes_consensus)].iterrows():
                    _c = _rr["code"]
                    _nm = _rr["name"]
                    _ls = _rr.get("lishing", 0)
                    _dy = _rr.get("dy", 0)
                    _kf = _rr.get("kelly_final", 0)
                    _pe = _rr.get("pe", None)
                    _pb = _rr.get("pb", None)
                    _chg = _rr.get("chg", 0)
                    _ratio = _rr.get("ratio", 0)
                    _fir = factor_cache.get(_c, 0)

                    # 美股特殊处理
                    is_us = "美股" in _rr.get("group", "")
                    if is_us:
                        _ls_c = _sc_us_score(_ls)
                    else:
                        _ls_c = _sc_score(_ls, 70, 60)

                    kf_c = _sc_score(_kf, 10, 5)
                    ir_c = _sc_score(_fir, 0.03, 0.01)
                    chg_c = "#00ff88" if _chg >= 0 else "#ff4444"

                    card = f"<div style='border:1px solid #555;border-radius:10px;padding:12px;margin:8px 0'>"
                    card += f"<h4 style='margin:0 0 8px 0'>{_nm} <span style='font-size:0.8em;color:#888'>({_c})</span></h4>"
                    card += f"<div style='display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:8px'>"
                    card += f"<div style='border:1px solid {_ls_c};border-radius:6px;padding:6px;text-align:center'>"
                    card += f"<div style='font-size:0.7em;color:#888'>🎯 {'成长评分' if is_us else '李氏评分'}</div>"
                    card += f"<div style='color:{_ls_c};font-size:1.2em'>{_ls if pd.notna(_ls) else '-'}</div></div>"
                    card += f"<div style='border:1px solid {kf_c};border-radius:6px;padding:6px;text-align:center'>"
                    card += f"<div style='font-size:0.7em;color:#888'>💰 Kelly仓位</div>"
                    card += f"<div style='color:{kf_c};font-size:1.2em'>{_kf:.0f}%</div></div>"
                    card += f"<div style='border:1px solid {ir_c};border-radius:6px;padding:6px;text-align:center'>"
                    card += f"<div style='font-size:0.7em;color:#888'>📊 因子IR</div>"
                    card += f"<div style='color:{ir_c};font-size:1.2em'>{_fir:.3f}</div></div>"
                    card += f"<div style='border:1px solid {chg_c};border-radius:6px;padding:6px;text-align:center'>"
                    card += f"<div style='font-size:0.7em;color:#888'>📈 涨跌</div>"
                    card += f"<div style='color:{chg_c};font-size:1.2em'>{_chg:+.1f}%</div></div></div>"
                    card += f"<div style='margin-top:8px;font-size:0.85em;color:#aaa'>"
                    card += f"PE={_pe if pd.notna(_pe) else '-'} | PB={_pb if pd.notna(_pb) else '-'} | 股息率={_dy:.2f}% | 当前占比={_ratio:.0f}%</div></div>"
                    st.markdown(card, unsafe_allow_html=True)
            else:
                st.info("暂无持仓数据")
        else:
            st.info("请先刷新持仓数据")


def render_t11(t11):
    with t11:
        st.markdown("### 📈 持仓历史对比")
        st.caption("自动保存持仓快照，支持多时点对比分析")

        from modules.exporter import save_snapshot, load_snapshots, SNAPSHOT_DIR

        # 自动保存快照
        if df is not None and not df.empty:
            last_snapshot = st.session_state.get("_last_snapshot_time")
            now = datetime.datetime.now()
            if last_snapshot is None or (now - last_snapshot).total_seconds() > 300:  # 5分钟
                try:
                    save_snapshot(df)
                    st.session_state["_last_snapshot_time"] = now
                except Exception:
                    pass

        # 控件
        c1, c2, c3 = st.columns([2, 2, 1])
        with c1:
            days_range = st.slider("查看天数", 1, 30, 7, key="hist_days")
        with c2:
            metric = st.selectbox("对比指标", ["price", "dy_lfy", "kelly_final", "lishing"], 
                                  format_func=lambda x: {"price": "现价", "dy_lfy": "股息率", 
                                                          "kelly_final": "Kelly仓位", "lishing": "李氏评分"}[x],
                                  key="hist_metric")
        with c3:
            snap_count = len(list(SNAPSHOT_DIR.glob("snapshot_*.csv"))) if SNAPSHOT_DIR.exists() else 0
            st.metric("快照数", snap_count)

        # 加载快照
        snapshots = _cached_load_snapshots(days_range)

        if len(snapshots) >= 2:
            # 多时点对比
            st.subheader(f"最近 {days_range} 天快照趋势")

            # 合并数据
            merged = None
            for ts, snap_df in snapshots:
                cols_to_keep = ["code", "name", "group", metric]
                cols_exist = [c for c in cols_to_keep if c in snap_df.columns]
                snap_sub = snap_df[cols_exist].copy()
                snap_sub["timestamp"] = ts
                if merged is None:
                    merged = snap_sub
                else:
                    merged = pd.concat([merged, snap_sub], ignore_index=True)

            if merged is not None and not merged.empty:
                # 每只股票的指标变化
                codes = df["code"].tolist() if df is not None else []
                names = dict(zip(df["code"], df["name"])) if df is not None else {}

                # 使用Streamlit内置line_chart
                chart_data = None
                for code in codes:
                    stock_data = merged[merged["code"] == code].sort_values("timestamp")
                    if len(stock_data) < 2:
                        continue
                    name = names.get(code, code)
                    series = stock_data.set_index("timestamp")[metric].rename(name)
                    if chart_data is None:
                        chart_data = series.to_frame()
                    else:
                        chart_data = chart_data.join(series, how="outer")

                if chart_data is not None:
                    metric_labels = {"price": "现价", "dy_lfy": "股息率(%)", "kelly_final": "Kelly仓位(%)", "lishing": "李氏评分"}
                    st.line_chart(chart_data, height=400)

                # 变化统计
                st.subheader("📊 变化统计")
                if df is not None and not df.empty:
                    changes = []
                    for code in codes:
                        stock_data = merged[merged["code"] == code].sort_values("timestamp")
                        if len(stock_data) >= 2:
                            first_val = stock_data[metric].iloc[0]
                            last_val = stock_data[metric].iloc[-1]
                            pct_change = ((last_val - first_val) / abs(first_val) * 100) if first_val != 0 else 0
                            changes.append({
                                "代码": code,
                                "名称": names.get(code, code),
                                "起始值": round(first_val, 2),
                                "当前值": round(last_val, 2),
                                "变化%": round(pct_change, 2),
                                "趋势": "📈" if pct_change > 0 else ("📉" if pct_change < 0 else "➡️")
                            })
                    if changes:
                        changes_df = pd.DataFrame(changes).sort_values("变化%", ascending=False)
                        st.dataframe(changes_df, use_container_width=True, hide_index=True)
        elif len(snapshots) == 1:
            st.info(f"仅有 1 个快照，需要至少 2 个快照才能进行对比。继续使用系统会自动保存快照。")
        else:
            st.info("暂无快照数据。系统会在每次刷新持仓时自动保存快照。")

    # ── Tab 12: 影子账户 ──

def render_t12(t12):
    with t12:
        from modules.shadow_account import (
            get_all_trades, generate_summary, add_sell, delete_sell, get_sells
        )

        st.markdown("### \U0001F50D 影子账户 — 交易行为分析")
        st.caption("基于成本分层买入记录 + 卖出记录，分析你的交易行为特征")

        df_trades = get_all_trades()

        if df_trades.empty:
            st.warning("暂无交易记录。请先在「成本分层」Tab 录入建仓/加仓数据。")
        else:
            summary = generate_summary(df_trades, df)

            if summary is None:
                st.warning("分析失败，请检查交易记录是否完整。")
            else:
                # ── 概览仪表盘 ──
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("覆盖股票", f"{summary['total_stocks']}只")
                c2.metric("交易批次", f"{summary['total_batches']}笔")
                c3.metric("平均持仓", f"{summary['avg_holding_days']:.0f}天")
                c4.metric("入场胜率", f"{summary['overall_win_rate']:.1f}%")
                if summary.get("max_drawdown") is not None:
                    c5.metric("最大回撤", f"{summary['max_drawdown']:.2f}%",
                              help=f"净值峰值→谷底最大跌幅 (基于{summary['dd_covered']}只股票历史行情)")
                else:
                    c5.metric("最大回撤", "—")

                nav_df = summary.get("nav_df")
                if nav_df is not None and not nav_df.empty:
                    st.caption(f"影子账户净值曲线（首日=1.0，含价格波动，峰值 {summary.get('dd_peak_date')} → 谷底 {summary.get('dd_trough_date')}）")
                    st.line_chart(nav_df, height=180)

                st.markdown("---")

                # ── 1. 持仓天数分析 ──
                st.subheader("1\ufe0f\u20e3 持仓天数分析")
                holding_df = summary["holding_detail"]
                if not holding_df.empty:
                    st.dataframe(holding_df, use_container_width=True, hide_index=True)
                    avg_days = holding_df["avg_holding_days"].mean()
                    if avg_days > 365:
                        st.success(f"\u2705 平均持仓 {avg_days:.0f} 天（{avg_days/365:.1f}年），符合长期投资风格")
                    elif avg_days > 90:
                        st.info(f"\u2139\ufe0f 平均持仓 {avg_days:.0f} 天，中期持有")
                    else:
                        st.warning(f"\u26a0\ufe0f 平均持仓 {avg_days:.0f} 天偏短，注意频繁交易成本")

                # ── 2. 入场时机分析 ──
                st.subheader("2\ufe0f\u20e3 入场时机分析（买入批次浮盈/浮亏）")
                entry_list = summary["entry_detail"]
                if entry_list:
                    entry_rows = []
                    for e in entry_list:
                        row = {
                            "代码": e["code"], "名称": e["name"],
                            "批次数": e["batches"], "盈利批次": e["win_batches"],
                            "胜率": f"{e['win_rate']:.1f}%", "平均盈亏": f"{e['avg_pnl_pct']:.2f}%",
                        }
                        if e.get("best_batch"):
                            row["最佳批次"] = f"{e['best_batch']['date']} +{e['best_batch']['pnl_pct']:.1f}%"
                        if e.get("worst_batch"):
                            row["最差批次"] = f"{e['worst_batch']['date']} {e['worst_batch']['pnl_pct']:.1f}%"
                        entry_rows.append(row)
                    st.dataframe(pd.DataFrame(entry_rows), use_container_width=True, hide_index=True)

                # ── 3. 加仓行为分析 ──
                st.subheader("3\ufe0f\u20e3 加仓行为分析")
                add_list = summary["add_detail"]
                if add_list:
                    add_rows = []
                    for a in add_list:
                        color = "\U0001f7e2" if a["behavior"] == "\u9020\u4f4e\u52a0\u4ed3" else "\U0001f534" if a["behavior"] == "\u8ffd\u6da8\u52a0\u4ed3" else "\u26aa"
                        add_rows.append({
                            "代码": a["code"], "名称": a["name"],
                            "首次建仓": f"{a['first_buy_date']} @ {a['first_price']:.2f}",
                            "加仓次数": a["add_count"],
                            "逢低加仓": a["low_adds"], "追涨加仓": a["high_adds"],
                            "平均加仓价": a["avg_add_price"],
                            "行为": f"{color} {a['behavior']}",
                        })
                    st.dataframe(pd.DataFrame(add_rows), use_container_width=True, hide_index=True)
                    low_pct = summary["low_add_stocks"] / max(len(add_list), 1) * 100
                    if low_pct > 60:
                        st.success(f"\u2705 {low_pct:.0f}% 的股票采用逢低加仓策略，成本控制良好")
                    else:
                        st.warning(f"\u26a0\ufe0f 仅有 {low_pct:.0f}% 的股票逢低加仓，注意追涨风险")
                else:
                    st.info("暂无多次加仓记录（每只股票仅1个批次），无法分析加仓行为")

                # ── 4. 处置效应检测 ──
                st.subheader("4\ufe0f\u20e3 处置效应检测")
                disp_list = summary["disposition_detail"]
                has_sells = summary["has_sell_records"]
                if disp_list:
                    if has_sells:
                        st.caption("基于实际卖出记录分析")
                        disp_rows = []
                        for d in disp_list:
                            disp_rows.append({
                                "代码": d["code"], "名称": d["name"],
                                "盈利卖出持有天数": d["avg_win_holding_days"],
                                "亏损卖出持有天数": d["avg_loss_holding_days"],
                                "盈利卖出次数": d["win_sells"], "亏损卖出次数": d["loss_sells"],
                                "诊断": d["effect"],
                            })
                        st.dataframe(pd.DataFrame(disp_rows), use_container_width=True, hide_index=True)
                        has_effect = any(d["effect"] == "\u6709\u5904\u7f6e\u6548\u5e94" for d in disp_list)
                        if has_effect:
                            st.error("\u26a0\ufe0f 检测到处置效应：盈利仓卖出更快、亏损仓持有更久。这是常见的行为偏差，建议设置止盈/止损纪律。")
                    else:
                        st.caption("\u6682\u65e0\u5356\u51fa\u8bb0\u5f55\uff0c\u57fa\u4e8e\u672a\u5e73\u4ed3\u6279\u6b21\u7684\u6d6e\u76c8/\u6d6e\u4e8f\u5206\u6790")
                        disp_rows = []
                        for d in disp_list:
                            disp_rows.append({
                                "代码": d["code"], "名称": d["name"],
                                "浮盈批次持有天数": d["avg_win_holding_days"],
                                "浮亏批次持有天数": d["avg_loss_holding_days"],
                                "浮盈批次": d["win_batches"], "浮亏批次": d["loss_batches"],
                                "诊断": d["effect"],
                            })
                        st.dataframe(pd.DataFrame(disp_rows), use_container_width=True, hide_index=True)
                else:
                    st.info("数据不足，无法分析处置效应")

                # ── 5. 追涨杀跌检测 ──
                st.subheader("5\ufe0f\u20e3 追涨杀跌检测")
                chase_list = summary["chasing_detail"]
                if chase_list:
                    chase_rows = []
                    for c in chase_list:
                        color = "\U0001f534" if "\u8ffd\u6da8" in c["behavior"] else "\U0001f7e2" if "\u6284\u5e95" in c["behavior"] else "\u26aa"
                        chase_rows.append({
                            "代码": c["code"], "名称": c["name"],
                            "首次买入价": c["first_price"], "最近买入价": c["last_price"],
                            "最低买入价": c["min_price"], "最高买入价": c["max_price"],
                            "最近价格分位": f"{c['last_price_pctl']:.0f}%",
                            "诊断": f"{color} {c['behavior']}",
                        })
                    st.dataframe(pd.DataFrame(chase_rows), use_container_width=True, hide_index=True)
                    chase_pct = summary["chase_entry_stocks"] / max(len(chase_list), 1) * 100
                    if chase_pct > 40:
                        st.warning(f"\u26a0\ufe0f {chase_pct:.0f}% 的股票最近一次买入在价格高位，注意追涨风险")

                # ── 卖出记录管理 ──
                st.markdown("---")
                st.subheader("\U0001F4DD 卖出记录管理")
                st.caption("\u5f55\u5165\u5356\u51fa\u8bb0\u5f55\u540e\uff0c\u7cfb\u7edf\u53ef\u4ee5\u8fdb\u884c\u66f4\u7cbe\u51c6\u7684\u5904\u7f6e\u6548\u5e94\u5206\u6790\u548c\u5b9e\u9645\u80dc\u7387\u8ba1\u7b97")

                with st.expander("\u5f55\u5165\u5356\u51fa\u8bb0\u5f55", expanded=False):
                    if df is not None and not df.empty:
                        sell_codes = df["code"].tolist()
                        sell_names = df.set_index("code")["name"].to_dict()
                        sel_code = st.selectbox("\u9009\u62e9\u80a1\u7968", sell_codes,
                                               format_func=lambda c: f"{c} {sell_names.get(c, '')}",
                                               key="shadow_sell_code")
                        cur_price = df[df["code"] == sel_code]["price"].iloc[0] if not df[df["code"] == sel_code].empty else 0

                        sc1, sc2, sc3 = st.columns(3)
                        with sc1:
                            sell_date = st.date_input("\u5356\u51fa\u65e5\u671f", key="shadow_sell_date")
                        with sc2:
                            sell_shares = st.number_input("\u80a1\u6570", min_value=1, value=100, step=100, key="shadow_sell_shares")
                        with sc3:
                            sell_price = st.number_input("\u5356\u51fa\u4ef7\u683c", value=float(cur_price) if cur_price > 0 else 0.0, step=0.01, key="shadow_sell_price")

                        sell_note = st.text_input("\u5907\u6ce8\uff08\u5982\u6b62\u635f/\u6b62\u76c8/\u8c03\u4ed3\uff09", key="shadow_sell_note")

                        if st.button("\u786e\u8ba4\u5f55\u5165", key="shadow_sell_btn"):
                            add_sell(sel_code, sell_date.strftime("%Y-%m-%d"), int(sell_shares), float(sell_price), sell_note)
                            st.success(f"\u5df2\u5f55\u5165\uff1a{sel_code} {sell_names.get(sel_code, '')} \u5356\u51fa {int(sell_shares)} \u80a1 @ {float(sell_price):.2f}")
                            st.rerun()
                    else:
                        st.info("\u8bf7\u5148\u5237\u65b0\u6301\u4ed3\u6570\u636e")

                # 显示已有卖出记录
                existing_sells = []
                for code in (df["code"].tolist() if df is not None and not df.empty else []):
                    sells = get_sells(code)
                    name = df[df["code"] == code]["name"].iloc[0] if not df[df["code"] == code].empty else code
                    for i, s in enumerate(sells):
                        existing_sells.append({
                            "代码": code, "名称": name,
                            "日期": s["date"], "股数": s["shares"],
                            "价格": s["price"], "备注": s.get("note", ""),
                            "_index": i, "_code": code,
                        })
                if existing_sells:
                    st.caption(f"\u5171 {len(existing_sells)} \u6761\u5356\u51fa\u8bb0\u5f55")
                    for es in existing_sells:
                        cols = st.columns([3, 2, 2, 2, 2, 1])
                        cols[0].write(f"{es['代码']} {es['名称']}")
                        cols[1].write(es["日期"])
                        cols[2].write(f"{es['股数']}股")
                        cols[3].write(f"{es['价格']:.2f}")
                        cols[4].write(es["备注"])
                        if cols[5].button("\u5220", key=f"del_sell_{es['_code']}_{es['_index']}"):
                            delete_sell(es["_code"], es["_index"])
                            st.rerun()

    # ── Tab 13: 模拟盘验证 ──

def render_t13(t13):
    with t13:
        from modules.paper_trader import (
            run_full_paper_cycle, get_paper_status, reset_paper,
            capture_daily_snapshot, validate_signals, update_paper_portfolio,
            check_and_run_daily, get_schedule_status, set_schedule_config,
            VIRTUAL_CAPITAL
        )

        st.markdown("### \U0001F9EA 模拟盘验证系统")
        st.caption("用东方财富真实数据驱动信号快照 + 虚拟组合执行，验证系统买卖信号的真实性和可靠性")

        st.markdown("""
        <div style='border:1px solid #444;border-radius:8px;padding:12px;margin:8px 0'>
        <b>🎯 验证原理</b><br>
        1. <b>每日快照</b>: 对所有持仓生成信号评分 (买入/卖出/持有)<br>
        2. <b>虚拟执行</b>: 按 Kelly 建议 + 信号强弱模拟下单<br>
        3. <b>业绩追踪</b>: 维护虚拟组合净值曲线, 计算夏普/回撤<br>
        4. <b>信号验证</b>: 回溯检查每条信号的准确性 (买后涨了吗? 卖后跌了吗?)<br>
        <span style='color:#00ff88'>✅ 所有数据来自东方财富 (AKShare)，is_mock=False 标记真实数据</span>
        </div>
        """, unsafe_allow_html=True)

        col_a, col_b, col_c = st.columns([2, 1, 1])
        with col_a:
            if st.button("\U0001F680 运行完整验证周期", type="primary", key="paper_run"):
                with st.spinner("运行模拟盘验证 (约60秒)..."):
                    status = run_full_paper_cycle()
                st.success("✅ 验证周期完成!")
                st.rerun()
        with col_b:
            if st.button("\U0001F4CA 仅生成今日信号快照", key="paper_snapshot"):
                with st.spinner("生成信号快照..."):
                    snaps = capture_daily_snapshot()
                st.success(f"✅ 快照已生成: {len(snaps)} 只标的")
                st.rerun()
        with col_c:
            if st.button("\U0001F504 重置模拟盘", key="paper_reset"):
                reset_paper()
                st.warning("⚠️ 模拟盘已重置")
                st.rerun()

        status = get_paper_status()
        pf = status.get("portfolio", {})
        nav = status.get("nav", {})
        validation = status.get("validation", {})
        positions = status.get("positions", [])
        today_snap = status.get("today_snapshot", [])
        pending_orders = status.get("pending_orders", [])
        skipped_orders = status.get("skipped_orders", [])

        # ── 虚拟组合概览 ──
        st.subheader("📊 虚拟组合概览")
        tc = pf.get("total_value", VIRTUAL_CAPITAL)
        cash = pf.get("cash", VIRTUAL_CAPITAL)
        pv = pf.get("positions_value", 0)
        n_positions = sum(1 for p in positions if p.get("shares", 0) > 0)
        pnl = tc - VIRTUAL_CAPITAL
        pnl_pct = pnl / VIRTUAL_CAPITAL * 100

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("虚拟总资产", f"¥{tc:,.0f}", f"{pnl_pct:+.2f}%")
        c2.metric("现金", f"¥{cash:,.0f}")
        c3.metric("持仓市值", f"¥{pv:,.0f}")
        c4.metric("持仓数", f"{n_positions}只")
        max_dd = nav.get("max_drawdown")
        if max_dd is not None:
            c5.metric("最大回撤", f"{max_dd:.2f}%")
        else:
            c5.metric("最大回撤", "—")

        # 净值曲线
        if nav.get("dates") and len(nav["dates"]) >= 2:
            st.markdown("#### 📈 虚拟组合净值曲线")
            nav_df = pd.DataFrame({
                "date": pd.to_datetime(nav["dates"]),
                "nav": nav["navs"],
                "value": nav["values"],
            })
            nav_df = nav_df.set_index("date")
            st.line_chart(nav_df["nav"], height=250)

            if nav.get("sharpe") is not None:
                sc1, sc2, sc3 = st.columns(3)
                sc1.metric("年化收益", f"{nav.get('annual_return', 0)*100:.2f}%")
                sc2.metric("年化波动", f"{nav.get('annual_vol', 0)*100:.2f}%")
                sc3.metric("夏普比率", f"{nav.get('sharpe', 0):.2f}")

        # ── 持仓明细 ──
        if positions:
            st.subheader("📋 虚拟持仓明细")
            pos_df = pd.DataFrame(positions)
            if not pos_df.empty:
                pos_df["盈亏%"] = pos_df["pnl_pct"].apply(lambda x: f"{x:+.2f}%")
                pos_df["市值"] = pos_df["market_value"].apply(lambda x: f"¥{x:,.0f}")
                pos_df["浮动盈亏"] = pos_df["pnl"].apply(lambda x: f"¥{x:+,.0f}")
                pos_df["成本价"] = pos_df["avg_cost"].apply(lambda x: f"{x:.2f}")
                pos_df["现价"] = pos_df["price"].apply(lambda x: f"{x:.2f}")
                display_cols = ["code", "name", "shares", "成本价", "现价", "市值", "浮动盈亏", "盈亏%"]
                display_cols = [c for c in display_cols if c in pos_df.columns or c in ["成本价", "现价", "市值", "浮动盈亏", "盈亏%"]]
                rename_map = {"code": "代码", "name": "名称", "shares": "股数"}
                pos_display = pos_df[[c for c in ["code", "name", "shares"] if c in pos_df.columns]].copy()
                for col in ["成本价", "现价", "市值", "浮动盈亏", "盈亏%"]:
                    if col in pos_df.columns:
                        pos_display[col] = pos_df[col]
                st.dataframe(pos_display, width="stretch", hide_index=True)
        else:
            st.info("暂无虚拟持仓。点击「运行完整验证周期」开始模拟交易。")

        # ── 挂单与未成交 ──
        if pending_orders or skipped_orders:
            st.markdown("---")
            st.subheader("⏳ 挂单与未成交")
            if pending_orders:
                pending_rows = []
                for o in pending_orders:
                    pending_rows.append({
                        "信号日": o.get("signal_date", "-"),
                        "代码": o.get("code", "-"),
                        "方向": "🟢买入" if o.get("action") == "buy" else "🔴卖出",
                        "股数": o.get("shares", 0),
                        "原因": o.get("reason", "-"),
                    })
                st.dataframe(pd.DataFrame(pending_rows), width="stretch", hide_index=True)
            if skipped_orders:
                skipped_rows = []
                for o in skipped_orders[-50:][::-1]:
                    skipped_rows.append({
                        "信号日": o.get("signal_date", "-"),
                        "执行日": o.get("execution_date", "-"),
                        "代码": o.get("code", "-"),
                        "方向": "🟢买入" if o.get("action") == "buy" else "🔴卖出",
                        "股数": o.get("shares", 0),
                        "未成交原因": o.get("reason", "-"),
                    })
                st.dataframe(pd.DataFrame(skipped_rows), width="stretch", hide_index=True)

        # ── 今日信号快照 ──
        if today_snap:
            st.markdown("---")
            st.subheader("🎯 今日信号快照")
            snap_rows = []
            for s in today_snap:
                if s.get("price") is None:
                    continue
                snap_rows.append({
                    "代码": s["code"],
                    "价格": f"{s['price']:.2f}",
                    "买入信号": s.get("buy_label", "-"),
                    "买入评分": s.get("buy_score", 0),
                    "卖出信号": s.get("sell_label", "-"),
                    "卖出评分": s.get("sell_score", 0),
                    "Kelly": f"{s.get('kelly_final', 0):.1f}%",
                    "数据源": s.get("data_source", "-"),
                    "真实数据": "✅" if not s.get("is_mock", True) else "❌",
                })
            if snap_rows:
                st.dataframe(pd.DataFrame(snap_rows), width="stretch", hide_index=True)

        # ── 信号验证结果 ──
        if validation and validation.get("by_horizon"):
            st.markdown("---")
            st.subheader("🔍 信号准确性验证")
            st.caption("基于历史快照回溯验证: 买入信号之后 N 天是否上涨? 卖出信号之后 N 天是否下跌?")

            horizons = validation.get("by_horizon", {})
            cols = st.columns(len(horizons))
            for i, (h_key, h_data) in enumerate(horizons.items()):
                with cols[i % len(cols)]:
                    st.markdown(f"#### {h_key}")
                    for sig_type in ["buy", "sell", "hold"]:
                        sd = h_data.get(sig_type, {})
                        if sd.get("total", 0) > 0:
                            label = {"buy": "🟢买入", "sell": "🔴卖出", "hold": "⚪持有"}.get(sig_type, sig_type)
                            wr = sd.get("win_rate", 0)
                            avg_ret = sd.get("avg_ret_pct", 0)
                            excess_line = ""
                            if sd.get("avg_excess_ret_pct") is not None:
                                excess_line = (f"<br>中证红利超额: {sd['avg_excess_ret_pct']:+.2f}% | "
                                               f"超额胜率 {sd['excess_win_rate']:.1f}%")
                            color = "#00ff88" if wr > 60 else "#ffaa00" if wr > 45 else "#ff4444"
                            st.markdown(
                                f"<div style='border-left:3px solid {color};padding:4px 8px;margin:4px 0'>"
                                f"<b>{label}</b>: {wr:.1f}% 胜率 (n={sd['total']})<br>"
                                f"平均收益: {avg_ret:+.2f}%{excess_line}"
                                f"</div>", unsafe_allow_html=True
                            )

            # 信号明细
            details = validation.get("details", [])
            if details:
                with st.expander(f"📋 信号明细 (最近 {len(details)} 条)", expanded=False):
                    detail_rows = []
                    for d in details[-50:]:
                        detail_rows.append({
                            "信号日期": d["signal_date"],
                            "代码": d["code"],
                            "类型": d["signal_type"],
                            "买入分": d["buy_score"],
                            "卖出分": d["sell_score"],
                            "入场价": d["entry_price"],
                            "出场价": d["exit_price"],
                            "收益%": f"{d['return_pct']:+.2f}%",
                            "超额%": (f"{d['excess_ret_pct']:+.2f}%" if d.get("excess_ret_pct") is not None else "-"),
                            "周期": f"{d['horizon']}d",
                            "正确": "✅" if d["correct"] else "❌",
                        })
                    if detail_rows:
                        st.dataframe(pd.DataFrame(detail_rows), width="stretch", hide_index=True)

            total = validation.get("total_signals", 0)
            if total > 0:
                st.caption(f"共验证 {total} 条信号 | 验证时间: {validation.get('validated_at', '-')}")
        else:
            if today_snap:
                st.info("信号验证需要至少 2 天的快照数据积累。继续使用系统, 每天会自动生成快照供后续验证。")

        # ── 交易记录 ──
        trades = pf.get("trades", [])
        if trades:
            st.markdown("---")
            st.subheader("📝 虚拟交易记录")
            trade_rows = []
            for t in trades[-30:]:
                trade_rows.append({
                    "日期": t["date"],
                    "代码": t["code"],
                    "方向": "🟢买入" if t["action"] == "buy" else "🔴卖出",
                    "股数": t["shares"],
                    "价格": f"{t['price']:.2f}",
                    "买入分": t.get("buy_score", "-"),
                    "卖出分": t.get("sell_score", "-"),
                    "原因": t.get("reason", "-"),
                    "数据源": t.get("data_source", "-"),
                })
            if trade_rows:
                st.dataframe(pd.DataFrame(trade_rows), width="stretch", hide_index=True)

        # ── 数据真实性声明 ──
        st.markdown("---")
        st.markdown("""
        <div style='background:#1a1a2e;border:1px solid #333;border-radius:8px;padding:12px;margin:8px 0'>
        <b>🔒 数据真实性声明</b><br>
        • 所有行情数据来自 <b>东方财富</b> (via AKShare / 腾讯行情接口)<br>
        • 每条记录标记 <code>data_source</code> 和 <code>is_mock</code> 字段<br>
        • <code>is_mock=False</code> 表示真实行情数据, 不使用模拟数据<br>
        • 信号计算复用系统已有的 <b>李氏评分 + Kelly + 技术面</b> 引擎<br>
        • 模拟盘结果仅供参考, 不构成投资建议
        </div>
        """, unsafe_allow_html=True)

        # ── 定时任务管理 ──
        st.markdown("---")
        st.subheader("⏰ 定时任务管理")
        sc1, sc2, sc3 = st.columns(3)
        sched = get_schedule_status()

        with sc1:
            st.metric("定时状态", "✅ 已启用" if sched["enabled"] else "❌ 已禁用")
        with sc2:
            st.metric("计划时间", sched["run_time"])
        with sc3:
            st.metric("今日状态", "✅ 已完成" if sched["today_done"] else ("⏳ 待运行" if sched["is_trading_day"] else "🌙 非交易日"))

        if sched.get("last_run"):
            st.caption(f"上次运行: {sched['last_run']} | 状态: {sched.get('last_run_status', '-')}")

        col_sched1, col_sched2, col_sched3 = st.columns(3)
        with col_sched1:
            if st.button("▶️ 立即运行 (强制)", key="paper_force"):
                with st.spinner("强制运行模拟盘验证..."):
                    res = check_and_run_daily(force=True)
                st.success(f"完成: {res.get('status')} - {res.get('reason', '')}")
                st.rerun()
        with col_sched2:
            new_time = st.text_input("执行时间 (HH:MM)", value=sched["run_time"], key="paper_time")
            if st.button("💾 保存时间", key="paper_save_time"):
                set_schedule_config(run_time=new_time)
                st.success(f"保存: 每日 {new_time} 自动运行")
                st.rerun()
        with col_sched3:
            if st.button("🔄 切换启用" if sched["enabled"] else "▶️ 启用定时", key="paper_toggle"):
                set_schedule_config(enabled=not sched["enabled"])
                st.rerun()

    # ── Tab 14: 真实成交对账 ──

def render_t14(t14):
    with t14:
        import tempfile

        from modules.broker_import import (
            parse_trade_table, import_records, load_broker_trades,
            update_push_link, reconcile_report)
        from modules.push_audit import load_push_log, push_summary
        from modules.execution_log import (
            ensure_pending_from_audit, record_manual, confirm_from_broker,
            get_execution_log, pending_summary, pending_reminders)

        ensure_pending_from_audit()
        confirm_from_broker()

        st.markdown("### 🧾 推送 → 模拟 → 真实成交对账")
        st.caption("A股：导入券商真实成交明细，按「日期+代码」关联推送审计，再与模拟盘成交比对价格偏差与已实现盈亏。")

        report = reconcile_report()
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("真实成交", report["total_trades"])
        c2.metric("已关联推送", report["linked_trades"])
        c3.metric("模拟匹配", report["paper_matched"])
        c4.metric("均价偏差", "—" if report["avg_price_deviation_pct"] is None else f"{report['avg_price_deviation_pct']:.2f}%")
        c5.metric("已实现盈亏", f"¥{report['realized_pnl']:,.2f}")
        st.caption(f"真实成交总费用 ¥{report['total_fees']:,.2f} | 分红到账 ¥{report['dividend_income']:,.2f} | 含分红总盈利 ¥{report['total_profit']:,.2f}")

        by_code = report.get("by_code") or {}
        if by_code:
            code_rows = [{"代码": code, "已实现盈亏": round(v.get("pnl", 0), 2), "费用": round(v.get("fees", 0), 2)}
                         for code, v in by_code.items()]
            with st.expander("按代码盈亏明细", expanded=False):
                st.dataframe(pd.DataFrame(code_rows), width="stretch", hide_index=True)

        st.markdown("---")
        st.subheader("📋 人工跟单执行登记")
        st.caption("收到选股推送后人工下单，在这里登记结果；导入券商成交后自动核验。")
        esum = pending_summary()
        e1, e2, e3, e4 = st.columns(4)
        e1.metric("推送", esum["total"])
        e2.metric("待执行", esum["pending"])
        e3.metric("成交确认", esum["confirmed"])
        e4.metric("忽略", esum["ignored"])
        reminders = pending_reminders()
        if reminders:
            st.warning(f"有 {len(reminders)} 条推送待确认执行，请在下方登记或忽略。")
        exec_rows = get_execution_log(limit=30)
        for entry in exec_rows:
            label = f"{entry['date']} | {entry['push_id']} | {len(entry.get('codes') or [])} 只 | {entry['status']}"
            with st.expander(label, expanded=False):
                st.write("标的: " + ", ".join(entry.get("codes") or []))
                if entry.get("broker_codes"):
                    st.success("真实成交核验: " + ", ".join(entry["broker_codes"]))
                if entry.get("manual_note"):
                    st.caption("备注: " + entry["manual_note"])
                status_opt = st.radio(
                    "登记结果", ["executed", "partial", "ignored"],
                    index=0, key=f"exec_radio_{entry['push_id']}",
                    format_func=lambda x: {"executed": "已按推送下单", "partial": "部分执行", "ignored": "忽略（不买）"}[x])
                note = st.text_input("原因/备注", key=f"exec_note_{entry['push_id']}")
                if st.button("保存登记", key=f"exec_save_{entry['push_id']}"):
                    record_manual(entry["push_id"], status_opt, note)
                    st.success("已保存")
                    st.rerun()
        if not exec_rows:
            st.info("暂无执行记录。每日选股推送发出后会自动生成待执行记录。")

        st.markdown("---")
        st.subheader("💸 分红到账登记")
        from modules.dividend_log import add_dividend, get_dividend_log
        dg1, dg2, dg3 = st.columns(3)
        div_code = dg1.text_input("证券代码", key="div_code")
        div_date = dg2.date_input("到账日期", key="div_date")
        div_amount = dg3.number_input("分红金额(¥)", min_value=0.0, step=10.0, key="div_amount")
        div_note = st.text_input("备注（如中期分红/年报分红）", key="div_note")
        if st.button("登记分红", key="div_add_btn"):
            if not div_code.strip() or div_amount <= 0:
                st.error("请填写证券代码和正数金额")
            else:
                add_dividend(div_code.strip(), div_code.strip(), div_date.strftime("%Y-%m-%d"), div_amount, div_note)
                st.success("已登记")
                st.rerun()
        div_rows = get_dividend_log(limit=50)
        if div_rows:
            st.dataframe(pd.DataFrame(div_rows), width="stretch", hide_index=True)
        else:
            st.info("暂无分红记录。长期持有策略建议把每笔到账分红登记进来，盈利统计才会完整。")

        st.markdown("---")
        st.subheader("📥 导入 A 股券商成交")
        uploaded = st.file_uploader("上传券商导出的成交明细（Table_*.xlsx / .xls）", type=["xlsx", "xls"], key="broker_upload")
        if uploaded is not None:
            tmp_path = os.path.join(tempfile.gettempdir(),
                                    f"broker_{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}_{uploaded.name}")
            try:
                with open(tmp_path, "wb") as f:
                    f.write(uploaded.getvalue())
                records = parse_trade_table(tmp_path)
                if not records:
                    st.warning("未解析到有效成交记录，请确认表头包含：成交日期、证券代码、委托方向、成交数量、成交均价")
                else:
                    res = import_records(records)
                    st.success(f"导入 {res['added']} 条，跳过重复 {res['duplicates']} 条")
                    st.rerun()
            except Exception as exc:
                st.error(f"导入失败：{exc}")
            finally:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        trades = load_broker_trades(limit=200)
        if not trades:
            st.info("暂无真实成交记录。上传券商成交表后，这里会展示每笔成交与推送、模拟盘的关联状态。")
        else:
            st.subheader("🔗 手动关联推送")
            unlinked = [t for t in trades if not t.get("push_id")]
            if unlinked:
                st.warning(f"有 {len(unlinked)} 条成交尚未关联推送；可在下方手动关联。")
            idx_options = list(range(len(trades)))
            sel_idx = st.selectbox(
                "选择成交记录", idx_options,
                format_func=lambda i: (f"{trades[i]['executed_at']} {trades[i]['code']} "
                                       f"{trades[i]['name']} {'买入' if trades[i]['side'] == 'buy' else '卖出'} "
                                       f"{trades[i]['shares']}股"),
                key="broker_trade_select")
            push_log = load_push_log(limit=200)
            push_options = [("", "不关联")]
            push_options += [(r["push_id"],
                              f"{r['push_id']} | {r.get('meta', {}).get('date', '')} | {r.get('status', '')}")
                             for r in push_log]
            sel_push = st.selectbox("关联推送", push_options, format_func=lambda p: p[1], key="broker_push_select")
            if st.button("保存关联", key="broker_link_btn"):
                trade_ref = trades[sel_idx].get("trade_ref", "")
                if not trade_ref:
                    st.error("该成交缺少成交编号，无法手动关联")
                else:
                    changed = update_push_link(trade_ref, sel_push[0] or None)
                    st.success(f"已更新 {changed} 条成交关联")
                    st.rerun()

            st.subheader("📋 真实成交明细")
            trade_rows = []
            for t in trades:
                trade_rows.append({
                    "成交时间": t.get("executed_at", "-"),
                    "代码": t.get("code", "-"),
                    "名称": t.get("name", "-"),
                    "方向": "🟢买入" if t.get("side") == "buy" else "🔴卖出",
                    "股数": t.get("shares", 0),
                    "均价": t.get("price", 0),
                    "金额": t.get("amount", 0),
                    "费用": t.get("fee_total", 0),
                    "推送": t.get("push_id") or "未关联",
                    "导入时间": t.get("imported_at", "-"),
                })
            st.dataframe(pd.DataFrame(trade_rows), width="stretch", hide_index=True)

        st.markdown("---")
        st.subheader("📜 推送审计")
        summary = push_summary(limit=1000)
        a1, a2, a3 = st.columns(3)
        a1.metric("审计记录", summary["total"])
        a2.metric("送达", summary["sent"])
        a3.metric("送达率", "—" if summary["delivery_rate"] is None else f"{summary['delivery_rate']:.1f}%")
        audit_rows = []
        for rec in load_push_log(limit=200):
            meta = rec.get("meta") or {}
            audit_rows.append({
                "时间": rec.get("ts", "-"),
                "推送ID": rec.get("push_id", "-"),
                "通道": rec.get("channel", "-"),
                "状态": "✅ 成功" if rec.get("status") == "sent" else "❌ 失败",
                "类型": meta.get("kind", "-"),
                "日期": meta.get("date", "-"),
                "市场": meta.get("market", "-"),
                "内容摘要": (rec.get("content_preview") or "").replace("\n", " "),
                "重试": rec.get("retries", 0),
                "耗时ms": rec.get("latency_ms", "-"),
            })
        if audit_rows:
            st.dataframe(pd.DataFrame(audit_rows), width="stretch", hide_index=True)
        else:
            st.info("暂无推送审计记录。推送会写入 data/push_audit.jsonl，每条记录含内容哈希，可核对消息是否被篡改。")

    # ── Tab 15: 仓位管理 ──

def render_t15(t15):
    with t15:
        from modules.position_manager import build_position_plan
        from modules.cost_tier import calc_overall_tier

        if df is None or df.empty:
            st.info("请先刷新持仓数据后再查看仓位管理")
        else:
            cost_data = ct_load()
            tier_by_code = {}
            for _, r in df.iterrows():
                code = str(r.get("code", ""))
                price = float(r.get("price") or 0)
                if code and price > 0:
                    tier_by_code[code] = calc_overall_tier(code, price).get("tier", "数据不足")
            plan = build_position_plan(df.to_dict("records"), tier_by_code=tier_by_code)
            s = plan["summary"]
            st.markdown("### 🧭 仓位管理")
            c1, c2, c3, c4, c5, c6 = st.columns(6)
            c1.metric("当前总仓位", f"{s['total_current']:.1f}%")
            c2.metric("目标总仓位", f"{s['total_target']:.1f}%")
            c3.metric("现金", f"{s['cash_ratio']:.1f}%")
            c4.metric("可加仓", f"{s['addable']}只")
            c5.metric("暂停/停止", f"{s['paused'] + s['stopped']}只")
            c6.metric("单票超限", f"{s['over_single_limit']}只")
            if s["total_current"] >= s["max_total"]:
                st.warning("总仓位已达组合上限，暂停新增资金")
            if s["freshness_codes"]:
                st.warning("数据新鲜度异常：" + "、".join(s["freshness_codes"]))

            st.subheader("📋 逐只仓位结论")
            verdict_label = {"可加仓": "🟢可加仓", "暂停": "🟡暂停", "持有": "⚪持有", "停止加仓": "🔴停止加仓"}
            entry_rows = []
            for e in plan["entries"]:
                entry_rows.append({
                    "代码": e["code"], "名称": e["name"], "当前%": e["current_ratio"],
                    "目标%": e["target_ratio"], "可加%": e["gap_ratio"],
                    "评分": e["lishing"], "股息率%": e["dy"], "档位": e["tier"],
                    "结论": verdict_label.get(e["verdict"], e["verdict"]), "原因": e["reasons"], "分红": e["div_reinvest"],
                })
            st.dataframe(pd.DataFrame(entry_rows), width="stretch", hide_index=True)

            st.subheader("📐 层级仓位")
            layer_rows = []
            for l in plan["layers"]:
                layer_rows.append({
                    "层级": l["layer"], "上限%": l["invested_cap"], "当前%": l["current_ratio"],
                    "目标%": l["target_ratio"], "余量%": l["spare_ratio"], "状态": l["status"],
                })
            st.dataframe(pd.DataFrame(layer_rows), width="stretch", hide_index=True)




    # -- Tab 16: Munger Checklist --

def render_t16(t16):
    with t16:
        from modules.munger_checklist import build_checklist

        st.markdown("### 🧠 芒格清单")
        st.caption("6 问程序化决策：股价大跌时先拆结构再动手 — 自动计算所有持仓")

        if df is None or df.empty:
            st.info("请先刷新持仓数据再查看芒格清单")
        else:
            score_hist = load_score_history()
            codes = df["code"].tolist()
            buy_scores = {}
            for _, rr in df.iterrows():
                c = rr.get("code", "")
                s = rr.get("lishing")
                if c and s is not None:
                    buy_scores[c] = s

            action_colors = {"add": "#00ff88", "hold": "#ffd166", "reduce": "#ff9f43", "sell": "#ff4444", "freeze": "#888"}
            action_labels = {"add": "🟢加仓", "hold": "⚪持有", "reduce": "🟡减仓", "sell": "🔴卖出", "freeze": "⏸冻结"}

            # 批量计算所有股票
            reports = []
            progress = st.progress(0, text="计算芒格清单...")
            for i, code in enumerate(codes):
                row_data = df[df["code"] == code].iloc[0].to_dict()
                try:
                    report = build_checklist(
                        code, row_data, df=df,
                        layer_summaries=layer_summaries if "layer_summaries" in dir() else None,
                        score_hist=score_hist,
                        manual_div_yields=MANUAL_DIV_YIELDS,
                        layers_cfg=LAYERS if "LAYERS" in dir() else None,
                        buy_scores=buy_scores,
                    )
                    q6 = report["q6"]
                    reports.append({
                        "code": code,
                        "name": row_data.get("name", code),
                        "action": q6.get("action", ""),
                        "label": q6.get("label", ""),
                        "reason": q6.get("reason", ""),
                        "quantity_pct": q6.get("quantity_pct"),
                        "execution_note": q6.get("execution_note", ""),
                        "q1": report.get("q1", {}).get("conclusion", ""),
                        "q2": report.get("q2", {}).get("conclusion", ""),
                        "q3": report.get("q3", {}).get("conclusion", ""),
                        "q4": report.get("q4", {}).get("conclusion", ""),
                        "q5": report.get("q5", {}).get("conclusion", ""),
                        "counter": report.get("counter_evidence", ""),
                        "full": report,
                    })
                except Exception as e:
                    reports.append({"code": code, "name": row_data.get("name", code),
                                    "action": "freeze", "label": "计算异常", "reason": str(e),
                                    "q1": "", "q2": "", "q3": "", "q4": "", "q5": "", "counter": ""})
                progress.progress((i + 1) / len(codes), text=f"计算 {code}...")
            progress.empty()

            # 汇总表格
            import pandas as pd
            summary_rows = []
            for r in reports:
                ac = r.get("action", "")
                summary_rows.append({
                    "代码": r["code"],
                    "名称": r["name"],
                    "建议": f"{action_labels.get(ac, ac)}",
                    "理由": r.get("reason", "")[:40],
                    "调仓%": r.get("quantity_pct"),
                    "Q1公司变": r.get("q1", "")[:20],
                    "Q2买因": r.get("q2", "")[:20],
                    "Q3怕啥": r.get("q3", "")[:20],
                    "反证": r.get("counter", "")[:30],
                })
            if summary_rows:
                st.subheader("📋 全部持仓芒格清单汇总")
                st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

            # 逐只展开详情
            st.subheader("📝 逐只详情")
            for r in reports:
                ac = r.get("action", "")
                color = action_colors.get(ac, "#888")
                with st.expander(f"{r['name']} ({r['code']}) — {r.get('label', '')}", expanded=False):
                    st.markdown(f"<span style='font-size:1.3em;font-weight:bold;color:{color}'>{r.get('label', '')}</span>", unsafe_allow_html=True)
                    st.caption(f"理由: {r.get('reason', '')}")
                    if r.get("quantity_pct"):
                        st.info(f"建议调整: {r['quantity_pct']:.1f}%  |  {r.get('execution_note', '')}")
                    for qkey, qlabel in [("q1", "Q1 公司变了吗？"), ("q2", "Q2 我当初为什么买？"),
                                          ("q3", "Q3 市场在怕什么？"), ("q4", "Q4 仓位还合理吗？"),
                                          ("q5", "Q5 有更好的机会吗？")]:
                        st.markdown(f"**{qlabel}**: {r.get(qkey, '')}")
                    st.markdown(f"**反证条件**: {r.get('counter', '')}")


    # -- Tab 17: Business Valuation --

def render_t17(t17):
    with t17:
        from modules.valuation import valuate, push_lines

        st.markdown("### 🧭 主营业务估值")
        st.caption("主营质量分(ROE/现金流/负债) + 合理PE/PB + 内在价值 + 安全边际 — 自动计算所有持仓")

        if df is None or df.empty:
            st.info("请先刷新持仓数据再查看估值")
        else:
            val_codes = df["code"].tolist()

            # 批量计算所有股票
            val_results = []
            progress = st.progress(0, text="计算估值...")
            for i, code in enumerate(val_codes):
                row_data = df[df["code"] == code].iloc[0].to_dict()
                try:
                    result = valuate(row_data)
                    result["code"] = code
                    result["name"] = row_data.get("name", code)
                    result["price"] = row_data.get("price", 0)
                    val_results.append(result)
                except Exception as e:
                    val_results.append({"code": code, "name": row_data.get("name", code),
                                        "available": False, "error": str(e)})
                progress.progress((i + 1) / len(val_codes), text=f"计算 {code}...")
            progress.empty()

            # 汇总表格
            import pandas as pd
            summary_rows = []
            for r in val_results:
                if not r.get("available"):
                    summary_rows.append({
                        "代码": r["code"], "名称": r.get("name", ""),
                        "现价": r.get("price", 0),
                        "质量分": "—", "合理PE": "—", "内在价值": "—",
                        "安全边际": "—", "贵不贵": r.get("error", "数据不足"),
                        "值不值": "—",
                    })
                else:
                    summary_rows.append({
                        "代码": r["code"], "名称": r.get("name", ""),
                        "现价": r.get("price", 0),
                        "质量分": r.get("quality", 0),
                        "合理PE": round(r.get("fair_pe", 0) or 0, 1),
                        "内在价值": round(r.get("intrinsic", 0) or 0, 2),
                        "安全边际": f"{r.get('margin_of_safety', 0):.1f}%" if r.get("margin_of_safety") is not None else "—",
                        "贵不贵": r.get("price_level", "—"),
                        "值不值": r.get("verdict", "—"),
                    })
            if summary_rows:
                st.subheader("📋 全部持仓估值汇总")
                st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

            # 逐只详情
            st.subheader("📝 逐只详情")
            for r in val_results:
                if not r.get("available"):
                    with st.expander(f"{r.get('name', '')} ({r['code']}) — 数据不足", expanded=False):
                        st.warning(f"数据不足: {r.get('missing', [])}")
                    continue
                with st.expander(f"{r.get('name', '')} ({r['code']}) — {r.get('price_level', '')}", expanded=False):
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("主营质量分", f"{r.get('quality', 0):.0f}/100")
                    c2.metric("合理PE", f"{r.get('fair_pe', 0):.1f}" if r.get("fair_pe") else "N/A")
                    c3.metric("内在价值", f"{r.get('intrinsic', 0):.2f}" if r.get("intrinsic") else "N/A")
                    c4.metric("安全边际", f"{r.get('margin_of_safety', 0):.1f}%" if r.get("margin_of_safety") is not None else "N/A")

                    st.markdown(f"**贵不贵**: {r.get('price_level', 'N/A')}")
                    st.markdown(f"**值不值得价值投资**: {r.get('verdict', 'N/A')}")

                    qparts = r.get("quality_parts", {})
                    if qparts:
                        st.markdown("**主营质量分构成**")
                        pc1, pc2, pc3 = st.columns(3)
                        pc1.metric("ROE", f"{qparts.get('roe', 0):.0f}/50")
                        pc2.metric("现金流", f"{qparts.get('ocf', 0):.0f}/30")
                        pc3.metric("低负债", f"{qparts.get('debt', 0):.0f}/20")

                    li = r.get("li", {})
                    munger = r.get("munger", {})
                    col_a, col_b = st.columns(2)
                    with col_a:
                        st.markdown("**💰 嘉诚诚视角**")
                        st.markdown(f"结论: {li.get('conclusion', 'N/A')}")
                        for ck in li.get("checks", []):
                            st.markdown(f"- {ck}")
                    with col_b:
                        st.markdown("**🧠 芒格视角**")
                        st.markdown(f"结论: {munger.get('conclusion', 'N/A')}")
                        for ck in munger.get("checks", []):
                            st.markdown(f"- {ck}")

    # ── Tab 18: 亚洲多空监测 ──

def render_t18(t18):
    with t18:
        from modules.asia_monitor.dashboard import render_dashboard
        render_dashboard()


# dispatch
render_t1(t1)
render_t2(t2)
render_t3(t3)
render_t4(t4)
render_t5(t5)
render_t6(t6)
render_t7(t7)
render_t8(t8)
render_t9(t9)
render_t10(t10)
render_t11(t11)
render_t12(t12)
render_t13(t13)
render_t14(t14)
render_t15(t15)
render_t16(t16)
render_t17(t17)
render_t18(t18)

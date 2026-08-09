import streamlit as st  # noqa
# 本模块由 app.py 自动抽取生成

import streamlit as st
import pandas as pd
from modules.config import (WATCH_LIST)
from modules.data.market import (fetch_smas, fetch_price_percentiles)
from modules.regime import (detect_regime, fetch_macro_signals, get_factor_regime)
from modules.signals import (evaluate_buy, evaluate_sell, get_high_250d, get_buy_score, save_score_snapshot, format_fix_summary)

def render(tab, df=None, layer_summaries=None):
    with tab:
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

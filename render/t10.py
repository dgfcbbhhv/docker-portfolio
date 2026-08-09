import streamlit as st  # noqa
# 本模块由 app.py 自动抽取生成

import streamlit as st
import pandas as pd

def render(tab, df=None, layer_summaries=None):
    with tab:
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

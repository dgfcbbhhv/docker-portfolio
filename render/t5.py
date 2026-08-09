import streamlit as st  # noqa
# 本模块由 app.py 自动抽取生成

import streamlit as st
import pandas as pd
from modules.config import (PORT_A)
from modules.backtest import (fetch_histories)
from modules.optimizer import (optimize)
from modules.stress import (single_day, industry_collapse, correlation_break, monte_carlo_stress)

def render(tab, df=None, layer_summaries=None):
    with tab:
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

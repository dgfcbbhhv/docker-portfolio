import streamlit as st  # noqa
# 本模块由 app.py 自动抽取生成

import streamlit as st
import pandas as pd
from modules.config import (PORT_A, PORT_HK)
from modules.data.fundamental import (fetch_fins, fetch_fins_hk)
from modules.backtest import (backtest_scenarios, fetch_histories)
from modules.alpha_beta import (decompose_portfolio)

def render(tab, df=None, layer_summaries=None):
    with tab:
        st.markdown("### \U0001F3AF 三场景回测")
        st.caption("⚠️ 本回测基于历史后复权数据, 存在幸存者偏差; 单边交易成本0.1%")
        c1,c2=st.columns(2)
        with c1: bt_years=st.slider("回测年数",1,10,5,1,key="bt_years")
        with c2: bt_cap=st.slider("单只上限",10,50,30,5,key="bt_cap")
        bt_btn=st.button("\U0001F680 开始回测",type="primary",key="bt_run")

        if bt_btn:
            with st.spinner("回测中(约30秒)..."):
                fins=dict(fetch_fins(list(PORT_A.keys())))
                fins.update(fetch_fins_hk(list(PORT_HK.keys())))
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

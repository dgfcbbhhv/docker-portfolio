import logging
import streamlit as st  # noqa
logger = logging.getLogger(__name__)
# 本模块由 app.py 自动抽取生成

import streamlit as st
import pandas as pd
from modules.cost_tier import (load_all as ct_load, calc_overall_tier)
from modules.lishing import (grade_label)

def render(tab, df=None, layer_summaries=None):
    with tab:
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
            sub2["占比"]=sub2.apply(lambda r: "%.0f%%"%r["ratio"] if pd.notna(r.get("ratio")) else ("%.0f%%"%r["weight"] if pd.notna(r.get("weight")) else "-"),axis=1)
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
                from modules.daily_picks import classify_operation
                from modules.holdings import get_actual_holdings as _gah
                try:
                    _ah = _gah()
                    _pos = (_ah or {}).get("positions") or {}
                except Exception:
                    _ah, _pos = None, {}
                code = r.get("code")
                held = code in _pos
                _cat, label = classify_operation(r, held, holdings=_ah)
                return label
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
        except Exception as _e:
            logger.warning(f"[{__name__}] %s", _e, exc_info=True)

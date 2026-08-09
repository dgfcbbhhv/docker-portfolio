import logging
import streamlit as st  # noqa
logger = logging.getLogger(__name__)
# 本模块由 app.py 自动抽取生成

import streamlit as st
import pandas as pd

def render(tab, df=None, layer_summaries=None):
    with tab:
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

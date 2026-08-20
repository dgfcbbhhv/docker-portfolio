import logging
import streamlit as st  # noqa
logger = logging.getLogger(__name__)
# 本模块由 app.py 自动抽取生成

import streamlit as st
import pandas as pd
from modules.signals import (load_score_history)
from modules.config import (MANUAL_DIV_YIELDS, LAYERS)

def render(tab, df=None, layer_summaries=None):
    with tab:
        from modules.munger_checklist import build_checklist

        st.markdown("### 🧠 芒格清单")
        st.caption("6 问程序化决策：股价大跌时先拆结构再动手 — 自动计算所有持仓（含 Q0 参考点重置）")

        if df is None or df.empty:
            st.info("请先刷新持仓数据再查看芒格清单")
        else:
            from modules.cost_tier import calc_overall_tier as _calc_tier
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
                    _price = float(row_data.get("price") or 0)
                    _tier = _calc_tier(code, _price).get("tier", "数据不足") if _price > 0 else None
                    report = build_checklist(
                        code, row_data, df=df,
                        layer_summaries=layer_summaries if "layer_summaries" in dir() else None,
                        score_hist=score_hist,
                        manual_div_yields=MANUAL_DIV_YIELDS,
                        layers_cfg=LAYERS if "LAYERS" in dir() else None,
                        buy_scores=buy_scores,
                        tier=_tier,
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
                        "q0": report.get("q0", {}).get("conclusion", ""),
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
                                    "q0": "", "q1": "", "q2": "", "q3": "", "q4": "", "q5": "", "counter": ""})
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
                    "Q0持现金": r.get("q0", "")[:12],
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
                    for qkey, qlabel in [("q0", "Q0 参考点重置（若持现金还买吗？）"),
                                          ("q1", "Q1 公司变了吗？"), ("q2", "Q2 我当初为什么买？"),
                                          ("q3", "Q3 市场在怕什么？"), ("q4", "Q4 仓位还合理吗？"),
                                          ("q5", "Q5 有更好的机会吗？")]:
                        st.markdown(f"**{qlabel}**: {r.get(qkey, '')}")
                    st.markdown(f"**反证条件**: {r.get('counter', '')}")

import streamlit as st  # noqa
# 本模块由 app.py 自动抽取生成

import logging
import streamlit as st
import pandas as pd
from modules.cost_tier import (load_all as ct_load)
logger = logging.getLogger(__name__)

def render(tab, df=None, layer_summaries=None):
    with tab:
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
            capital = None
            try:
                from modules.holdings import get_actual_holdings
                _ah = get_actual_holdings()
                if _ah and _ah.get("capital"):
                    capital = float(_ah["capital"])
                if not capital:
                    # broker 源无本金概念(capital=0), 回退到模拟盘/虚拟本金
                    from modules.paper_trader import VIRTUAL_CAPITAL, _get_paper_portfolio
                    _pf = _get_paper_portfolio() or {}
                    capital = float(_pf.get("capital") or VIRTUAL_CAPITAL or 0) or None
            except Exception as _e:
                logger.warning("t15 capital 读取失败: %s", _e)
            plan = build_position_plan(df.to_dict("records"), tier_by_code=tier_by_code, capital=capital)
            s = plan["summary"]
            st.markdown("### 🧭 仓位管理")
            if capital:
                st.caption(f"总资产 ≈ ¥{capital:,.0f}｜单笔风险预算默认每笔最多亏 1.5%（止损 5%）")
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
                ladder_txt = ""
                if e.get("add_step"):
                    ladder_txt = f"第{e['add_step']}层 +{e.get('add_pct', 0)}%"
                    alloc = e.get("ladder_alloc") or []
                    if alloc:
                        ladder_txt += f"（{'>'.join(f'+{a}' for a in alloc)}）"
                    ac = e.get("add_count", 0)
                    if ac:
                        ladder_txt += f" 已加{ac}次"
                rb = e.get("risk_budget")
                rb_txt = ""
                if rb and rb.get("max_value"):
                    rb_txt = f"≤¥{rb['max_value']:,.0f}"
                    if e.get("risk_cap_pct") is not None:
                        rb_txt += f"（{e['risk_cap_pct']:.1f}%）"
                entry_rows.append({
                    "代码": e["code"], "名称": e["name"], "当前%": e["current_ratio"],
                    "目标%": e["target_ratio"], "可加%": e["gap_ratio"],
                    "评分": e["lishing"], "股息率%": e["dy"], "档位": e["tier"],
                    "结论": verdict_label.get(e["verdict"], e["verdict"]), "金字塔": ladder_txt,
                    "单笔风险上限": rb_txt,
                    "原因": e["reasons"], "分红": e["div_reinvest"],
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

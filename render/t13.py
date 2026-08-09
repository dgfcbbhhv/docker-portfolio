import streamlit as st  # noqa
# 本模块由 app.py 自动抽取生成

import streamlit as st
import pandas as pd

def render(tab, df=None, layer_summaries=None):
    with tab:
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

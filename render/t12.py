import streamlit as st  # noqa
# 本模块由 app.py 自动抽取生成

import streamlit as st
import pandas as pd

def render(tab, df=None, layer_summaries=None):
    with tab:
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

import logging
import streamlit as st  # noqa
logger = logging.getLogger(__name__)
# 本模块由 app.py 自动抽取生成

import streamlit as st
import pandas as pd
import os
import datetime

def render(tab, df=None, layer_summaries=None):
    with tab:
        import tempfile

        from modules.broker_import import (
            parse_trade_table, import_records, load_broker_trades,
            update_push_link, reconcile_report)
        from modules.push_audit import load_push_log, push_summary
        from modules.execution_log import (
            ensure_pending_from_audit, record_manual, confirm_from_broker,
            get_execution_log, pending_summary, pending_reminders)

        ensure_pending_from_audit()
        confirm_from_broker()

        st.markdown("### 🧾 推送 → 模拟 → 真实成交对账")
        st.caption("A股：导入券商真实成交明细，按「日期+代码」关联推送审计，再与模拟盘成交比对价格偏差与已实现盈亏。")

        # —— 实际持仓三处对账（成交/影子/成本分层）——
        try:
            from modules.actual_holdings import reconcile as _ah_reconcile
            from modules.actual_holdings import sync_shadow_from_broker, fix_cost_batches_from_broker
            st.markdown("#### 🔍 实际持仓三处对账（真实成交 / 影子账号 / 成本分层）")
            _ah = _ah_reconcile()
            if _ah["rows"]:
                _ah_rows = pd.DataFrame([
                    {"代码": r["code"], "真实成交": r["broker_shares"], "影子账号": r["shadow_shares"],
                     "成本分层": r["cost_shares"], "一致": "✅" if r["ok"] else "❌"}
                    for r in _ah["rows"]])
                st.dataframe(_ah_rows, width="stretch", hide_index=True)
            if _ah["issues"]:
                st.error("对账不一致：" + "；".join(_ah["issues"]))
            else:
                st.success("三处实际持仓数据一致")
            ac1, ac2 = st.columns(2)
            with ac1:
                if st.button("🔄 用真实成交同步影子账号", key="ah_sync_shadow"):
                    _r = sync_shadow_from_broker()
                    st.success(_r["note"] if _r["updated"] else "影子账号已一致")
                    st.rerun()
            with ac2:
                if st.button("🔧 按真实成交修正成本分层", key="ah_fix_cost"):
                    _r = fix_cost_batches_from_broker()
                    st.success(_r["note"] if _r["updated"] else "成本分层已一致")
                    st.rerun()
            st.markdown("---")
        except Exception as _ahe:
            st.warning("实际持仓对账不可用: " + str(_ahe))

        report = reconcile_report()
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("真实成交", report["total_trades"])
        c2.metric("已关联推送", report["linked_trades"])
        c3.metric("模拟匹配", report["paper_matched"])
        c4.metric("均价偏差", "—" if report["avg_price_deviation_pct"] is None else f"{report['avg_price_deviation_pct']:.2f}%")
        c5.metric("已实现盈亏", f"¥{report['realized_pnl']:,.2f}")
        st.caption(f"真实成交总费用 ¥{report['total_fees']:,.2f} | 分红到账 ¥{report['dividend_income']:,.2f} | 含分红总盈利 ¥{report['total_profit']:,.2f}")

        by_code = report.get("by_code") or {}
        if by_code:
            code_rows = [{"代码": code, "已实现盈亏": round(v.get("pnl", 0), 2), "费用": round(v.get("fees", 0), 2)}
                         for code, v in by_code.items()]
            with st.expander("按代码盈亏明细", expanded=False):
                st.dataframe(pd.DataFrame(code_rows), width="stretch", hide_index=True)

        st.markdown("---")
        st.subheader("📋 人工跟单执行登记")
        st.caption("收到选股推送后人工下单，在这里登记结果；导入券商成交后自动核验。")
        esum = pending_summary()
        e1, e2, e3, e4 = st.columns(4)
        e1.metric("推送", esum["total"])
        e2.metric("待执行", esum["pending"])
        e3.metric("成交确认", esum["confirmed"])
        e4.metric("忽略", esum["ignored"])
        reminders = pending_reminders()
        if reminders:
            st.warning(f"有 {len(reminders)} 条推送待确认执行，请在下方登记或忽略。")
        exec_rows = get_execution_log(limit=30)
        for entry in exec_rows:
            label = f"{entry['date']} | {entry['push_id']} | {len(entry.get('codes') or [])} 只 | {entry['status']}"
            with st.expander(label, expanded=False):
                st.write("标的: " + ", ".join(entry.get("codes") or []))
                if entry.get("broker_codes"):
                    st.success("真实成交核验: " + ", ".join(entry["broker_codes"]))
                if entry.get("manual_note"):
                    st.caption("备注: " + entry["manual_note"])
                status_opt = st.radio(
                    "登记结果", ["executed", "partial", "ignored"],
                    index=0, key=f"exec_radio_{entry['push_id']}",
                    format_func=lambda x: {"executed": "已按推送下单", "partial": "部分执行", "ignored": "忽略（不买）"}[x])
                note = st.text_input("原因/备注", key=f"exec_note_{entry['push_id']}")
                if st.button("保存登记", key=f"exec_save_{entry['push_id']}"):
                    record_manual(entry["push_id"], status_opt, note)
                    st.success("已保存")
                    st.rerun()
        if not exec_rows:
            st.info("暂无执行记录。每日选股推送发出后会自动生成待执行记录。")

        st.markdown("---")
        st.subheader("💸 分红到账登记")
        from modules.dividend_log import add_dividend, get_dividend_log
        dg1, dg2, dg3 = st.columns(3)
        div_code = dg1.text_input("证券代码", key="div_code")
        div_date = dg2.date_input("到账日期", key="div_date")
        div_amount = dg3.number_input("分红金额(¥)", min_value=0.0, step=10.0, key="div_amount")
        div_note = st.text_input("备注（如中期分红/年报分红）", key="div_note")
        if st.button("登记分红", key="div_add_btn"):
            if not div_code.strip() or div_amount <= 0:
                st.error("请填写证券代码和正数金额")
            else:
                add_dividend(div_code.strip(), div_code.strip(), div_date.strftime("%Y-%m-%d"), div_amount, div_note)
                st.success("已登记")
                st.rerun()
        div_rows = get_dividend_log(limit=50)
        if div_rows:
            st.dataframe(pd.DataFrame(div_rows), width="stretch", hide_index=True)
        else:
            st.info("暂无分红记录。长期持有策略建议把每笔到账分红登记进来，盈利统计才会完整。")

        st.markdown("---")
        st.subheader("📥 导入 A 股券商成交")
        uploaded = st.file_uploader("上传券商导出的成交明细（Table_*.xlsx / .xls）", type=["xlsx", "xls"], key="broker_upload")
        if uploaded is not None:
            tmp_path = os.path.join(tempfile.gettempdir(),
                                    f"broker_{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}_{uploaded.name}")
            try:
                with open(tmp_path, "wb") as f:
                    f.write(uploaded.getvalue())
                records = parse_trade_table(tmp_path)
                if not records:
                    st.warning("未解析到有效成交记录，请确认表头包含：成交日期、证券代码、委托方向、成交数量、成交均价")
                else:
                    res = import_records(records)
                    st.success(f"导入 {res['added']} 条，跳过重复 {res['duplicates']} 条")
                    st.rerun()
            except Exception as exc:
                st.error(f"导入失败：{exc}")
            finally:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        trades = load_broker_trades(limit=200)
        if not trades:
            st.info("暂无真实成交记录。上传券商成交表后，这里会展示每笔成交与推送、模拟盘的关联状态。")
        else:
            st.subheader("🔗 手动关联推送")
            unlinked = [t for t in trades if not t.get("push_id")]
            if unlinked:
                st.warning(f"有 {len(unlinked)} 条成交尚未关联推送；可在下方手动关联。")
            idx_options = list(range(len(trades)))
            sel_idx = st.selectbox(
                "选择成交记录", idx_options,
                format_func=lambda i: (f"{trades[i]['executed_at']} {trades[i]['code']} "
                                       f"{trades[i]['name']} {'买入' if trades[i]['side'] == 'buy' else '卖出'} "
                                       f"{trades[i]['shares']}股"),
                key="broker_trade_select")
            push_log = load_push_log(limit=200)
            push_options = [("", "不关联")]
            push_options += [(r["push_id"],
                              f"{r['push_id']} | {r.get('meta', {}).get('date', '')} | {r.get('status', '')}")
                             for r in push_log]
            sel_push = st.selectbox("关联推送", push_options, format_func=lambda p: p[1], key="broker_push_select")
            if st.button("保存关联", key="broker_link_btn"):
                trade_ref = trades[sel_idx].get("trade_ref", "")
                if not trade_ref:
                    st.error("该成交缺少成交编号，无法手动关联")
                else:
                    changed = update_push_link(trade_ref, sel_push[0] or None)
                    st.success(f"已更新 {changed} 条成交关联")
                    st.rerun()

            st.subheader("📋 真实成交明细")
            trade_rows = []
            for t in trades:
                trade_rows.append({
                    "成交时间": t.get("executed_at", "-"),
                    "代码": t.get("code", "-"),
                    "名称": t.get("name", "-"),
                    "方向": "🟢买入" if t.get("side") == "buy" else "🔴卖出",
                    "股数": t.get("shares", 0),
                    "均价": t.get("price", 0),
                    "金额": t.get("amount", 0),
                    "费用": t.get("fee_total", 0),
                    "推送": t.get("push_id") or "未关联",
                    "导入时间": t.get("imported_at", "-"),
                })
            st.dataframe(pd.DataFrame(trade_rows), width="stretch", hide_index=True)

        st.markdown("---")
        st.subheader("📜 推送审计")
        summary = push_summary(limit=1000)
        a1, a2, a3 = st.columns(3)
        a1.metric("审计记录", summary["total"])
        a2.metric("送达", summary["sent"])
        a3.metric("送达率", "—" if summary["delivery_rate"] is None else f"{summary['delivery_rate']:.1f}%")
        audit_rows = []
        for rec in load_push_log(limit=200):
            meta = rec.get("meta") or {}
            audit_rows.append({
                "时间": rec.get("ts", "-"),
                "推送ID": rec.get("push_id", "-"),
                "通道": rec.get("channel", "-"),
                "状态": "✅ 成功" if rec.get("status") == "sent" else "❌ 失败",
                "类型": meta.get("kind", "-"),
                "日期": meta.get("date", "-"),
                "市场": meta.get("market", "-"),
                "内容摘要": (rec.get("content_preview") or "").replace("\n", " "),
                "重试": rec.get("retries", 0),
                "耗时ms": rec.get("latency_ms", "-"),
            })
        if audit_rows:
            st.dataframe(pd.DataFrame(audit_rows), width="stretch", hide_index=True)
        else:
            st.info("暂无推送审计记录。推送会写入 data/push_audit.jsonl，每条记录含内容哈希，可核对消息是否被篡改。")

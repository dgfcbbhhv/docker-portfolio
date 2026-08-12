import logging
import streamlit as st  # noqa
logger = logging.getLogger(__name__)
# 本模块由 app.py 自动抽取生成

import streamlit as st
import pandas as pd
from modules.cost_tier import (load_all as ct_load, calc_overall_tier, add_batch, update_dividend, set_dividend_source, delete_batch)
from modules.config import (MANUAL_DIV_YIELDS)
from modules.data.app_cache import (fetch_divs_hk_cached as _cached_fetch_divs_hk, fetch_divs_cached as _cached_fetch_divs)

def render(tab, df=None, layer_summaries=None):
    with tab:
        st.markdown("### \U0001F4B0 持有成本分层管理")
        st.caption("按成本股息率 vs 当前股息率的比值，将每只标的分为三档：🟡黄金(≥1.2) → 可加仓 · ⚪白银(0.9~1.2) → 持有 · 🔴警戒(<0.9) → 暂停加仓")
        st.caption("⚠️ 成本数据需手动录入你的实际建仓/加仓记录（日期+股数+买入价）")

        cost_data = ct_load()
        if df is not None and not df.empty:
            all_codes = df["code"].tolist()
            if "group" not in df.columns:
                groups = [""]
            else:
                groups = ["A股持仓", "港股持仓"]
            for grp in groups:
                if grp:
                    sub = df[df["group"] == grp]
                    if sub.empty:
                        continue
                else:
                    sub = df
                ic = {"A股持仓": "\U0001F1E6\U0001F1F0", "港股持仓": "\U0001F1ED\U0001F1F0"}.get(grp, "\U0001F4CA")
                if grp:
                    st.subheader(ic + " " + grp)
                for _, rr in sub.iterrows():
                    code = rr["code"]
                    name = rr["name"]
                    price = rr.get("price")
                    if price is None or pd.isna(price) or float(price or 0) <= 0:
                        continue
                    price = float(price)
                    stock = cost_data.get(code, {"batches": [], "dividend_per_share": 0.0})
                    overall = calc_overall_tier(code, price)
                    has_data = bool(stock.get("batches")) and stock.get("dividend_per_share", 0) > 0
                    if not has_data:
                        with st.expander(f"{name} ({code}) — ⚪ 请录入建仓/加仓数据", expanded=False):
                            st.markdown(f"**当前股价**: {price:.2f}")
                            st.markdown(f"**手动股息率**: {MANUAL_DIV_YIELDS.get(code, {}).get('ttm', MANUAL_DIV_YIELDS.get(code, {}).get('lfy', 0)):.2f}%")
                            st.caption("录入你的实际买入批次：日期、股数、买入价")
                            with st.form(f"quick_init_{code}"):
                                q1, q2, q3 = st.columns(3)
                                with q1: q_date = st.text_input("买入日期", placeholder="2025-01-15", value="", key=f"qi_d_{code}")
                                with q2: q_shares = st.number_input("股数", min_value=0, step=100, value=1000, key=f"qi_s_{code}")
                                with q3: q_cost = st.number_input("买入价", min_value=0.0, step=0.01, value=price, key=f"qi_c_{code}")
                                q_note = st.text_input("备注", placeholder="建仓/加仓", key=f"qi_n_{code}")
                                if st.form_submit_button("✅ 确认录入"):
                                    if q_date and q_shares > 0 and q_cost > 0:
                                        add_batch(code, q_date, int(q_shares), float(q_cost), q_note)
                                        dps = MANUAL_DIV_YIELDS.get(code, {}).get("ttm", MANUAL_DIV_YIELDS.get(code, {}).get("lfy", 0)) / 100 * q_cost
                                        update_dividend(code, dps)
                                        set_dividend_source(code, "manual")
                                        st.success(f"✅ 已录入: {q_shares}股 @ {q_cost:.2f}，每股分红 {dps:.4f}")
                                        st.rerun()
                                    else:
                                        st.error("请填写完整的日期、股数、买入价")
                        continue
                    tier_color = {"黄金": "#ffd700", "白银": "#cccccc", "警戒": "#ff4444"}.get(overall["tier"], "#888")
                    tier_icon = {"黄金": "\U0001F7E1", "白银": "\u26AA", "警戒": "\U0001F534"}.get(overall["tier"], "⚪")
                    with st.expander(
                        f"{tier_icon} {name} | 整体：{overall['tier']} | 均价 {overall['avg_cost']} | "
                        f"成本息率 {overall['cost_dy']}% | 比值 {overall['ratio']}",
                        expanded=False
                    ):
                        col_a, col_b, col_c, col_d = st.columns(4)
                        col_a.metric("整体档位", overall["tier"], delta_color="off")
                        col_b.metric("加权均价", f"{overall['avg_cost']:.2f}")
                        col_c.metric("成本股息率", f"{overall['cost_dy']:.2f}%")
                        col_d.metric("比值", f"{overall['ratio']:.2f}")
                        st.markdown(
                            f"<div style='border-left:4px solid {tier_color};padding:8px 12px;margin:8px 0'>"
                            f"<b>建议</b>：{ '✅ 整体处于黄金档，新增资金可加仓' if overall['tier']=='黄金' else '⏸️ 整体处于白银档，持有不动' if overall['tier']=='白银' else '⛔ 整体处于警戒档，暂不加仓，等待成本摊薄' }"
                            f"</div>", unsafe_allow_html=True
                        )
                        div_source = stock.get("dividend_source", "auto")
                        auto_div = 0
                        if div_source == "auto":
                            if grp == "港股持仓":
                                div_map = _cached_fetch_divs_hk(tuple(all_codes))
                            else:
                                div_map = _cached_fetch_divs(tuple(all_codes))
                            _div_info = div_map.get(code, 0); auto_div = _div_info.get("ttm", 0) if isinstance(_div_info, dict) else (_div_info or 0)
                        cur_div = stock.get("dividend_per_share", auto_div)
                        st.caption(f"每股分红：{'自动' if div_source=='auto' else '手动'} {cur_div:.4f}")
                        if div_source == "auto" and auto_div > 0 and cur_div != auto_div:
                            update_dividend(code, auto_div)
                            st.rerun()
                        col_e, col_f = st.columns([1, 1])
                        with col_e:
                            new_div = st.number_input(
                                "每股分红（手动覆盖）", value=float(cur_div), format="%.4f",
                                key=f"div_{code}", step=0.01
                            )
                            if st.button("应用分红", key=f"set_div_{code}"):
                                update_dividend(code, new_div)
                                set_dividend_source(code, "manual" if new_div != auto_div else "auto")
                                st.rerun()
                        with col_f:
                            if st.button("\U0001F504 恢复自动读取", key=f"auto_div_{code}"):
                                update_dividend(code, auto_div)
                                set_dividend_source(code, "auto")
                                st.rerun()
                        st.markdown("**批次明细**")
                        bt_data = []
                        for b in overall.get("batches", []):
                            bt_data.append({
                                "日期": b.get("date", ""),
                                "股数": b["shares"],
                                "成本": b["cost"],
                                "成本息率": f"{b['cost_dy']:.2f}%",
                                "比值": f"{b['ratio']:.2f}",
                                "档位": b["tier"],
                                "备注": b.get("note", ""),
                            })
                        if bt_data:
                            df_batches = pd.DataFrame(bt_data)
                            st.dataframe(df_batches, width="stretch", hide_index=True)
                        with st.expander("新增批次 / 管理"):
                            c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
                            with c1:
                                b_date = st.text_input("买入日期", placeholder="2025-01-15", key=f"bd_{code}")
                            with c2:
                                b_shares = st.number_input("股数", min_value=0, step=100, key=f"bs_{code}")
                            with c3:
                                b_cost = st.number_input("买入成本(元/股)", min_value=0.0, step=0.01, key=f"bc_{code}")
                            with c4:
                                b_note = st.text_input("备注", placeholder="建仓/加仓", key=f"bn_{code}")
                            if st.button("\U0001F4E4 添加批次", key=f"add_batch_{code}"):
                                add_batch(code, b_date, int(b_shares), float(b_cost), b_note)
                                st.success(f"已添加 {name} 批次: {b_shares}股 @ {b_cost}")
                                st.rerun()
                            if overall.get("batches"):
                                del_idx = st.number_input(
                                    "删除批次序号（从0开始）", min_value=0,
                                    max_value=len(overall["batches"]) - 1, value=0,
                                    key=f"del_{code}"
                                )
                                if st.button("\U0001F5D1 删除批次", key=f"del_btn_{code}"):
                                    delete_batch(code, int(del_idx))
                                    st.rerun()

        # 批量导入功能
        st.divider()
        st.markdown("### \U0001F4E5 批量导入成本数据")

        col1, col2 = st.columns(2)
        with col1:
            st.caption("方式一: 粘贴CSV数据")
            st.code("""格式: 代码,日期,股数,成本,备注
    示例:
    600900,2025-01-15,1000,12.50,建仓
    600900,2025-06-20,500,11.80,加仓
    601398,2025-03-10,2000,5.20,首次买入""")
            with st.form("batch_import"):
                import_text = st.text_area("粘贴CSV数据", height=150, key="import_text")
                if st.form_submit_button("导入"):
                    if import_text.strip():
                        lines = import_text.strip().split("\n")
                        success_count = 0
                        error_lines = []
                        for i, line in enumerate(lines, 1):
                            parts = line.split(",")
                            if len(parts) < 4:
                                error_lines.append(f"第{i}行: 格式错误")
                                continue
                            try:
                                code = parts[0].strip()
                                date = parts[1].strip()
                                shares = int(parts[2].strip())
                                cost = float(parts[3].strip())
                                note = parts[4].strip() if len(parts) > 4 else ""
                                add_batch(code, date, shares, cost, note)
                                success_count += 1
                            except Exception as e:
                                error_lines.append(f"第{i}行: {str(e)}")

                        if success_count > 0:
                            st.success(f"成功导入 {success_count} 条记录")
                        if error_lines:
                            st.warning(f"以下行导入失败:\n" + "\n".join(error_lines))
                    else:
                        st.warning("请输入数据")

        with col2:
            st.caption("方式二: 上传券商对账单 (自动识别买入记录)")
            uploaded_file = st.file_uploader("上传CSV/XLSX文件", type=["csv", "xlsx"])
            if uploaded_file is not None:
                try:
                    if uploaded_file.name.endswith(".xlsx"):
                        df_upload = pd.read_excel(uploaded_file)
                    else:
                        df_upload = pd.read_csv(uploaded_file)
                    if df_upload is None or df_upload.empty:
                        st.warning("文件为空或无数据")
                    else:
                        cols = [str(c) for c in df_upload.columns]
                        df_upload.columns = cols
                        def _find(*keys):
                            for c in cols:
                                low = str(c).lower()
                                if any(k in low for k in keys):
                                    return c
                            return None
                        def_code = _find("代码") or _find("code")
                        def_date = _find("成交日期") or _find("日期") or _find("date")
                        def_shares = _find("成交数量") or _find("数量") or _find("shares")
                        def_cost = _find("成交均价") or _find("成交价格") or _find("价格") or _find("cost")
                        def_dir = _find("委托方向") or _find("买卖") or _find("操作") or _find("方向") or _find("交易类型")

                        st.subheader("识别到的交易记录")
                        st.dataframe(df_upload.head(10))

                        st.caption("请确认列映射（已自动识别，可调整）")
                        col_options = cols
                        idx_map = {}
                        for key, d in [("code", def_code), ("date", def_date),
                                       ("shares", def_shares), ("cost", def_cost)]:
                            idx_map[key] = col_options.index(d) if d in col_options else 0
                        col_map = {}
                        col_map["code"] = st.selectbox("股票代码列", col_options, index=idx_map["code"], key="map_code")
                        col_map["date"] = st.selectbox("成交日期列", col_options, index=idx_map["date"], key="map_date")
                        col_map["shares"] = st.selectbox("成交数量列", col_options, index=idx_map["shares"], key="map_shares")
                        col_map["cost"] = st.selectbox("成交价格列", col_options, index=idx_map["cost"], key="map_cost")

                        st.caption("买入→成本分层批次；卖出→影子账户交易记录")
                        import_sells = st.checkbox("同时导入卖出记录（进入影子账户）", value=True,
                                                    help="需要已识别买卖方向列; 取消则只导入买入")
                        dir_col = st.selectbox("买卖方向列(可选)", [""] + col_options,
                                               index=(col_options.index(def_dir) + 1) if def_dir in col_options else 0,
                                               key="map_dir")

                        def _norm_code(v):
                            s = str(v).strip().upper()
                            if s.endswith(".0") and s[:-2].isdigit():
                                s = s[:-2]
                            for suf in (".SH", ".SZ", ".HK", ".US"):
                                if s.endswith(suf):
                                    s = s[:-3]
                            for pref in ("SH", "SZ", "HK"):
                                if s.startswith(pref) and s[2:].isdigit():
                                    s = s[2:]
                            return s.zfill(6) if s.isdigit() else s

                        if st.button("开始导入", key="file_import"):
                            from modules.shadow_account import add_sell
                            buy_success = 0
                            sell_success = 0
                            dup = 0
                            skipped_other = 0
                            errors = []
                            buys_net = {}  # code -> 本次导入净买入(股)
                            rows = []
                            for i, row in df_upload.iterrows():
                                try:
                                    dval = str(row.get(dir_col, "") or "") if dir_col else "买"
                                    is_buy = "买" in dval or "B" in dval.upper()
                                    is_sell = "卖" in dval or "S" in dval.upper()
                                    code = _norm_code(row[col_map["code"]])
                                    date = str(row[col_map["date"]]).strip()[:10]
                                    shares = int(float(row[col_map["shares"]]))
                                    price = float(row[col_map["cost"]])
                                    if not code or shares <= 0 or price <= 0:
                                        errors.append(f"第{i + 2}行: 代码/股数/价格无效")
                                        continue
                                    rows.append((is_buy, is_sell, code, date, shares, price))
                                except Exception as e:
                                    errors.append(f"第{i + 2}行: {e}")

                            for is_buy, is_sell, code, date, shares, price in rows:
                                if is_buy:
                                    buys_net[code] = buys_net.get(code, 0) + shares
                                if is_sell:
                                    buys_net[code] = buys_net.get(code, 0) - shares

                            for is_buy, is_sell, code, date, shares, price in rows:
                                try:
                                    if is_buy:
                                        if add_batch(code, date, shares, price, "导入"):
                                            buy_success += 1
                                        else:
                                            dup += 1
                                    elif is_sell and import_sells:
                                        if add_sell(code, date, shares, price, "导入"):
                                            sell_success += 1
                                        else:
                                            dup += 1
                                    else:
                                        skipped_other += 1
                                except Exception as e:
                                    errors.append(f"{code}: {e}")

                            oversells = {c: -n for c, n in buys_net.items() if n < 0}
                            if buy_success > 0:
                                st.success(f"成功导入 {buy_success} 条买入记录 → 成本分层")
                            if sell_success > 0:
                                st.success(f"成功导入 {sell_success} 条卖出记录 → 影子账户")
                            if dup > 0:
                                st.info(f"跳过 {dup} 条重复记录(与已有数据一致)")
                            if skipped_other > 0:
                                st.info(f"已跳过 {skipped_other} 条其他记录(红股入账等)")
                            if oversells:
                                st.warning("以下代码卖出超过本次导入的买入量, 请核对是否漏导买入:\n"
                                           + "\n".join(f"{c} 超卖 {v} 股" for c, v in oversells.items()))
                            if errors:
                                st.warning(f"以下行导入失败 ({len(errors)} 条):\n" + "\n".join(errors[:20]))
                except Exception as e:
                    st.error(f"读取文件失败: {e}")

        if not cost_data:
            st.info("还没有成本数据。在上方持仓列表中点击「初始化」开始录入。")
        else:
            st.divider()
            st.markdown("### \U0001F4CA 整体分配建议")
            st.caption("基于所有已录入标的的整体档位，给出新增资金分配方向")
            gold = []
            silver = []
            warning = []
            for code in cost_data:
                if df is not None and code in df["code"].values:
                    price = float(df[df["code"] == code]["price"].iloc[0])
                    ot = calc_overall_tier(code, price)
                    name = df[df["code"] == code]["name"].iloc[0]
                    item = f"**{name}** ({code}) — {ot['tier']} | 比值 {ot['ratio']}"
                    if ot["tier"] == "黄金":
                        gold.append(item)
                    elif ot["tier"] == "白银":
                        silver.append(item)
                    else:
                        warning.append(item)
            g, s, w = st.columns(3)
            with g:
                st.markdown("\U0001F7E1 **黄金档（可加仓）**")
                if gold:
                    for it in gold:
                        st.markdown(f"<div style='border-left:3px solid #ffd700;padding:4px 8px;margin:4px 0'>{it}</div>", unsafe_allow_html=True)
                else:
                    st.info("无")
            with s:
                st.markdown("\u26AA **白银档（持有）**")
                if silver:
                    for it in silver:
                        st.markdown(f"<div style='border-left:3px solid #ccc;padding:4px 8px;margin:4px 0'>{it}</div>", unsafe_allow_html=True)
                else:
                    st.info("无")
            with w:
                st.markdown("\U0001F534 **警戒档（暂停加仓）**")
                if warning:
                    for it in warning:
                        st.markdown(f"<div style='border-left:3px solid #ff4444;padding:4px 8px;margin:4px 0'>{it}</div>", unsafe_allow_html=True)
                else:
                    st.info("无")
            st.divider()
            st.markdown(
                "<div style='background:#1a1a2e;border:1px solid #333;border-radius:8px;padding:12px'>"
                "<b>\U0001F4A1 资金分配原则</b><br>"
                "1. 优先加仓 <b>黄金档</b> 标的<br>"
                "2. 所有标的均为 <b>白银档</b> → 持有现金等待<br>"
                "3. 出现 <b>警戒档</b> → 对该标的暂停一切加仓操作"
                "</div>", unsafe_allow_html=True
            )

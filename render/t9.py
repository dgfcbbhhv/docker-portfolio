import logging
import streamlit as st  # noqa
logger = logging.getLogger(__name__)
# 本模块由 app.py 自动抽取生成

import streamlit as st
import pandas as pd
from modules.config import (WATCH_LIST)
from modules.factors import (calculate_factors_for_stock, get_factor_descriptions, get_factor_types, filter_factors_by_type)
from modules.learner import (get_learner)
from modules.data.app_cache import (fetch_factor_history_cached as _cached_fetch_factor_history)
from modules.factor_batch import (FACTOR_BATCH, start_batch)

def render(tab, df=None, layer_summaries=None):
    with tab:
        st.markdown("### \U0001F4B0 Alpha因子智能选择 \U0001F9E0")
        st.caption("基于个股时序 IC/IR 自动推荐最佳因子 | 自动扫描所有股票 | 自动计算进化轨迹")

        watch_codes = list(WATCH_LIST.keys())
        col1, col2 = st.columns([4, 1])
        with col1:
            st.markdown(f"**扫描范围**: {len(watch_codes)} 只股票")
        with col2:
            retrain = st.button("\U0001F504 重新训练全部", key="retrain_all")

        # 批量处理所有股票 → 后台线程；进度用 st.fragment 片段级自刷，绝不重建整页
        batch_key = "factor_batch_results"

        # 「重新训练全部」清空既有结果，强制重算
        if retrain and batch_key in st.session_state:
            st.session_state.pop(batch_key, None)

        # 尚无结果且后台未跑 → 只启动一次任务
        if batch_key not in st.session_state and not FACTOR_BATCH["running"] and not FACTOR_BATCH["done"]:
            start_batch(watch_codes, do_train=retrain)

        @st.fragment(run_every=2)
        def _factor_batch_ui():
            if batch_key in st.session_state:
                return
            if FACTOR_BATCH["done"]:
                st.session_state[batch_key] = FACTOR_BATCH["results"]
                st.session_state[batch_key + "_ts"] = FACTOR_BATCH["ts"]
                st.rerun()
                return
            if FACTOR_BATCH["running"]:
                _t = FACTOR_BATCH["total"] or 1
                st.progress(min(FACTOR_BATCH["i"] / _t, 1.0),
                            text=f"后台批量训练中 {FACTOR_BATCH['i']}/{FACTOR_BATCH['total']} ...")
                st.caption("计算在后台进行，本页可继续操作；完成后自动展示结果")

        _factor_batch_ui()

        batch_results = st.session_state.get(batch_key, {})
        if FACTOR_BATCH["error"]:
            st.error("后台批量任务异常: " + str(FACTOR_BATCH["error"]))

        # ── 全局汇总 ──
        st.subheader("📊 因子全景汇总")
        _stocks = {k: v for k, v in batch_results.items() if not k.startswith("_")}
        total_trained = sum(1 for v in _stocks.values() if v.get("result"))
        total_evolving = sum(1 for v in _stocks.values() if v.get("evo_summary"))
        total_errors = sum(1 for v in _stocks.values() if v.get("error"))
        g1, g2, g3, g4 = st.columns(4)
        g1.metric("已处理", f"{len(_stocks)}只")
        g2.metric("已训练", f"{total_trained}只")
        g3.metric("进化轨迹", f"{total_evolving}只")
        g4.metric("数据不足", f"{total_errors}只")

        # 全部股票因子明细表
        summary_rows = []
        for code, data in batch_results.items():
            if not isinstance(data, dict):
                continue
            if data.get("result") and data["result"].get("recommended"):
                top1 = data["result"]["recommended"][0] if data["result"]["recommended"] else {}
                evo = data.get("evo_summary") or {}
                summary_rows.append({
                    "代码": code,
                    "名称": data["name"],
                    "行业": data["result"].get("industry", "-"),
                    "置信度": data["result"].get("confidence", "-"),
                    "Top因子": top1.get("name", "-"),
                    "IR": f"{top1.get('ir', 0):.3f}",
                    "提升中": len(evo.get("improving", [])),
                    "衰退中": len(evo.get("declining", [])),
                    "最佳因子": evo.get("best_factor", "-"),
                })
        if summary_rows:
            st.dataframe(pd.DataFrame(summary_rows), width="stretch", hide_index=True)

        # ── 每只股票详情（全部展开显示） ──
        with st.expander("📋 各股因子详情（默认折叠，点击展开）", expanded=False):
            for code, data in sorted(batch_results.items(), key=lambda x: WATCH_LIST.get(x[0], {}).get("market", "Z")):
                if not isinstance(data, dict):
                    continue
                name = data["name"]

                if data.get("error"):
                    with st.expander(f"⚠️ {code} {name} - {data['error']}", expanded=False):
                        st.warning(data["error"])
                    continue

                if not data.get("result"):
                    continue

                result = data["result"]
                train_info = data["train_info"]
                evolution = data.get("evolution")
                evo_summary = data.get("evo_summary")
                bonus = data.get("bonus", 0)

                conf_color = {"high": "#00ff88", "medium": "#ffaa00", "low": "#888"}.get(result.get("confidence", "low"), "#888")
                conf_label = {"high": "高置信度", "medium": "中置信度", "low": "低置信度"}.get(result.get("confidence", "?"), "?")
                mkt_label = {"A": "A股", "HK": "港股", "US": "美股"}.get(data.get("market", "?"), "?")

                st.markdown(f"<div style='border:2px solid {conf_color};border-radius:12px;padding:16px;margin:12px 0'>", unsafe_allow_html=True)

                # 标题行
                hdr = f"## {code} {name} [{mkt_label}]"
                hdr += f" | <span style='color:{conf_color}'>{conf_label}</span>"
                if evo_summary:
                    hdr += f" | 📈{len(evo_summary.get('improving',[]))} ➡️{len(evo_summary.get('stable',[]))} 📉{len(evo_summary.get('declining',[]))}"
                st.markdown(hdr, unsafe_allow_html=True)

                # 行业/样本/奖励分
                st.markdown(f"**行业**: {result.get('industry', '-')} | **样本**: {train_info.get('n_days', 0)}天 | **因子数**: {train_info.get('n_factors', 0)} | **动态奖励**: +{bonus:.1f}")

                if result.get("recommended"):
                    # 推荐因子卡片
                    rec_cols = st.columns(3)
                    for i, rec in enumerate(result["recommended"]):
                        with rec_cols[i]:
                            ir_color = "#00ff88" if rec["ir"] > 0.5 else "#ffaa00" if rec["ir"] > 0.2 else "#888"
                            st.markdown(f"<div class=kelly-card style='border-color:{ir_color}'>", unsafe_allow_html=True)
                            st.markdown(f"**{rec['name']}** ({rec['name_cn']})")
                            st.markdown(f"<span style='color:{ir_color};font-size:1.2rem'>**IR: {rec['ir']:.3f}**</span>", unsafe_allow_html=True)
                            st.markdown(f"IC均值: {rec['ic_mean']:.4f} | IC标准差: {rec.get('ic_std', 0):.4f}")
                            st.markdown(f"IC>0.03比例: {rec['positive_ratio']:.0%} | 样本: {rec['n_samples']}天")
                            st.markdown("</div>", unsafe_allow_html=True)

                # ── 因子进化轨迹（详细） ──
                if evolution and evo_summary:
                    st.markdown("---")
                    st.markdown("### 📈 因子进化轨迹")

                    e_cols = st.columns(5)
                    with e_cols[0]:
                        st.metric("因子总数", evo_summary["total"])
                    with e_cols[1]:
                        st.metric("📈 提升中", len(evo_summary["improving"]))
                    with e_cols[2]:
                        st.metric("➡️ 稳定", len(evo_summary["stable"]))
                    with e_cols[3]:
                        st.metric("📉 衰退中", len(evo_summary["declining"]))
                    with e_cols[4]:
                        st.metric("最佳因子", evo_summary.get("best_factor", "?"))

                    # 提升因子列表
                    if evo_summary.get("improving"):
                        st.success("📈 提升中: " + ", ".join(evo_summary["improving"]))
                    if evo_summary.get("stable"):
                        st.info("➡️ 稳定: " + ", ".join(evo_summary["stable"]))
                    if evo_summary.get("declining"):
                        st.warning("📉 衰退中: " + ", ".join(evo_summary["declining"]))

                    # 进化明细完整表格
                    evo_rows = []
                    for fname, edata in sorted(evolution.items(), key=lambda x: -abs(x[1]["mean_ic"])):
                        desc = get_factor_descriptions().get(fname, {})
                        trend_map = {"improving": "📈 提升", "declining": "📉 衰退", "stable": "➡️ 稳定"}
                        # 计算最近30天IC
                        recent_30 = edata["history"][-30:] if len(edata["history"]) >= 30 else edata["history"]
                        recent_ic = sum(h["ic"] for h in recent_30) / len(recent_30) if recent_30 else 0
                        evo_rows.append({
                            "因子": fname,
                            "中文名": desc.get("name", ""),
                            "趋势": trend_map.get(edata["trend"], "?"),
                            "均值IC": f"{edata['mean_ic']:.4f}",
                            "最新IC": f"{edata['latest_ic']:.4f}",
                            "近30天IC": f"{recent_ic:.4f}",
                            "趋势强度": f"{edata['trend_strength']:+.4f}",
                            "IC轨迹图": "查看下方",
                        })
                    if evo_rows:
                        st.dataframe(pd.DataFrame(evo_rows), width="stretch", hide_index=True)

                    # 每个因子的 IC 轨迹折线图（合并为单个多线图，避免图表过多卡死浏览器）
                    st.markdown("#### 📊 各因子 IC 时间序列")
                    ic_merged = None
                    for fname, edata in sorted(evolution.items(), key=lambda x: -abs(x[1]["mean_ic"])):
                        ic_df = pd.DataFrame(edata["history"])
                        if len(ic_df) >= 3:
                            s = ic_df.copy()
                            s["date"] = pd.to_datetime(s["date"])
                            s = s.set_index("date")["ic"].rename(fname)
                            if ic_merged is None:
                                ic_merged = s.to_frame()
                            else:
                                ic_merged = ic_merged.join(s, how="outer")
                    if ic_merged is not None:
                        st.line_chart(ic_merged, use_container_width=True, height=220)

                # ── 全部因子 IC/IR 明细 ──
                if result.get("all_results"):
                    st.markdown("---")
                    st.markdown("### 📋 全部因子 IC/IR 明细")
                    ic_data = []
                    for fname, fdata in sorted(result["all_results"].items(), key=lambda x: -abs(x[1].get("ir", 0))):
                        desc = get_factor_descriptions().get(fname, {})
                        ir_val = fdata.get("ir", 0)
                        ir_color = "🟢" if ir_val > 0.5 else "🟡" if ir_val > 0.2 else "🔴"
                        ic_data.append({
                            "因子": fname,
                            "名称": desc.get("name", ""),
                            "IC均值": f"{fdata['ic_mean']:.4f}",
                            "IC标准差": f"{fdata['ic_std']:.4f}",
                            "IR": f"{ir_val:.3f}",
                            "IC>0.03比例": f"{fdata['positive_ratio']:.1%}",
                            "样本数": fdata.get("n_samples", 0),
                        })
                    if ic_data:
                        st.dataframe(pd.DataFrame(ic_data), width="stretch", hide_index=True)

                # ── 因子值时序图（全部因子） ──
                if data.get("hist_df") is not None:
                    st.markdown("---")
                    st.markdown("### 📈 因子值时序图")
                    try:
                        factor_vals = calculate_factors_for_stock(data["hist_df"])
                        if factor_vals:
                            fv_df = pd.DataFrame(factor_vals)
                            fv_df["date"] = data["hist_df"]["date"].values[:len(fv_df)]
                            fv_df = fv_df.set_index("date")
                            # 只显示Top因子的值（合并为单个多线图）
                            top3_names = [r["name"] for r in result.get("recommended", [])]
                            top3_names = [fn for fn in top3_names if fn in fv_df.columns]
                            if top3_names:
                                st.markdown(f"**Top因子值时序**: {', '.join(top3_names)}")
                                st.line_chart(fv_df[top3_names], use_container_width=True, height=180)
                    except Exception:
                        st.caption("因子值计算失败")

                st.markdown("</div>", unsafe_allow_html=True)

                # 存入 session_state 供其他Tab使用
                st.session_state["frec_" + code] = result.get("recommended", [])
                st.session_state["fbonus_" + code] = bonus

            st.divider()
            st.markdown("### \U0001F4CA 因子模型概览")
            st.caption("已训练的个股因子模型 (data/factor_models/)")
            import glob, json, os
            model_dir = os.path.join(os.path.dirname(__file__), "data", "factor_models")
            model_files = sorted(glob.glob(os.path.join(model_dir, "*.json")))
            if model_files and any(os.path.basename(f) not in ("_placeholder_") for f in model_files):
                model_data = []
                for mf in model_files:
                    bname = os.path.basename(mf)
                    if "_history" in bname or "fbonus" in bname or bname == "_placeholder_":
                        continue
                    try:
                        with open(mf, "r", encoding="utf-8") as fh:
                            m = json.load(fh)
                        code = m.get("code", bname.replace(".json", ""))
                        factors = m.get("factors", {})
                        if factors:
                            sf = sorted(factors.items(), key=lambda x: -abs(x[1].get("ir", 0)))
                            top3 = ", ".join([f"{n}({d.get('ir', 0):.2f})" for n, d in sf[:3]])
                        else:
                            top3 = "-"
                        model_data.append({
                            "股票": f"{code} {WATCH_LIST.get(code, {}).get('name', '')}",
                            "训练日期": m.get("trained_at", "")[:10],
                            "样本": f"{m.get('n_days', 0)}天",
                            "因子": f"{len(factors)}个",
                            "Top-3 IR": top3,
                        })
                    except Exception as _e:
                        logger.warning(f"{__name__}: {_e}", exc_info=True)
                if model_data:
                    st.dataframe(pd.DataFrame(model_data), width="stretch", hide_index=True)
                else:
                    st.caption("无已训练模型")
            else:
                st.caption("无已训练模型")

            st.divider()
            st.markdown("### 🔧 因子类型浏览")
            st.caption("以下为因子类型说明（自动全选）")
            old_types = get_factor_types()
            selected_types = st.multiselect("选择因子类型", old_types, default=old_types, key="factor_types_all")
            factor_names = {}
            for t in selected_types:
                factor_names.update(filter_factors_by_type(t))
            if factor_names:
                cols = st.columns(2)
                for idx, (name, desc) in enumerate(factor_names.items()):
                    with cols[idx % 2]:
                        st.markdown(f"<div class=kelly-card><b>{name}</b>: {desc['name']}<br><span style='color:#888'>{desc['description']}</span></div>", unsafe_allow_html=True)

            st.divider()
            st.markdown("### 📈 因子进化仪表盘（全股自动）")
            st.caption("一键计算所有股票的因子进化轨迹 | 自动判断提升/稳定/衰退")

            evo_batch_key = "factor_evolution_batch"
            need_evo_batch = evo_batch_key not in st.session_state or retrain

            if need_evo_batch:
                learner = get_learner()
                evo_batch = {}
                evo_progress = st.progress(0, text="准备计算进化轨迹...")
                for idx, code in enumerate(watch_codes):
                    name = WATCH_LIST[code]["name"]
                    evo_progress.progress(idx / len(watch_codes), text=f"计算中 ({idx+1}/{len(watch_codes)}): {code} {name}")
                    try:
                        # 优先使用已缓存的 hist_df
                        cached_hist = st.session_state.get("factor_data_" + code, {})
                        hist_df = cached_hist.get("hist_df") if isinstance(cached_hist, dict) else None
                        if hist_df is None or hist_df.empty:
                            hist_df = _cached_fetch_factor_history(code, WATCH_LIST[code]["market"])

                        if hist_df is not None and not hist_df.empty:
                            evolution = learner.compute_evolution(code, hist_df)
                            summary = learner.get_evolution_summary(evolution) if evolution else None
                            evo_batch[code] = {"name": name, "market": WATCH_LIST[code]["market"], "evolution": evolution, "summary": summary}
                        else:
                            evo_batch[code] = {"name": name, "error": "无历史数据"}
                    except Exception as e:
                        evo_batch[code] = {"name": name, "error": str(e)}

                st.session_state[evo_batch_key] = evo_batch
                evo_progress.progress(1.0, text="进化轨迹计算完成!")
                st.success(f"已计算 {len(evo_batch)} 只股票的因子进化轨迹")

            evo_batch = st.session_state.get(evo_batch_key, {})

            # 汇总统计
            total_improving = 0
            total_declining = 0
            total_stable = 0
            best_factors = {}
            for code, data in evo_batch.items():
                if data.get("summary"):
                    total_improving += len(data["summary"].get("improving", []))
                    total_declining += len(data["summary"].get("declining", []))
                    total_stable += len(data["summary"].get("stable", []))
                    bf = data["summary"].get("best_factor")
                    if bf:
                        best_factors[bf] = best_factors.get(bf, 0) + 1

            st.markdown("#### 📊 全局统计")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("提升中因子", total_improving)
            c2.metric("稳定因子", total_stable)
            c3.metric("衰退中因子", total_declining)
            top_bf = max(best_factors, key=best_factors.get) if best_factors else "-"
            c4.metric("出现最多的最佳因子", top_bf)

            # 全局汇总表
            evo_summary_rows = []
            for code, data in evo_batch.items():
                if data.get("summary"):
                    s = data["summary"]
                    evo_summary_rows.append({
                        "代码": code,
                        "名称": data["name"],
                        "提升": len(s.get("improving", [])),
                        "稳定": len(s.get("stable", [])),
                        "衰退": len(s.get("declining", [])),
                        "提升因子": ", ".join(s.get("improving", [])),
                        "衰退因子": ", ".join(s.get("declining", [])),
                        "最佳因子": s.get("best_factor", "-"),
                    })
            if evo_summary_rows:
                st.dataframe(pd.DataFrame(evo_summary_rows), width="stretch", hide_index=True)

            # 每只股票的进化详情
            st.markdown("#### 📋 各股因子进化详情（全部展开）")
            for code, data in sorted(evo_batch.items(), key=lambda x: WATCH_LIST.get(x[0], {}).get("market", "Z")):
                name = data["name"]

                if data.get("error"):
                    continue
                if not data.get("summary"):
                    continue

                summary = data["summary"]
                evolution = data["evolution"]
                mkt_label = {"A": "A股", "HK": "港股", "US": "美股"}.get(data.get("market", "?"), "?")

                st.markdown(f"<div style='border:1px solid #444;border-radius:10px;padding:12px;margin:10px 0'>", unsafe_allow_html=True)
                st.markdown(f"**{code} {name} [{mkt_label}]** | 📈{len(summary.get('improving',[]))} ➡️{len(summary.get('stable',[]))} 📉{len(summary.get('declining',[]))} | 最佳: {summary.get('best_factor', '-')}")

                if summary.get("improving"):
                    st.success("📈 提升中: " + ", ".join(summary["improving"]))
                if summary.get("stable"):
                    st.info("➡️ 稳定: " + ", ".join(summary["stable"]))
                if summary.get("declining"):
                    st.warning("📉 衰退中: " + ", ".join(summary["declining"]))

                # 进化明细表
                if evolution:
                    evo_rows = []
                    for fname, edata in sorted(evolution.items(), key=lambda x: -abs(x[1]["mean_ic"])):
                        desc = get_factor_descriptions().get(fname, {})
                        trend_map = {"improving": "📈 提升", "declining": "📉 衰退", "stable": "➡️ 稳定"}
                        recent_30 = edata["history"][-30:] if len(edata["history"]) >= 30 else edata["history"]
                        recent_ic = sum(h["ic"] for h in recent_30) / len(recent_30) if recent_30 else 0
                        evo_rows.append({
                            "因子": fname, "中文名": desc.get("name", ""),
                            "趋势": trend_map.get(edata["trend"], "?"),
                            "均值IC": f"{edata['mean_ic']:.4f}",
                            "最新IC": f"{edata['latest_ic']:.4f}",
                            "近30天IC": f"{recent_ic:.4f}",
                            "趋势强度": f"{edata['trend_strength']:+.4f}",
                        })
                    if evo_rows:
                        st.dataframe(pd.DataFrame(evo_rows), width="stretch", hide_index=True)

                        # IC轨迹图（合并为单个多线图）
                        ic_merged = None
                        for fname, edata in sorted(evolution.items(), key=lambda x: -abs(x[1]["mean_ic"])):
                            ic_df = pd.DataFrame(edata["history"])
                            if len(ic_df) >= 3:
                                s = ic_df.copy()
                                s["date"] = pd.to_datetime(s["date"])
                                s = s.set_index("date")["ic"].rename(fname)
                                if ic_merged is None:
                                    ic_merged = s.to_frame()
                                else:
                                    ic_merged = ic_merged.join(s, how="outer")
                        if ic_merged is not None:
                            st.line_chart(ic_merged, use_container_width=True, height=200)

                st.markdown("</div>", unsafe_allow_html=True)

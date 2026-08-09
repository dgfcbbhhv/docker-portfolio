import logging
import streamlit as st  # noqa
logger = logging.getLogger(__name__)
# 本模块由 app.py 自动抽取生成

import streamlit as st
import pandas as pd
import datetime
from modules.data.app_cache import (load_snapshots_cached as _cached_load_snapshots)

def render(tab, df=None, layer_summaries=None):
    with tab:
        st.markdown("### 📈 持仓历史对比")
        st.caption("自动保存持仓快照，支持多时点对比分析")

        from modules.exporter import save_snapshot, load_snapshots, SNAPSHOT_DIR

        # 自动保存快照
        if df is not None and not df.empty:
            last_snapshot = st.session_state.get("_last_snapshot_time")
            now = datetime.datetime.now()
            if last_snapshot is None or (now - last_snapshot).total_seconds() > 300:  # 5分钟
                try:
                    save_snapshot(df)
                    st.session_state["_last_snapshot_time"] = now
                except Exception as _e:
                    logger.warning(f"[{__name__}] %s", _e, exc_info=True)

        # 控件
        c1, c2, c3 = st.columns([2, 2, 1])
        with c1:
            days_range = st.slider("查看天数", 1, 30, 7, key="hist_days")
        with c2:
            metric = st.selectbox("对比指标", ["price", "dy_lfy", "kelly_final", "lishing"], 
                                  format_func=lambda x: {"price": "现价", "dy_lfy": "股息率", 
                                                          "kelly_final": "Kelly仓位", "lishing": "李氏评分"}[x],
                                  key="hist_metric")
        with c3:
            snap_count = len(list(SNAPSHOT_DIR.glob("snapshot_*.csv"))) if SNAPSHOT_DIR.exists() else 0
            st.metric("快照数", snap_count)

        # 加载快照
        snapshots = _cached_load_snapshots(days_range)

        if len(snapshots) >= 2:
            # 多时点对比
            st.subheader(f"最近 {days_range} 天快照趋势")

            # 合并数据
            merged = None
            for ts, snap_df in snapshots:
                cols_to_keep = ["code", "name", "group", metric]
                cols_exist = [c for c in cols_to_keep if c in snap_df.columns]
                snap_sub = snap_df[cols_exist].copy()
                snap_sub["timestamp"] = ts
                if merged is None:
                    merged = snap_sub
                else:
                    merged = pd.concat([merged, snap_sub], ignore_index=True)

            if merged is not None and not merged.empty:
                # 每只股票的指标变化
                codes = df["code"].tolist() if df is not None else []
                names = dict(zip(df["code"], df["name"])) if df is not None else {}

                # 使用Streamlit内置line_chart
                chart_data = None
                for code in codes:
                    stock_data = merged[merged["code"] == code].sort_values("timestamp")
                    if len(stock_data) < 2:
                        continue
                    name = names.get(code, code)
                    series = stock_data.set_index("timestamp")[metric].rename(name)
                    if chart_data is None:
                        chart_data = series.to_frame()
                    else:
                        chart_data = chart_data.join(series, how="outer")

                if chart_data is not None:
                    metric_labels = {"price": "现价", "dy_lfy": "股息率(%)", "kelly_final": "Kelly仓位(%)", "lishing": "李氏评分"}
                    st.line_chart(chart_data, height=400)

                # 变化统计
                st.subheader("📊 变化统计")
                if df is not None and not df.empty:
                    changes = []
                    for code in codes:
                        stock_data = merged[merged["code"] == code].sort_values("timestamp")
                        if len(stock_data) >= 2:
                            first_val = stock_data[metric].iloc[0]
                            last_val = stock_data[metric].iloc[-1]
                            pct_change = ((last_val - first_val) / abs(first_val) * 100) if first_val != 0 else 0
                            changes.append({
                                "代码": code,
                                "名称": names.get(code, code),
                                "起始值": round(first_val, 2),
                                "当前值": round(last_val, 2),
                                "变化%": round(pct_change, 2),
                                "趋势": "📈" if pct_change > 0 else ("📉" if pct_change < 0 else "➡️")
                            })
                    if changes:
                        changes_df = pd.DataFrame(changes).sort_values("变化%", ascending=False)
                        st.dataframe(changes_df, use_container_width=True, hide_index=True)
        elif len(snapshots) == 1:
            st.info(f"仅有 1 个快照，需要至少 2 个快照才能进行对比。继续使用系统会自动保存快照。")
        else:
            st.info("暂无快照数据。系统会在每次刷新持仓时自动保存快照。")

import logging
import streamlit as st  # noqa
logger = logging.getLogger(__name__)
# 本模块由 app.py 自动抽取生成

import streamlit as st
import pandas as pd
from modules.data.fund import (fetch_reit_list, fetch_etf_list, fetch_market_prices, fetch_fund_navs, fetch_fund_dividends, enrich_with_market, enrich_with_premium, enrich_with_dividend, _parse_dividend_summary)

def render(tab, df=None, layer_summaries=None):
    with tab:
        st.markdown("### \U0001F4B9 REIT / ETF 分析")
        mode = st.radio("分析模式", ["REITs 公募REITs", "ETF 场内基金"], horizontal=True, key="fund_mode")

        should_auto_load = "fund_result" not in st.session_state

        with st.expander("添加代码", expanded=True):
            codes_input = st.text_input("输入代码（逗号分隔）", placeholder="508068, 508098, 518600", key="fund_codes")
            col_a, col_b, col_c = st.columns(3)
            with col_a:
                if st.button("\U0001F50D 查询", key="fund_query"):
                    if not codes_input.strip():
                        st.warning("请输入代码")
                        st.stop()
                    raw = [c.strip() for c in codes_input.split(",") if c.strip()]
                    with st.spinner("获取数据..."):
                        mkt = fetch_market_prices(raw)
                        if mode.startswith("REIT"):
                            reits = fetch_reit_list()
                            fund_df = reits[reits["code"].isin(raw)].copy()
                            navs = fetch_fund_navs(raw)
                            for c in navs:
                                fund_df.loc[fund_df["code"] == c, "nav"] = navs[c]
                        else:
                            etfs = fetch_etf_list()
                            fund_df = etfs[etfs["code"].isin(raw)].copy()
                        fund_df = enrich_with_market(fund_df, mkt)
                        fund_df = enrich_with_premium(fund_df)
                        try:
                            div_all = fetch_fund_dividends()
                            div_sum = _parse_dividend_summary(div_all, raw)
                            fund_df = enrich_with_dividend(fund_df, div_sum)
                        except Exception:
                            fund_df["total_div"] = 0; fund_df["div_count"] = 0; fund_df["div_yield"] = None
                    st.session_state["fund_result"] = fund_df
            with col_b:
                if st.button("\U0001F4C2 加载全部REITs", key="load_reits"):
                    with st.spinner("加载REITs列表..."):
                        reits = fetch_reit_list()
                        codes_list = reits["code"].tolist()
                        mkt = fetch_market_prices(codes_list)
                        fund_df = reits.copy()
                        fund_df = enrich_with_market(fund_df, mkt)
                        fund_df = enrich_with_premium(fund_df)
                        try:
                            div_all = fetch_fund_dividends()
                            div_sum = _parse_dividend_summary(div_all, codes_list)
                            fund_df = enrich_with_dividend(fund_df, div_sum)
                        except Exception:
                            fund_df["total_div"] = 0; fund_df["div_count"] = 0; fund_df["div_yield"] = None
                    st.session_state["fund_result"] = fund_df
            with col_c:
                if st.button("\U0001F4C2 加载全部ETF", key="load_etfs"):
                    with st.spinner("加载ETF列表..."):
                        etfs = fetch_etf_list()
                        codes_list = etfs["code"].tolist()
                        mkt = fetch_market_prices(codes_list)
                        fund_df = etfs.copy()
                        fund_df = enrich_with_market(fund_df, mkt)
                        fund_df = enrich_with_premium(fund_df)
                    st.session_state["fund_result"] = fund_df

        if should_auto_load:
            if mode.startswith("REIT"):
                with st.spinner("加载REITs..."):
                    target_codes = ["508098", "508068"]
                    reits = fetch_reit_list()
                    df_auto = reits[reits["code"].isin(target_codes)].copy()
                    mkt = fetch_market_prices(target_codes)
                    navs = fetch_fund_navs(target_codes)
                    for c in navs:
                        df_auto.loc[df_auto["code"] == c, "nav"] = navs[c]
                    df_auto = enrich_with_market(df_auto, mkt)
                    df_auto = enrich_with_premium(df_auto)
                    try:
                        div_all = fetch_fund_dividends()
                        div_sum = _parse_dividend_summary(div_all, target_codes)
                        df_auto = enrich_with_dividend(df_auto, div_sum)
                    except Exception:
                        df_auto["total_div"] = 0; df_auto["div_count"] = 0; df_auto["div_yield"] = None
                st.session_state["fund_result"] = df_auto
            else:
                with st.spinner("加载ETF列表..."):
                    etfs = fetch_etf_list()
                    codes_list = etfs["code"].tolist()
                    mkt = fetch_market_prices(codes_list)
                    df_auto = etfs.copy()
                    df_auto = enrich_with_market(df_auto, mkt)
                    df_auto = enrich_with_premium(df_auto)
                st.session_state["fund_result"] = df_auto

        fund_df = st.session_state.get("fund_result")
        if fund_df is not None and not fund_df.empty:
            col_d, col_e = st.columns([3, 1])
            with col_e:
                show_all = st.toggle("显示全部", False, key="fund_show_all")
                if "premium" in fund_df.columns:
                    filter_opt = st.selectbox("筛选", ["全部", "仅溢价", "仅折价"], key="fund_filter")
                    if filter_opt == "仅溢价":
                        fund_df = fund_df[fund_df["premium"] > 0].copy()
                    elif filter_opt == "仅折价":
                        fund_df = fund_df[fund_df["premium"] < 0].copy()
                sort_by = st.selectbox("排序", ["代码", "价格", "折溢价率", "股息率"],
                                       key="fund_sort")
                ascending = st.checkbox("升序", False, key="fund_asc")
            display = fund_df.copy()
            if "类型" in display.columns and not show_all:
                grp = display.groupby("类型").apply(lambda x: x.head(3)).reset_index(drop=True)
                display = grp
            cols = ["code", "name"]
            if "类型" in display.columns:
                cols.append("类型")
            cols += ["price", "chg"]
            if "nav" in display.columns and "premium" in display.columns:
                cols += ["nav", "premium"]
            if "mcap" in display.columns:
                cols.append("mcap")
            if "div_yield" in display.columns:
                cols.append("div_yield")
            if "fund_type" in display.columns:
                cols.append("fund_type")
            cols = [c for c in cols if c in display.columns]
            sort_map = {"代码": "code", "价格": "price", "折溢价率": "premium", "股息率": "div_yield"}
            s_col = sort_map.get(sort_by, "code")
            if s_col in display.columns:
                display = display.sort_values(s_col, ascending=ascending)
            d = display[cols].copy()
            d.columns = ["代码", "名称"] + [c for c in d.columns[2:]]
            # None -> \"-\"
            for col in d.columns:
                if d[col].dtype == 'float64' or d[col].dtype == 'object':
                    d[col] = d[col].apply(lambda x: "-" if pd.isna(x) else x)
            st.dataframe(d, width="stretch", hide_index=True)
            if not display.empty:
                avg_premium = display["premium"].dropna().mean() if "premium" in display.columns else None
                avg_div = display["div_yield"].dropna().mean() if "div_yield" in display.columns else None
                c1, c2 = st.columns(2)
                if avg_premium is not None and pd.notna(avg_premium):
                    c1.metric("平均折溢价率", f"{avg_premium:.2f}%")
                if avg_div is not None and pd.notna(avg_div):
                    c2.metric("平均股息率", f"{avg_div:.2f}%")
        else:
            if "fund_result" in st.session_state:
                st.info("未匹配到数据，请检查代码是否正确")

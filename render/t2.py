import streamlit as st  # noqa
# 本模块由 app.py 自动抽取生成

import streamlit as st
import pandas as pd
from modules.config import (ALERT)

def render(tab, df=None, layer_summaries=None):
    with tab:
        ca,cb=st.columns([2,1])
        with ca:
            st.subheader("\U0001F4CC Kelly调仓路线")
            has=False
            if df is not None and not df.empty:
                for _,r in df[df["group"].isin(["A股持仓","港股持仓","美股"])].iterrows():
                    cur=r["ratio"] if pd.notna(r.get("ratio")) else (r.get("weight", 0) if pd.notna(r.get("weight")) else 0)
                    fa=r.get("kelly_portfolio_adj", r.get("kelly_factor_adj", r.get("kelly_vol_adj", r.get("kelly_final", 0))))
                    diff=fa-cur
                    if diff>2:
                        st.markdown("<p class=metric-up>"+chr(9650)+" 加仓 <b>"+r["name"]+"</b>: %.0f%% -> %.0f%% (+%.0f%%)</p>"%(cur,fa,diff),unsafe_allow_html=True)
                        has=True
                    elif diff<-2:
                        st.markdown("<p class=metric-down>"+chr(9660)+" 减仓 <b>"+r["name"]+"</b>: %.0f%% -> %.0f%% (%.0f%%)</p>"%(cur,fa,diff),unsafe_allow_html=True)
                        has=True
                if not has: st.info("\u2705 组合与Kelly建议基本一致")
            st.subheader("\U0001F50D 持仓体检")
            if df is not None and not df.empty:
                for _,r in df[df["group"].isin(["A股持仓","港股持仓","美股"])].iterrows():
                    iss=[]
                    is_us = "美股" in r.get("group","")
                    if not is_us and r["dy"]>0 and r["dy"]<1.5: iss.append("股息率%.2f%%"%r["dy"])
                    if pd.notna(r["pe"]) and r["pe"]>30: iss.append("PE %s"%r["pe"])
                    if pd.notna(r["chg"]) and r["chg"]<-ALERT: iss.append("回撤%.1f%%"%r["chg"])
                    if is_us:
                        if r["lishing"]<40: iss.append("成长评分%.0f"%r["lishing"])
                    else:
                        if r["lishing"]<60: iss.append("李氏评分%.0f"%r["lishing"])
                    if iss: st.error("**"+r["name"]+"**: "+"; ".join(iss))
                    else: st.success("**"+r["name"]+"**: \u2705 正常")

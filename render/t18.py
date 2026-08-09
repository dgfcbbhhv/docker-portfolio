import streamlit as st  # noqa
# 本模块由 app.py 自动抽取生成

import streamlit as st

def render(tab, df=None, layer_summaries=None):
    with tab:
        from modules.asia_monitor.dashboard import render_dashboard
        render_dashboard()

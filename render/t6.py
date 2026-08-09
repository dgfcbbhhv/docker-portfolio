import streamlit as st  # noqa
# 本模块由 app.py 自动抽取生成

import streamlit as st
from modules.alerts import (AlertSender, load_alert_history)

def render(tab, df=None, layer_summaries=None):
    with tab:
        st.markdown("### \u2139\uFE0F 全局假设声明")
        st.warning("""
        1. PE/PB: 动态TTM, 非静态年报值
        2. 波动率: 120日对数收益率年化标准差, 非已实现波动率
        3. 美股/港股股息率: 使用手动验证数据, API作为兜底
        """)
        st.markdown("""
        **数据源**: 东方财富/同花顺 via akshare, 港股 via 腾讯接口, 美股 via yfinance
        **李氏评分**: 9维度(PE/负债率/ROE/分红/现金流/PB/市值/审计/质押)+ 可选关联交易
        **Kelly**: Fractional Kelly Criterion, 继承FinceptTerminal实现
        **成长评分**: 5维度(估值分位/趋势结构/Kelly信号/波动率校准/趋势稳定性), 专用于美股
        **红利低波轮动**: 参考EasyXT视频策略, DuckDB缓存加速
        **网格交易**: 固定网格 + ATR动态网格, 支持历史回测
        **Alpha因子**: WorldQuant Alpha101 核心因子, 自动训练+进化轨迹分析
        **回测**: 基于历史后复权, 单边0.1%交易成本, 三场景(乐观/基准/悲观)
        **组合优化**: scipy.optimize, 最大夏普/最小波动率/风险平价, Monte Carlo 95%置信区间
        **压力测试**: 单日暴跌/行业全灭/相关性断裂/Monte Carlo极端模拟
        **缓存**: 行情1h / 财务30d / 分红90d, 存储在data/目录
        **并发限制**: AKShare最多3并发, 指数退避重试
        """)
        st.markdown("""
        **生产使用说明（个人实盘）**:
        1. Kelly/信号均为参考区间，不是精确仓位，不构成投资建议
        2. A/HK/US/REIT 按独立分层管理，各层投入上限与回撤红线在 `config.json` 的 `layers` 中配置
        3. 数据过期或使用手动兜底时，页面顶部会显式提示，请勿忽略
        4. 后台预警由独立进程 `python -m modules.alert_worker` 运行
        """)

        st.divider()
        st.subheader("🔔 预警设置")
        st.caption("配置多通道预警, 实时监控组合风险")
        sender=AlertSender()
        ch=st.selectbox("通知渠道",["邮件","企业微信","Telegram","Bark","Webhook"],0,key="alert_ch")
        ch_map={"邮件":"email","企业微信":"wechat","Telegram":"telegram","Bark":"bark","Webhook":"webhook"}
        with st.form("alert_cfg"):
            if ch=="邮件":
                st.caption("发件邮箱用来登录SMTP发信, 收件邮箱接收预警。可以填同一个。推荐用QQ邮箱的SMTP授权码")
                st.text_input("SMTP服务器",value=sender.cfg.get("smtp_server",""),key="smtp_s",placeholder="smtp.qq.com")
                st.text_input("SMTP端口",value=sender.cfg.get("smtp_port","465"),key="smtp_p",placeholder="465")
                st.text_input("发件邮箱(SMTP登录)",value=sender.cfg.get("smtp_user",""),key="smtp_u",placeholder="your@qq.com")
                st.text_input("SMTP授权码",value=sender.cfg.get("smtp_pass",""),type="password",key="smtp_pw",placeholder="16位授权码")
                st.text_input("收件邮箱(接收预警)",value=sender.cfg.get("alert_to",""),key="smtp_to",placeholder="your@qq.com")
            elif ch=="企业微信":
                st.text_input("Webhook URL",value=sender.cfg.get("wechat_webhook",""),key="wc_url")
            elif ch=="Telegram":
                st.text_input("Bot Token",value=sender.cfg.get("tg_token",""),key="tg_tok")
                st.text_input("Chat ID",value=sender.cfg.get("tg_cid",""),key="tg_cid")
            elif ch=="Bark":
                st.caption("推送地址: 公共 api.day.app 或自建服务器 http://NAS_IP:8080/设备Key/ ; 若填写16位加密密钥则启用AES加密推送")
                st.text_input("Bark URL",value=sender.cfg.get("bark_url",""),key="bk_url",placeholder="http://192.168.31.24:8080/设备Key/")
                st.text_input("加密密钥KEY(16字节, 可选)",value=sender.cfg.get("bark_key",""),key="bk_key",placeholder="1234567890123456",type="password")
                st.text_input("加密IV(16字符, 与App设置一致, 可选)",value=sender.cfg.get("bark_iv",""),key="bk_iv",placeholder="1234567890123456")
                if sender.cfg.get("bark_key"):
                    st.success("🔒 已启用 AES-128-CBC 加密推送, 内容对服务器不可见")
            else:
                st.text_input("Webhook URL",value=sender.cfg.get("webhook_url",""),key="wh_url")
            st.number_input("股价跌幅阈值(%)",0.0,20.0,8.0,0.5,key="th_drop")
            st.number_input("股息率变动阈值(%)",0.0,5.0,0.5,0.1,key="th_dy")
            st.number_input("Kelly变化阈值(%)",0.0,20.0,5.0,1.0,key="th_kelly")
            st.number_input("评分下降阈值",0.0,30.0,10.0,1.0,key="th_ls")
            if st.form_submit_button("保存配置"):
                sender.update_cfg(
                    smtp_server=st.session_state.get("smtp_s",""),smtp_port=st.session_state.get("smtp_p","465"),
                    smtp_user=st.session_state.get("smtp_u",""),smtp_pass=st.session_state.get("smtp_pw",""),
                    alert_to=st.session_state.get("smtp_to",""),
                    wechat_webhook=st.session_state.get("wc_url",""),
                    tg_token=st.session_state.get("tg_tok",""),tg_chat_id=st.session_state.get("tg_cid",""),
                    webhook_url=st.session_state.get("wh_url",""),
                    bark_url=st.session_state.get("bk_url",""),
                    bark_key=st.session_state.get("bk_key",""),
                    bark_iv=st.session_state.get("bk_iv",""),
                )
                st.success("✅ 配置已保存")
        if st.button("发送测试邮件",key="test_alert"):
            ok=sender.send("测试预警","这是一条测试预警消息",ch_map[ch])
            st.success("✅ 已发送") if ok else st.error("❌ 发送失败, 请检查配置")
        # 预警历史
        hist=load_alert_history()
        if not hist.empty:
            st.subheader("📋 预警历史")
            st.dataframe(hist.tail(50),width="stretch",hide_index=True)
        else:
                st.info("暂无预警记录")

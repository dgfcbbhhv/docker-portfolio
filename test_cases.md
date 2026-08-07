# 红利央企持仓分析平台 — 测试用例文档

**项目**: docker-portfolio (红利央企持仓分析平台 v4.0)
**编写日期**: 2026-07-24
**编写人**: Bug跟踪与测试验收协同助手
**测试范围**: 16个模块 + 10个Tab UI + Docker部署

---

## 一、测试计划概览

### 1.1 测试目标

验证平台从数据拉取、指标计算、信号生成到 UI 展示的完整链路正确性，覆盖核心计算引擎（Kelly / 李氏评分 / 买卖信号）、数据层（缓存/重试/降级/验证）、回测与组合优化、压力测试、预警系统及 Docker 部署。

### 1.2 测试环境

| 项目 | 配置 |
|------|------|
| 运行方式 | Docker (`docker compose up -d --build`) 或本地 `streamlit run app.py` |
| Python | 3.12 (Docker) / 3.11+ (本地) |
| 端口 | 8501 |
| 数据源 | akshare (东方财富/同花顺) + 新浪/腾讯降级源 |
| 缓存目录 | `data/` (parquet) |
| 备注目录 | `notes/` (JSON) |
| 网络 | 需访问 akshare API；NAS 环境 push2.eastmoney 可能被阻断 |

### 1.3 验收标准

| 级别 | 标准 |
|------|------|
| P0 阻断 | Kelly/李氏评分/买卖信号计算结果错误 → 不可上线 |
| P1 严重 | 数据层缓存/降级失效、UI 崩溃、除零错误 → 不可上线 |
| P2 一般 | 格式显示问题、边界值轻微偏差 → 可上线但需修复 |
| P3 建议 | 交互体验优化、文案改进 → 后续迭代 |

### 1.4 用例编号规则

`TC-<模块缩写>-<序号>`，模块缩写：CACHE/market/fund/KELLY/LSHING/SIGNAL/REGIME/COST/BT/OPT/STRESS/ALERT/UI/DOCKER

---

## 二、测试用例总表

### 模块 1: 缓存层 cache.py

| 用例ID | 类型 | 用例标题 | 前置条件 | 输入 | 预期结果 | 优先级 |
|--------|------|----------|----------|------|----------|--------|
| TC-CACHE-001 | 正常 | 缓存写入与读取 | data/ 目录可写 | `save("spot","a_all",df)` 后 `load("spot","a_all")` | 返回的 df 与写入一致，含 `_ts` 列 | P0 |
| TC-CACHE-002 | 边界 | TTL 未过期返回缓存 | spot 缓存写入后 < 3600s | `load("spot","a_all")` | 返回缓存 df，不触发重新拉取 | P0 |
| TC-CACHE-003 | 边界 | TTL 过期返回 None | spot 缓存写入后 > 3600s | `load("spot","a_all")` | 返回 None | P0 |
| TC-CACHE-004 | 边界 | fin 类 TTL 30天 | fin 缓存写入后 31天 | `load("fin","600900")` | 返回 None | P1 |
| TC-CACHE-005 | 边界 | div 类 TTL 90天 | div 缓存写入后 91天 | `load("div","600900_div")` | 返回 None | P1 |
| TC-CACHE-006 | 异常 | allow_expired 读取过期缓存 | 缓存已过期 | `load("fin","600900",allow_expired=True)` | 返回 df 且 `_expired` 列为 True | P1 |
| TC-CACHE-007 | 异常 | 缓存文件损坏 | parquet 文件被截断 | `load("spot","a_all")` | 捕获异常返回 None，不崩溃 | P1 |
| TC-CACHE-008 | 正常 | retry 指数退避重试成功 | API 第2次成功 | `retry(lambda: api_call())` (第1次抛异常) | 第2次重试成功返回结果，等待 2s+随机 | P0 |
| TC-CACHE-009 | 异常 | retry 达到最大次数抛异常 | API 连续5次失败 | `retry(fn, max_n=5)` | 抛出最后一次异常 | P0 |
| TC-CACHE-010 | 异常 | retry 超时保护 | API 单次调用 > 30s | `retry(fn, timeout=30)` | 抛出 `API call timed out (30s)` | P0 |
| TC-CACHE-011 | 正常 | 并发限流 Semaphore(3) | 同时发起5个请求 | 5个线程同时 retry | 最多3个并发，其余等待 | P1 |
| TC-CACHE-012 | 正常 | clear_cache 清除单类 | spot 有缓存文件 | `clear_cache("spot")` | data/spot/ 下 parquet 全删，fin/ 保留 | P1 |
| TC-CACHE-013 | 正常 | clear_cache 清除全部 | 多类缓存存在 | `clear_cache()` | 所有 parquet 删除 | P1 |
| TC-CACHE-014 | 正常 | validate_spot 标记异常数据 | df 含 price=0, pe=600, pb=-1 | `validate_spot(df)` | 对应行 `_flags` 含 `price_invalid;` `pe_extreme;` `pb_invalid;` | P0 |
| TC-CACHE-015 | 边界 | validate_spot 正常数据不标记 | df 所有字段正常 | `validate_spot(df)` | `_flags` 全为空字符串 | P1 |
| TC-CACHE-016 | 正常 | set_ttl_override 修改 TTL | 默认 spot TTL=3600 | `set_ttl_override("spot",60)` 后 `load` | 60s 后过期 | P2 |

### 模块 2: 行情层 market.py

| 用例ID | 类型 | 用例标题 | 前置条件 | 输入 | 预期结果 | 优先级 |
|--------|------|----------|----------|------|----------|--------|
| TC-MARKET-001 | 正常 | fetch_spot_a 拉取A股行情 | 网络正常 | `fetch_spot_a(["600900","601398"])` | 返回 df 含 c/n/price/chg/pe/pb/mcap 列，经 validate_spot 处理 | P0 |
| TC-MARKET-002 | 正常 | fetch_spot_a 命中缓存 | spot 缓存存在 | `fetch_spot_a(["600900"])` | 返回缓存的 df，不触发网络请求 | P0 |
| TC-MARKET-003 | 异常 | EM 阻断降级到新浪 | push2.eastmoney 不可达 | `fetch_spot_a(["600900"])` | 新浪行情回退成功，df 含 price/chg，pe/pb 由腾讯或THS补充 | P0 |
| TC-MARKET-004 | 异常 | 新浪也失败抛 ConnectionError | 所有数据源不可达 | `fetch_spot_a(["600900"])` | 抛出 `ConnectionError("所有数据源均失败")` | P1 |
| TC-MARKET-005 | 正常 | fetch_spot_hk 拉取港股 | 网络正常 | `fetch_spot_hk(["00002","02638"])` | 返回 df 含 c/n/price/chg，pe/pb/mcap 为 None | P0 |
| TC-MARKET-006 | 正常 | fetch_spot_a_all 全市场过滤 | 网络正常 | `fetch_spot_a_all()` | 返回 df，已剔除 ST/退/N，price>0，amount>5000万 | P0 |
| TC-MARKET-007 | 正常 | fetch_spot_a_filtered 成交额过滤 | 全市场数据已缓存 | `fetch_spot_a_filtered(amount_min=1e8)` | 仅保留 amount>=1亿的股票 | P1 |
| TC-MARKET-008 | 正常 | fetch_vol 计算波动率 | 历史数据可拉取 | `fetch_vol("600900")` | 返回 float > 0，年化波动率，缓存到 hist | P0 |
| TC-MARKET-009 | 异常 | fetch_vol 数据不足返回默认值 | 历史数据 < 10条 | `fetch_vol("600900")` | 返回 0.2 (默认值) | P1 |
| TC-MARKET-010 | 正常 | fetch_price_percentile 计算分位 | 5年历史数据可拉取 | `fetch_price_percentile("600900",years=5)` | 返回 0-100 的 float，缓存到 hist | P0 |
| TC-MARKET-011 | 异常 | fetch_price_percentile 数据不足返回50 | 历史数据 <= 20条 | `fetch_price_percentile("600900")` | 返回 50.0 (中性默认) | P1 |
| TC-MARKET-012 | 正常 | fetch_sma 计算20日均线 | 历史数据 >= 20条 | `fetch_sma("600900",20)` | 返回 float > 0 | P1 |
| TC-MARKET-013 | 异常 | fetch_sma 数据不足返回None | 历史数据 < 20条 | `fetch_sma("600900",20)` | 返回 None | P2 |
| TC-MARKET-014 | 正常 | fetch_listing_days 计算上市天数 | 历史数据可拉取 | `fetch_listing_days("600900")` | 返回 int > 0 | P2 |
| TC-MARKET-015 | 异常 | fetch_listing_days 失败返回9999 | 拉取失败 | `fetch_listing_days("999999")` | 返回 9999 (默认不限制) | P2 |
| TC-MARKET-016 | 正常 | 批量并发拉取 fetch_vols | 8只股票 | `fetch_vols(["600900","601398",...])` | 返回 dict {code: vol}，并发执行 | P1 |
| TC-MARKET-017 | 正常 | 腾讯K线回退 _fetch_tx_histories | EM不可达 | `backtest._fetch_tx_history("600900")` | 返回 df 含 date/open/close/high/low/volume | P1 |

### 模块 3: 财务层 fundamental.py

| 用例ID | 类型 | 用例标题 | 前置条件 | 输入 | 预期结果 | 优先级 |
|--------|------|----------|----------|------|----------|--------|
| TC-FUND-001 | 正常 | fetch_fins 拉取财务数据 | 网络正常 | `fetch_fins(["600900"])` | 返回 dict 含 roe/eps/debt/ocf_ratio/div_years/total_shares | P0 |
| TC-FUND-002 | 正常 | fetch_fins 命中缓存 | fin 缓存存在 | `fetch_fins(["600900"])` | 返回缓存数据，不触发网络请求 | P0 |
| TC-FUND-003 | 正常 | fetch_fins 过期缓存降级读取 | fin 缓存已过期且API失败 | `fetch_fins(["600900"])` | `allow_expired=True` 读取过期缓存，不返回空 | P1 |
| TC-FUND-004 | 正常 | fetch_divs 拉取A股分红 | 网络正常 | `fetch_divs(["600900"])` | 返回 dict {code: 每股分红}，值 >= 0 | P0 |
| TC-FUND-005 | 正常 | fetch_divs_hk 拉取港股分红 | 网络正常 | `fetch_divs_hk(["00002"])` | 返回 dict {code: 每股派息} | P0 |
| TC-FUND-006 | 异常 | 财务数据全部缺失 | API 全部失败 | `fetch_fins(["999999"])` | 返回 dict 含默认值 {roe:None, eps:None, div_years:0, audit:True} | P1 |
| TC-FUND-007 | 正常 | _pct 解析百分比字符串 | — | `_pct("15.90%")` / `_pct("15.9")` | 均返回 15.9 | P1 |
| TC-FUND-008 | 异常 | _pct 解析无效值 | — | `_pct(None)` / `_pct("abc")` | 返回 None | P2 |
| TC-FUND-009 | 正常 | ROE 优先取年报(12-31) | THS数据含季报+年报 | `fetch_fins(["600900"])` | roe 取 12-31 报告期的值，非季报 | P1 |
| TC-FUND-010 | 正常 | ocf_ratio = cfps/eps*100 | THS数据正常 | `fetch_fins(["600900"])` | ocf_ratio = 每股经营现金流/每股收益*100 | P1 |

### 模块 4: Kelly 引擎 kelly.py

| 用例ID | 类型 | 用例标题 | 前置条件 | 输入 | 预期结果 | 优先级 |
|--------|------|----------|----------|------|----------|--------|
| TC-KELLY-001 | 正常 | 标准输入计算 | — | price=25, pb=1.2, pe=8, roe=15, eps=2, debt=30, div=1.0, vol=0.18 | p≈0.8(0.5+0.3×1+0.2×1), b>0, kelly_raw>0, kelly_opt>0, kelly_final>0, kelly_vol_adj>0 | P0 |
| TC-KELLY-002 | 边界 | 股息率=5% 时 p 股息贡献拿满 | — | div=1.25, price=25 (dy=5%) | p 的 div_contrib = 0.30 (拿满) | P0 |
| TC-KELLY-003 | 边界 | 股息率>5% 时 p 钳位不超过0.95 | — | div=5, price=25 (dy=20%), roe=20 | p = 0.95 (钳位上限) | P0 |
| TC-KELLY-004 | 边界 | ROE=15% 时 p 的 ROE 贡献拿满 | — | roe=15 | roe_contrib = 0.20 | P0 |
| TC-KELLY-005 | 边界 | p 下限钳位 0.05 | — | div=0, roe=0 | p = 0.50 (base，因 div_contrib=0, roe_contrib=0) | P0 |
| TC-KELLY-006 | 边界 | PB<1 时有修复空间 pb_up>0 | — | pb=0.8 | pb_up = (1-0.8)/0.8×100 = 25.0 | P0 |
| TC-KELLY-007 | 边界 | PB>=1 时 pb_up=0 | — | pb=1.5 | pb_up = 0.0 (无修复空间) | P0 |
| TC-KELLY-008 | 边界 | b 钳位 [0.1, 10.0] | — | 极端高预期回报 | b = 10.0 (上限) | P0 |
| TC-KELLY-009 | 边界 | b 下限 0.1 | — | 预期亏损极大 | b = 0.1 (下限) | P0 |
| TC-KELLY-010 | 正常 | quarter Kelly = raw × 0.25 | — | 标准输入 | kelly_opt = kelly_raw × 0.25 × 100 (四舍五入1位) | P0 |
| TC-KELLY-011 | 正常 | 相关性折扣 = 1 - 0.75² | — | kelly_opt > 0 | kelly_final = kelly_opt × (1-0.5625) = kelly_opt × 0.4375 | P0 |
| TC-KELLY-012 | 边界 | kelly_final 不超过 MAXPOS=30 | — | 极高 kelly_opt | kelly_final = 30.0 | P0 |
| TC-KELLY-013 | 正常 | vol_adjust 高波动打折 | — | vol=0.36 (36%) | factor = (18/36)^0.5 = 0.707，kelly_vol_adj < kelly_final | P0 |
| TC-KELLY-014 | 正常 | vol_adjust 低波动奖励 | — | vol=0.09 (9%) | factor = (18/9)^0.5 = 1.414 → 钳位 1.3，kelly_vol_adj > kelly_final | P0 |
| TC-KELLY-015 | 边界 | vol_adjust 因子上限 1.3 | — | vol=0.01 (极低) | factor = 1.3 (钳位上限) | P0 |
| TC-KELLY-016 | 边界 | vol_adjust 因子下限 0.3 | — | vol=2.0 (200%) | factor = 0.3 (钳位下限) | P0 |
| TC-KELLY-017 | 边界 | vol=0 时 vol_adjust 返回1.0 | — | vol=0 | factor = 1.0，kelly_vol_adj = kelly_final | P0 |
| TC-KELLY-018 | 异常 | price=0 时不崩溃 | — | price=0, div=1 | div_yield=0, p=0.5, 不抛异常 | P0 |
| TC-KELLY-019 | 异常 | pb=None 时 safe_pb=1.0 | — | pb=None | el = 1.0×vol×100，不抛异常 | P0 |
| TC-KELLY-020 | 异常 | div=None 时 div_yield=0 | — | div=None | div_yield=0.0, p 的 div_contrib=0 | P1 |
| TC-KELLY-021 | 异常 | roe=None 时 roe 贡献=0 | — | roe=None | p 的 roe_contrib=0, p=0.5+div_contrib | P1 |
| TC-KELLY-022 | 边界 | kelly_raw 为负时 kelly_opt=0 | — | b×p < (1-p) (负期望) | kelly_raw=0 (max(0,...)), kelly_opt=0, kelly_final=0 | P0 |

### 模块 5: 李氏评分 lishing.py

| 用例ID | 类型 | 用例标题 | 前置条件 | 输入 | 预期结果 | 优先级 |
|--------|------|----------|----------|------|----------|--------|
| TC-LSHING-001 | 正常 | 全维度满分输入 | 所有数据正常 | pe=10,debt=30,roe=18,dy=5,div_years=5,ocf_ratio=120,pb=0.8,mcap=1500 | total=100, status=normal, grade=🟢极度安全 | P0 |
| TC-LSHING-002 | 正常 | 评分等级 ≥85 → 🟢 | — | 构造 total≥85 的输入 | grade="🟢 极度安全" | P0 |
| TC-LSHING-003 | 正常 | 评分等级 70-84 → 🟡 | — | 构造 total=75 的输入 | grade="🟡 符合李氏" | P0 |
| TC-LSHING-004 | 正常 | 评分等级 55-69 → 🟠 | — | 构造 total=60 的输入 | grade="🟠 谨慎" | P0 |
| TC-LSHING-005 | 正常 | 评分等级 <55 → 🔴 | — | 构造 total=40 的输入 | grade="🔴 高风险" | P0 |
| TC-LSHING-006 | 红线 | 审计非标准无保留 → 一票否决 | audit="保留意见" | `calc({...,audit:"保留意见"})` | total=0, status=red, msg含"审计异常" | P0 |
| TC-LSHING-007 | 红线 | audit=False → 一票否决 | — | `calc({...,audit:False})` | total=0, status=red, msg="审计未通过" | P0 |
| TC-LSHING-008 | 红线 | 名称含 ST → 一票否决 | name="*ST测试" | `calc({name:"*ST测试"})` | total=0, status=red, msg="ST风险" | P0 |
| TC-LSHING-009 | 红线 | 名称含"退" → 一票否决 | name="退市股" | `calc({name:"退市股"})` | total=0, status=red, msg="ST风险" | P0 |
| TC-LSHING-010 | 红线 | st_flag=True → 一票否决 | — | `calc({st_flag:True})` | total=0, status=red, msg="ST风险" | P0 |
| TC-LSHING-011 | 红线 | 连续两年现金流为负 → 一票否决 | ocf_two_years=-5 | `calc({ocf_two_years:-5})` | total=0, status=red, msg含"连续两年经营现金流为负" | P0 |
| TC-LSHING-012 | 边界 | PE<15 → 1.0 | — | pe=10 | pe 维度 raw=1.0 | P0 |
| TC-LSHING-013 | 边界 | PE 15-19 → 0.8 | — | pe=17 | pe 维度 raw=0.8 | P0 |
| TC-LSHING-014 | 边界 | PE 20-24 → 0.5 | — | pe=22 | pe 维度 raw=0.5 | P0 |
| TC-LSHING-015 | 边界 | PE 25-29 → 0.3 | — | pe=27 | pe 维度 raw=0.3 | P0 |
| TC-LSHING-016 | 边界 | PE>=30 → 0.1 | — | pe=35 | pe 维度 raw=0.1 | P0 |
| TC-LSHING-017 | 边界 | 负债率<40 → 1.0 | — | debt=30 | debt 维度 raw=1.0 | P0 |
| TC-LSHING-018 | 边界 | 负债率 40-49 → 0.8 | — | debt=45 | debt 维度 raw=0.8 | P0 |
| TC-LSHING-019 | 边界 | 负债率 50-59 → 0.6 | — | debt=55 | debt 维度 raw=0.6 | P1 |
| TC-LSHING-020 | 边界 | 负债率 60-69 → 0.3 | — | debt=65 | debt 维度 raw=0.3 | P1 |
| TC-LSHING-021 | 边界 | 负债率>=70 → 0.0 | — | debt=75 | debt 维度 raw=0.0 | P0 |
| TC-LSHING-022 | 边界 | ROE>15 → 1.0 | — | roe=18 | roe 维度 raw=1.0 | P0 |
| TC-LSHING-023 | 边界 | ROE 12-15 → 0.8 | — | roe=13 | roe 维度 raw=0.8 | P0 |
| TC-LSHING-024 | 边界 | ROE 8-12 → 0.6 | — | roe=10 | roe 维度 raw=0.6 | P1 |
| TC-LSHING-025 | 边界 | ROE 5-8 → 0.3 | — | roe=6 | roe 维度 raw=0.3 | P1 |
| TC-LSHING-026 | 边界 | ROE<=5 → 0.0 | — | roe=3 | roe 维度 raw=0.0 | P0 |
| TC-LSHING-027 | 边界 | 分红 yrs≥5&dy>4 → 1.0 | — | div_years=5, dy=4.5 | div 维度 raw=1.0 | P0 |
| TC-LSHING-028 | 边界 | 分红 yrs≥3&dy>3 → 0.8 | — | div_years=3, dy=3.5 | div 维度 raw=0.8 | P0 |
| TC-LSHING-029 | 边界 | 分红 yrs≥3 → 0.6 | — | div_years=3, dy=2 | div 维度 raw=0.6 | P1 |
| TC-LSHING-030 | 边界 | 分红 dy>3 → 0.5 | — | div_years=1, dy=3.5 | div 维度 raw=0.5 | P1 |
| TC-LSHING-031 | 边界 | 分红 dy>0 → 0.2 | — | div_years=0, dy=1 | div 维度 raw=0.2 | P2 |
| TC-LSHING-032 | 边界 | 分红 dy=0 → 0.0 | — | dy=0, div_years=0 | div 维度 raw=0.0 | P0 |
| TC-LSHING-033 | 边界 | 现金流 ocf>100 → 1.0 | — | ocf_ratio=120 | cf 维度 raw=1.0 | P0 |
| TC-LSHING-034 | 边界 | 现金流 80-100 → 0.8 | — | ocf_ratio=90 | cf 维度 raw=0.8 | P0 |
| TC-LSHING-035 | 边界 | 现金流 60-80 → 0.5 | — | ocf_ratio=70 | cf 维度 raw=0.5 | P1 |
| TC-LSHING-036 | 边界 | 现金流 0-60 → 0.3 | — | ocf_ratio=30 | cf 维度 raw=0.3 | P1 |
| TC-LSHING-037 | 边界 | 现金流<=0 → 0.0 | — | ocf_ratio=-5 | cf 维度 raw=0.0 | P0 |
| TC-LSHING-038 | 边界 | PB<0.5 → 1.0 | — | pb=0.4 | pb 维度 raw=1.0 | P0 |
| TC-LSHING-039 | 边界 | PB 0.5-0.99 → 0.8 | — | pb=0.7 | pb 维度 raw=0.8 | P1 |
| TC-LSHING-040 | 边界 | PB 1.0-1.49 → 0.6 | — | pb=1.2 | pb 维度 raw=0.6 | P1 |
| TC-LSHING-041 | 边界 | PB 1.5-1.99 → 0.4 | — | pb=1.8 | pb 维度 raw=0.4 | P2 |
| TC-LSHING-042 | 边界 | PB 2.0-2.99 → 0.2 | — | pb=2.5 | pb 维度 raw=0.2 | P2 |
| TC-LSHING-043 | 边界 | PB>=3.0 → 0.1 | — | pb=4.0 | pb 维度 raw=0.1 | P1 |
| TC-LSHING-044 | 边界 | 市值>1000 → 1.0 | — | mcap=1500 | mcap 维度 raw=1.0 | P0 |
| TC-LSHING-045 | 边界 | 市值 500-1000 → 0.8 | — | mcap=700 | mcap 维度 raw=0.8 | P1 |
| TC-LSHING-046 | 边界 | 市值 200-500 → 0.6 | — | mcap=300 | mcap 维度 raw=0.6 | P1 |
| TC-LSHING-047 | 边界 | 市值 100-200 → 0.4 | — | mcap=150 | mcap 维度 raw=0.4 | P2 |
| TC-LSHING-048 | 边界 | 市值 50-100 → 0.2 | — | mcap=80 | mcap 维度 raw=0.2 | P2 |
| TC-LSHING-049 | 边界 | 市值<=50 → 0.1 | — | mcap=30 | mcap 维度 raw=0.1 | P1 |
| TC-LSHING-050 | 权重回收 | debt=None 时跳过并权重回收 | debt 缺失 | `calc({pe:10,debt:None,roe:15,...})` | debt 维度 missing=True，actual_weight=90(100-10)，total 按实际权重归一化 | P0 |
| TC-LSHING-051 | 权重回收 | 多维度缺失时权重回收 | debt/ocf/pledge 缺失 | 多个 None | actual_weight 相应减少，total 仍归一化到实际权重 | P0 |
| TC-LSHING-052 | 权重回收 | 所有维度缺失 → total=0 | 全部 None | `calc({pe:None,debt:None,...})` | total=0, status=warning, msg="无有效数据" | P0 |
| TC-LSHING-053 | 关联交易 | 默认不计分(use_rpt=False) | 无备注 | `calc({...})` | use_rpt=False, rpt 维度 missing=True, actual_weight=95 | P0 |
| TC-LSHING-054 | 关联交易 | 备注含 rpt_ratio 后启用 | notes/{code}.json 含 rpt_ratio | `calc({...})` | use_rpt=True, rpt 计入评分, actual_weight=100 | P0 |
| TC-LSHING-055 | 关联交易 | rpt_ratio<10 → 1.0 | note={rpt_ratio:5} | `calc({...})` | rpt 维度 raw=1.0 | P1 |
| TC-LSHING-056 | 关联交易 | rpt_ratio 10-29 → 0.5 | note={rpt_ratio:20} | `calc({...})` | rpt 维度 raw=0.5 | P1 |
| TC-LSHING-057 | 关联交易 | rpt_ratio>=30 → 0.0 | note={rpt_ratio:50} | `calc({...})` | rpt 维度 raw=0.0 | P1 |
| TC-LSHING-058 | 正常 | 不达标项加入 flags | raw<0.5 的维度 | `calc({pe:35,...})` | flags 列表含 "PE评分" | P1 |
| TC-LSHING-059 | 正常 | save_note/load_note 持久化 | notes/ 可写 | `save_note("600900",{rpt_ratio:5})` 后 `load_note("600900")` | 返回 {rpt_ratio:5} | P1 |
| TC-LSHING-060 | 正常 | grade_label 等级映射 | — | `grade_label(90)` / `grade_label(0)` | "🟢 极度安全" / "🔴 高风险" | P1 |

### 模块 6: 买卖信号 signals.py

| 用例ID | 类型 | 用例标题 | 前置条件 | 输入 | 预期结果 | 优先级 |
|--------|------|----------|----------|------|----------|--------|
| TC-SIGNAL-001 | 正常 | 6条件全满足 → BUY | — | ls=80, pe_pctl=20, pb_pctl=15, dy=4.0, above_ma20=True, red=False, th=70 | signal=buy, label=🟢可上车, met=6, total=6 | P0 |
| TC-SIGNAL-002 | 正常 | 部分满足 → WATCH | — | ls=80, pe_pctl=50(>30), 其余满足 | signal=watch, label=🟡观望, met<total | P0 |
| TC-SIGNAL-003 | 红线 | 红线触发 → AVOID | — | red_line_triggered=True | signal=avoid, label=🔴回避 | P0 |
| TC-SIGNAL-004 | 边界 | 李氏评分=阈值(=70) → 满足 | — | ls=70, th=70 | lishing_score 条件 ok=True (>=) | P0 |
| TC-SIGNAL-005 | 边界 | 李氏评分=阈值-1 → 不满足 | — | ls=69, th=70 | lishing_score 条件 ok=False | P0 |
| TC-SIGNAL-006 | 边界 | PE分位=29 → 满足(<30) | — | pe_pctl=29 | pe_pctl 条件 ok=True | P0 |
| TC-SIGNAL-007 | 边界 | PE分位=30 → 不满足 | — | pe_pctl=30 | pe_pctl 条件 ok=False (严格<) | P0 |
| TC-SIGNAL-008 | 边界 | PB分位=29 → 满足 | — | pb_pctl=29 | pb_pctl 条件 ok=True | P0 |
| TC-SIGNAL-009 | 边界 | 股息率=3.0 → 不满足(>) | — | dy=3.0 | div_yield 条件 ok=False (严格>) | P0 |
| TC-SIGNAL-010 | 边界 | 股息率=3.01 → 满足 | — | dy=3.01 | div_yield 条件 ok=True | P0 |
| TC-SIGNAL-011 | 异常 | pe_pctl=None → 不满足 | — | pe_pctl=None | pe_pctl 条件 ok=False | P1 |
| TC-SIGNAL-012 | 正常 | 条件修复建议 format_fix_summary | — | 有2条失败条件(非critical) | 返回含"**改善方向:**"和修复建议的文本 | P1 |
| TC-SIGNAL-013 | 正常 | format_fix_summary 全部满足 | — | 所有条件 ok=True | 返回 None | P2 |
| TC-SIGNAL-014 | 正常 | 卖出-红线触发 → 清仓 | — | sell_signal(ls=80, dy=4, red=True) | signal=clear, label=🔴清仓, reason含"红线触发" | P0 |
| TC-SIGNAL-015 | 正常 | 卖出-股息率<2% → 减仓 | — | sell_signal(ls=80, dy=1.5, red=False) | signal=reduce, label=🟡减仓, reason含"跌破2%" | P0 |
| TC-SIGNAL-016 | 边界 | 卖出-股息率=2.0 → 不减仓 | — | sell_signal(ls=80, dy=2.0, red=False, buy_score=80) | signal=hold (dy=2.0 不满足 <2.0) | P0 |
| TC-SIGNAL-017 | 正常 | 卖出-评分降幅>10% → 减仓(buy模式) | — | sell_signal(ls=70, dy=4, red=False, buy_score=85, mode="buy") | drop_pct=(85-70)/85=17.6%>10, signal=reduce | P0 |
| TC-SIGNAL-018 | 边界 | 卖出-评分降幅=10% → 不减仓 | — | sell_signal(ls=76.5, dy=4, red=False, buy_score=85, mode="buy") | drop_pct=10%, 不满足>10, signal=hold | P0 |
| TC-SIGNAL-019 | 正常 | 卖出-评分稳定 → 持有 | — | sell_signal(ls=80, dy=4, red=False, buy_score=82, mode="buy") | drop<10%, signal=hold, label=🟢持有 | P0 |
| TC-SIGNAL-020 | 正常 | 卖出-high_250d模式 | — | sell_signal(ls=70, dy=4, red=False, high_250d=90, mode="high_250d") | drop=(90-70)/90=22%>10, signal=reduce | P0 |
| TC-SIGNAL-021 | 异常 | 卖出-current_score=None → 持有 | — | sell_signal(None, 4, False) | signal=hold, reason="数据不足，默认持有" | P1 |
| TC-SIGNAL-022 | 异常 | 卖出-baseline=None → 持有 | — | sell_signal(ls=80, dy=4, red=False, buy_score=None) | signal=hold | P1 |
| TC-SIGNAL-023 | 正常 | save_score_snapshot 记录评分 | notes/ 可写 | `save_score_snapshot("600900", 82)` | _score_history.json 含 {600900:[{date:今天, score:82}]} | P1 |
| TC-SIGNAL-024 | 正常 | 同日重复记录覆盖 | 当天已有记录 | `save_score_snapshot("600900", 85)` (当天已有82) | 覆盖为85，不新增重复条目 | P1 |
| TC-SIGNAL-025 | 正常 | get_high_250d 取最高分 | 历史含多天记录 | `get_high_250d("600900")` | 返回历史最大 score | P1 |
| TC-SIGNAL-026 | 正常 | get_high_250d 无历史 → None | 无记录 | `get_high_250d("999999")` | 返回 None | P1 |
| TC-SIGNAL-027 | 正常 | set_buy_score/get_buy_score | notes/ 可写 | `set_buy_score("600900", 80)` 后 `get_buy_score("600900")` | 返回 80，notes/600900.json 含 buy_score 和 buy_date | P1 |
| TC-SIGNAL-028 | 边界 | 评分历史超365天截断 | history > 365条 | `save_score_snapshot` 多次 | history[code] 仅保留最近365条 | P2 |

### 模块 7: 市场诊断 regime.py

| 用例ID | 类型 | 用例标题 | 前置条件 | 输入 | 预期结果 | 优先级 |
|--------|------|----------|----------|------|----------|--------|
| TC-REGIME-001 | 正常 | 低估区间(composite<30) | — | pe_pctls={c1:20,c2:25}, pb_pctls={c1:15,c2:20} | regime=undervalued, threshold_adjust=-5, label=🟢低估 | P0 |
| TC-REGIME-002 | 正常 | 中性区间(30<=composite<70) | — | pe_pctls={c1:50}, pb_pctls={c1:50} | regime=neutral, threshold_adjust=0, label=🟡中性 | P0 |
| TC-REGIME-003 | 正常 | 高估区间(composite>=70) | — | pe_pctls={c1:80,c2:85}, pb_pctls={c1:75,c2:80} | regime=overvalued, threshold_adjust=+5, label=🔴高估 | P0 |
| TC-REGIME-004 | 边界 | composite=30 边界(中性) | — | pe_pctls median=30, pb median=30 → composite=30 | regime=neutral (30不满足<30) | P0 |
| TC-REGIME-005 | 边界 | composite=70 边界(高估) | — | composite=70 | regime=overvalued (70不满足<70) | P0 |
| TC-REGIME-006 | 正常 | PE中位>80 触发风险警告 | — | pe_median=85 | risk_warning 含"PE中位数超过80%" | P1 |
| TC-REGIME-007 | 正常 | PB中位>80 触发风险警告 | — | pb_median=85 | risk_warning 含"PB中位数超过80%" | P1 |
| TC-REGIME-008 | 异常 | 所有分位为None → 默认50 | — | pe_pctls={c1:None}, pb_pctls={c1:None} | pe_median=50, pb_median=50, composite=50, regime=neutral | P1 |
| TC-REGIME-009 | 正常 | composite = pe_median×0.6 + pb_median×0.4 | — | pe_median=40, pb_median=60 | composite=24+24=48 → neutral | P1 |
| TC-REGIME-010 | 正常 | 阈值调整影响买入判断 | — | undervalued时 threshold_adjust=-5 | Tab5 买入阈值 = 用户输入 - 5 | P0 |

### 模块 8: 成本分层 cost_tier.py

| 用例ID | 类型 | 用例标题 | 前置条件 | 输入 | 预期结果 | 优先级 |
|--------|------|----------|----------|------|----------|--------|
| TC-COST-001 | 正常 | 黄金档(ratio>=1.2) | — | batch_cost=10, dividend=1.0, current_price=15 | cost_dy=10%, current_dy=6.67%, ratio=1.5→黄金 | P0 |
| TC-COST-002 | 正常 | 白银档(0.9<=ratio<1.2) | — | batch_cost=10, dividend=1.0, current_price=11 | cost_dy=10%, current_dy=9.09%, ratio=1.1→白银 | P0 |
| TC-COST-003 | 正常 | 警戒档(ratio<0.9) | — | batch_cost=15, dividend=1.0, current_price=12 | cost_dy=6.67%, current_dy=8.33%, ratio=0.8→警戒 | P0 |
| TC-COST-004 | 边界 | ratio=1.2 恰好黄金 | — | 构造 ratio=1.2 | tier=黄金 (>=1.2) | P0 |
| TC-COST-005 | 边界 | ratio=0.9 恰好白银 | — | 构造 ratio=0.9 | tier=白银 (>=0.9) | P0 |
| TC-COST-006 | 异常 | current_price=0 → 数据不足 | — | calc_batch_tier(10, 1.0, 0) | tier=数据不足, ratio=None | P0 |
| TC-COST-007 | 异常 | dividend=0 → 数据不足 | — | calc_batch_tier(10, 0, 15) | tier=数据不足, ratio=None | P0 |
| TC-COST-008 | 异常 | batch_cost=0 → 数据不足 | — | calc_batch_tier(0, 1.0, 15) | tier=数据不足 | P1 |
| TC-COST-009 | 异常 | current_dy=0 → ratio=999 | — | current_price极大使current_dy→0 | ratio=999 (避免除零) | P1 |
| TC-COST-010 | 正常 | calc_overall_tier 加权均价 | 多批次 | batch1:100股@10, batch2:200股@15 | avg_cost=(1000+3000)/300=13.33 | P0 |
| TC-COST-011 | 正常 | calc_overall_tier 无批次 → 数据不足 | stock 无 batches | `calc_overall_tier("999", 10)` | tier=数据不足, batches=[] | P1 |
| TC-COST-012 | 正常 | add_batch 添加批次 | notes/ 可写 | `add_batch("600900","2025-01-15",100,10,"建仓")` | cost_tiers.json 含该批次 | P1 |
| TC-COST-013 | 正常 | delete_batch 删除批次 | 有2个批次 | `delete_batch("600900", 0)` | 删除第0个，保留第1个 | P1 |
| TC-COST-014 | 异常 | delete_batch 越界索引 | 有1个批次 | `delete_batch("600900", 5)` | 不崩溃，不删除（0<=idx<len检查） | P1 |
| TC-COST-015 | 正常 | update_dividend 更新分红 | — | `update_dividend("600900", 1.5)` | cost_tiers.json 中 dividend_per_share=1.5 | P1 |
| TC-COST-016 | 正常 | set_dividend_source 切换来源 | — | `set_dividend_source("600900","manual")` | dividend_source=manual | P2 |
| TC-COST-017 | 正常 | get_dividend_source 默认auto | 无设置 | `get_dividend_source("600900")` | 返回 "auto" | P2 |

### 模块 9: 回测 backtest.py

| 用例ID | 类型 | 用例标题 | 前置条件 | 输入 | 预期结果 | 优先级 |
|--------|------|----------|----------|------|----------|--------| 
| TC-BT-001 | 正常 | backtest_portfolio 标准回测 | 历史数据可拉取 | codes=["600900"], weights={600900:100}, years=5 | 返回 dict 含 nav, metrics(8项), monthly_returns, drawdown_attr | P0 |
| TC-BT-002 | 正常 | 三场景回测 bull/base/bear | fins 数据正常 | `backtest_scenarios(PORT_A, PORT_HK, fins, 5)` | 返回 {bull:..., base:..., bear:...}，bull含高ROE加权 | P0 |
| TC-BT-003 | 正常 | bull场景高ROE加权(×1.2) | roe>10 | `backtest_scenarios` | bull_weights 中 roe>10 的股票 ratio×1.2 | P0 |
| TC-BT-004 | 正常 | bear场景剔除低ROE | roe<=5 | `backtest_scenarios` | bear_weights 中 roe<=5 的股票权重=0 | P0 |
| TC-BT-005 | 正常 | 月度收益扣双边成本 | — | 标准回测 | monthly_ret 每月减去 cost×2 (=0.2%) | P1 |
| TC-BT-006 | 正常 | 年化换手率 = cost×2×12 | — | cost=0.001 | turnover = 0.001×2×12 = 0.024 → 2.4% | P1 |
| TC-BT-007 | 正常 | 扣费后年化 = annual - turnover×cost | — | 标准回测 | annual_return_net = annual_return - 0.024×0.001 | P1 |
| TC-BT-008 | 异常 | 无历史数据返回 None | API 全部失败 | `backtest_portfolio(["999999"], {999999:100})` | 返回 None | P0 |
| TC-BT-009 | 异常 | 权重总和=0 返回 None | — | weights 全为0 | 返回 None | P1 |
| TC-BT-010 | 正常 | 最大回撤计算 | — | 标准回测 | max_drawdown <= 0 (负数)，为历史最大回撤百分比 | P0 |
| TC-BT-011 | 正常 | 夏普比率 = (年化-rf)/年化波动 | — | 标准回测 | sharpe = (annual_return-0.025)/annual_vol | P0 |
| TC-BT-012 | 异常 | 年化波动=0 时夏普=0 | — | returns 全为0 | sharpe=0 (避免除零) | P1 |
| TC-BT-013 | 正常 | EM失败回退腾讯K线 | push2不可达 | `fetch_history("600900",5)` | 腾讯K线回退成功，df 含 date/close | P0 |
| TC-BT-014 | 正常 | 腾讯K线也失败返回空df | 所有源失败 | `fetch_history("999999",5)` | 返回空 DataFrame | P1 |
| TC-BT-015 | 正常 | 港股历史拉取 | — | `fetch_history("00002",5,mkt="H")` | 用 ak.stock_hk_hist，返回 df | P1 |
| TC-BT-016 | 正常 | fetch_histories 并行批量 | 8只股票 | `fetch_histories(["600900",...],5)` | 返回 dict，并行执行 | P1 |
| TC-BT-017 | 正常 | 回撤归因 dd_attr | — | 标准回测 | drawdown_attr 含回撤期各股票贡献，权重>0的才有值 | P2 |
| TC-BT-018 | 边界 | years=0 时不崩溃 | — | years=0 | annual_return=0 (if years>0 else 0 分支) | P1 |

### 模块 10: 组合优化 optimizer.py

| 用例ID | 类型 | 用例标题 | 前置条件 | 输入 | 预期结果 | 优先级 |
|--------|------|----------|----------|------|----------|--------|
| TC-OPT-001 | 正常 | max_sharpe 优化 | returns 正常 | 3只股票 returns | 返回权重数组，sum=1，每只∈[0,0.3] | P0 |
| TC-OPT-002 | 正常 | min_vol 优化 | returns 正常 | 3只股票 | 返回最小波动率权重，sum=1 | P0 |
| TC-OPT-003 | 正常 | risk_parity 优化 | returns 正常 | 3只股票 | 各只风险贡献接近 1/n | P0 |
| TC-OPT-004 | 边界 | 单只上限 30% 约束 | — | 2只股票 | 每只权重 <= 0.3，因 sum=1 不可行时优化可能失败 | P0 |
| TC-OPT-005 | 异常 | returns 为空 | — | `optimize(pd.DataFrame(), [], ...)` | 返回 {optimal_weights:{}, frontier:[], confidence:{}} | P0 |
| TC-OPT-006 | 异常 | 股票数<2 | — | `optimize(returns, ["600900"], ...)` | 返回空结果 | P0 |
| TC-OPT-007 | 正常 | 有效前沿 20个点 | — | 标准输入 | efficient_frontier 含约20个 (vol%, ret%) 元组 | P1 |
| TC-OPT-008 | 正常 | Monte Carlo 置信区间 | — | n_sims=100 | confidence 含 {code: (low_2.5%, high_97.5%)} | P0 |
| TC-OPT-009 | 正常 | port_vol 计算波动率 | — | w=[0.5,0.5], S=2×2 | 返回 sqrt(w'Sw) > 0 | P1 |
| TC-OPT-010 | 异常 | risk_parity vol=0 时返回大值 | — | S 全零 | rc 返回 1e10 (避免除零) | P1 |
| TC-OPT-011 | 边界 | 权重过滤 <0.001 不显示 | — | 优化结果有极小权重 | optimal_weights 不含 w<0.001 的股票 | P1 |
| TC-OPT-012 | 正常 | 三种方法切换 | — | method="max_sharpe"/"min_vol"/"risk_parity" | 分别调用对应函数 | P1 |

### 模块 11: 压力测试 stress.py

| 用例ID | 类型 | 用例标题 | 前置条件 | 输入 | 预期结果 | 优先级 |
|--------|------|----------|----------|------|----------|--------|
| TC-STRESS-001 | 正常 | 单日暴跌 -10% | — | `single_day(["600900"], {600900:100}, [-0.10])` | impact=-10.0 (100%×-10%×100) | P0 |
| TC-STRESS-002 | 正常 | 单日暴跌 -20% | — | drops=[-0.20] | impact=-20.0 | P0 |
| TC-STRESS-003 | 正常 | 默认drops=[-0.10,-0.20] | — | drops=None | 返回2个场景 | P1 |
| TC-STRESS-004 | 正常 | 行业全灭 银行 | — | `industry_collapse({601398:20,...})` | 银行全灭 impact = -(银行股权重之和) | P0 |
| TC-STRESS-005 | 正常 | 行业全灭 能源 | — | weights含601088,600938 | 能源全灭 impact = -(两只权重和) | P1 |
| TC-STRESS-006 | 正常 | 相关性断裂 0.7→0.95 | returns 正常 | `correlation_break(codes, weights, returns, 0.95)` | stressed_vol > original_vol, increase>0 | P0 |
| TC-STRESS-007 | 异常 | returns 为空 | — | `correlation_break([], {}, pd.DataFrame())` | 返回 {error:"无数据"} | P1 |
| TC-STRESS-008 | 正常 | Monte Carlo 极端模拟 | returns 正常 | `monte_carlo_stress(returns, weights, n_sims=100)` | 返回 1pct/5pct/10pct/mean/median，1pct最负 | P0 |
| TC-STRESS-009 | 异常 | Monte Carlo returns空 | — | `monte_carlo_stress(pd.DataFrame(), {})` | 返回 {error:"无数据"} | P1 |
| TC-STRESS-010 | 正常 | Monte Carlo 波动率放大3倍 | — | 标准输入 | S = returns.cov()×9 (3²放大) | P2 |
| TC-STRESS-011 | 正常 | INDUSTRY_MAP 含4行业 | — | 检查常量 | 银行/能源/电信/电力 4个映射 | P2 |

### 模块 12: 预警系统 alerts.py

| 用例ID | 类型 | 用例标题 | 前置条件 | 输入 | 预期结果 | 优先级 |
|--------|------|----------|----------|------|----------|--------|
| TC-ALERT-001 | 正常 | check_alerts 股价跌幅触发 | chg=-10, threshold=8 | `check_alerts(...)` | alerts 含 type="股价跌幅" | P0 |
| TC-ALERT-002 | 正常 | check_alerts Kelly变化触发 | |ck-pk|=6, threshold=5 | `check_alerts(...)` | alerts 含 type="Kelly变化" | P0 |
| TC-ALERT-003 | 正常 | check_alerts 评分下降触发 | ps-cs=15, threshold=10 | `check_alerts(...)` | alerts 含 type="评分下降" | P0 |
| TC-ALERT-004 | 边界 | 股价跌幅=阈值不触发 | chg=-8, threshold=8 | `check_alerts(...)` | 不触发 (chg < -threshold 严格小于) | P1 |
| TC-ALERT-005 | 正常 | 无异常返回空列表 | 所有指标正常 | `check_alerts(...)` | 返回 [] | P1 |
| TC-ALERT-006 | 正常 | 邮件发送(配置正确) | SMTP 配置有效 | `sender.send("测试","内容","email")` | 返回 True | P1 |
| TC-ALERT-007 | 异常 | 邮件发送(配置缺失) | 无 SMTP 配置 | `sender.send("测试","内容","email")` | 返回 False，不崩溃 | P0 |
| TC-ALERT-008 | 异常 | 企业微信 webhook 为空 | 无配置 | `sender.send("测试","内容","wechat")` | 返回 False | P1 |
| TC-ALERT-009 | 异常 | Telegram token 为空 | 无配置 | `sender.send("测试","内容","telegram")` | 返回 False | P1 |
| TC-ALERT-010 | 正常 | update_cfg 保存配置 | data/ 可写 | `sender.update_cfg(smtp_server="smtp.qq.com")` | alert_config.json 含该配置 | P1 |
| TC-ALERT-011 | 正常 | log_alerts 记录历史 | alerts 非空 | `log_alerts([{...}])` | alerts_log.csv 含该记录+timestamp | P1 |
| TC-ALERT-012 | 正常 | log_alerts 空列表不写 | alerts=[] | `log_alerts([])` | 不创建/修改文件 | P2 |
| TC-ALERT-013 | 正常 | load_alert_history 读取 | 日志存在 | `load_alert_history()` | 返回 DataFrame | P2 |
| TC-ALERT-014 | 异常 | load_alert_history 文件损坏 | csv 损坏 | `load_alert_history()` | 返回空 DataFrame，不崩溃 | P1 |
| TC-ALERT-015 | 正常 | 股息率变动预警逻辑 | prev_dy=5, cur_dy=4, threshold=0.5 | `check_alerts(...)` | dy_chg=20% > 0.5×10=5, 触发 | P1 |

### 模块 13: UI 各 Tab (Streamlit)

| 用例ID | 类型 | 用例标题 | 前置条件 | 操作步骤 | 预期结果 | 优先级 |
|--------|------|----------|----------|----------|----------|--------|
| TC-UI-001 | 正常 | 首次加载自动拉取数据 | 无缓存 | 访问 localhost:8501 | 显示 spinner"拉取持仓数据..."，完成后 Tab1 显示持仓表 | P0 |
| TC-UI-002 | 正常 | Tab1 持仓看板分组显示 | 数据已加载 | 查看 Tab1 | A股持仓/港股持仓/四大行轮动 三组分别显示表格 | P0 |
| TC-UI-003 | 正常 | Tab1 李氏评分颜色标签 | 数据已加载 | 查看李氏评分列 | ≥80 绿/60-79 黄/<60 红，含 emoji 标签 | P1 |
| TC-UI-004 | 正常 | Tab1 异常数据标红 | 有 _flags 非空 | 查看异常行 | 显示 ⚠ 警示文案+提示查看 Tab3 | P1 |
| TC-UI-005 | 正常 | Tab1 成本档位列 | 成本数据已录入 | 查看档位列 | 显示 🟡黄金/⚪白银/🔴警戒/- | P1 |
| TC-UI-006 | 正常 | 刷新数据按钮 | 数据已加载 | 点击"🔄 刷新数据" | 重新拉取 spot 数据(spots TTL 1h) | P0 |
| TC-UI-007 | 正常 | 强制刷新财务数据 | — | 点击"📅 强制刷新财务数据" | clear_cache("fin") + clear_cache("div") 后重新拉取 | P0 |
| TC-UI-008 | 正常 | Tab2 调仓报告 | 数据已加载 | 查看 Tab2 | Kelly 调仓路线显示加仓▲/减仓▼，四大行轮动显示最优 | P0 |
| TC-UI-009 | 正常 | Tab2 持仓体检 | 数据已加载 | 查看右侧 | 每只股票显示 ✅正常 或 ❌异常项(股息率/PE/回撤/评分) | P1 |
| TC-UI-010 | 正常 | Tab3 李氏选股清单展开 | 数据已加载 | 点击 expander | 显示各维度评分条+权重+不达标项+备注区 | P0 |
| TC-UI-011 | 正常 | Tab3 保存备注 | 输入 rpt_ratio 和备注 | 点击"保存备注" | notes/{code}.json 更新，显示"已保存" | P1 |
| TC-UI-012 | 正常 | Tab3 关联交易不计分提示 | 无 rpt 备注 | 查看 | 显示"关联交易: 数据不可获取, 不计分" | P1 |
| TC-UI-013 | 正常 | Tab4 红利低波轮动筛选 | — | 设置股息率/波动率/成交额/年限，点击"开始筛选" | 显示 spinner"全市场扫描"，完成后显示候选表 | P0 |
| TC-UI-014 | 异常 | Tab4 筛选无结果 | 阈值过严 | 极高股息率+极低波动率 | 显示"未找到符合条件的股票" | P1 |
| TC-UI-015 | 正常 | Tab4 与持仓对比 | 筛选完成 | 查看对比区 | 已持仓✅/可关注🆕 标签 | P1 |
| TC-UI-016 | 正常 | Tab5 买卖信号仪表盘 | 数据已加载 | 查看 Tab5 | 市场诊断框+每只股票 expander(买入/卖出条件) | P0 |
| TC-UI-017 | 正常 | Tab5 市场诊断显示 | 数据已加载 | 查看顶部 | 显示 regime label + PE/PB 中位 + 阈值调整 ±N | P0 |
| TC-UI-018 | 正常 | Tab5 阈值调整影响买入 | regime=undervalued | 查看买入条件 | 买入阈值 = 用户输入 - 5 (放宽) | P0 |
| TC-UI-019 | 正常 | Tab5 记录买入评分 | — | 点击"📍 记录买入评分" | notes/{code}.json 写入 buy_score，显示"已记录" | P1 |
| TC-UI-020 | 正常 | Tab5 修复建议显示 | 有不达标条件 | 查看 expander | 显示 format_fix_summary 的改善方向 | P1 |
| TC-UI-021 | 正常 | Tab5 卖出基准切换 | — | 切换"买入时"/"250日高点" | 卖出信号按对应模式计算 | P0 |
| TC-UI-022 | 正常 | Tab6 回测运行 | — | 设置年数+上限，点击"运行回测" | spinner"回测中"，显示三场景指标+净值曲线 | P0 |
| TC-UI-023 | 正常 | Tab6 三场景指标 | 回测完成 | 查看 | 乐观/基准/悲观 各显示 5 个 metric(总收益/年化/夏普/回撤/胜率) | P0 |
| TC-UI-024 | 正常 | Tab7 组合优化 | A股>=2只 | 选择优化目标 | 显示最优权重+95%置信区间表 | P0 |
| TC-UI-025 | 正常 | Tab7 压力测试 | 数据已加载 | 查看 | 单日暴跌/行业全灭/相关性断裂/Monte Carlo 四项结果 | P0 |
| TC-UI-026 | 异常 | Tab7 A股不足2只 | 仅1只A股 | 查看 | 显示"A股持仓不足2只, 无法优化" | P1 |
| TC-UI-027 | 正常 | Tab8 预警配置表单 | — | 填写 SMTP + 阈值，点击"保存配置" | 显示"✅ 配置已保存" | P1 |
| TC-UI-028 | 正常 | Tab8 发送测试邮件 | 配置已保存 | 点击"发送测试邮件" | 成功显示✅/失败显示❌ | P1 |
| TC-UI-029 | 正常 | Tab8 预警历史 | 有历史记录 | 查看 | 显示最近50条预警 DataFrame | P2 |
| TC-UI-030 | 正常 | Tab9 REIT/ETF 查询 | — | 输入代码，点击"查询" | 返回 df 含 code/name/price/chg/nav/premium/div_yield | P1 |
| TC-UI-031 | 正常 | Tab9 加载全部REITs | — | 点击"加载全部REITs" | 加载全市场 REITs 数据 | P1 |
| TC-UI-032 | 正常 | Tab9 折溢价筛选 | 数据已加载 | 选择"仅溢价"/"仅折价" | 表格只显示对应数据 | P2 |
| TC-UI-033 | 正常 | Tab10 成本分层显示 | 有成本数据 | 查看 | 每只标的显示整体档位+均价+成本息率+比值+建议 | P0 |
| TC-UI-034 | 正常 | Tab10 添加批次 | — | 输入日期/股数/成本，点击"添加批次" | cost_tiers.json 更新，显示"已添加" | P1 |
| TC-UI-035 | 正常 | Tab10 删除批次 | 有批次 | 输入序号，点击"删除批次" | 对应批次删除 | P1 |
| TC-UI-036 | 正常 | Tab10 整体分配建议 | 有多只成本数据 | 查看底部 | 黄金/白银/警戒 三栏分类显示 | P1 |
| TC-UI-037 | 正常 | Tab10 初始化成本 | 无成本数据 | 点击"初始化" | 创建空批次记录 | P2 |
| TC-UI-038 | 异常 | API 全部失败无缓存 | 网络断开+无缓存 | 访问 Tab1 | 显示 ⚠ "无法获取市场数据" 警告 | P0 |
| TC-UI-039 | 正常 | 港股持仓股息率计算 | 港股数据已加载 | 查看 Tab1 港股组 | dy = div/price×100，div>0 时显示 | P1 |
| TC-UI-040 | 正常 | 市值回退计算 | EM mcap 缺失 | mcap_val<=0 且有 total_shares | mcap = price×total_shares/1e8 | P1 |

### 模块 14: Docker 部署

| 用例ID | 类型 | 用例标题 | 前置条件 | 操作 | 预期结果 | 优先级 |
|--------|------|----------|----------|------|----------|--------|
| TC-DOCKER-001 | 正常 | docker compose 构建启动 | Docker 已安装 | `docker compose up -d --build` | 容器构建成功并启动，端口 8501 可访问 | P0 |
| TC-DOCKER-002 | 正常 | 容器健康运行 | 容器已启动 | `docker ps` | 容器状态 Up，restart=unless-stopped | P0 |
| TC-DOCKER-003 | 正常 | 浏览器访问 | 容器运行 | 访问 http://localhost:8501 | Streamlit 页面正常加载 | P0 |
| TC-DOCKER-004 | 正常 | data 卷持久化 | 容器运行后重启 | 重启容器 | data/ 目录数据保留 | P0 |
| TC-DOCKER-005 | 正常 | notes 卷持久化 | 容器运行后重启 | 重启容器 | notes/ 目录数据保留 | P0 |
| TC-DOCKER-006 | 正常 | 时区 Asia/Shanghai | 容器运行 | `docker exec ... date` | 时间为北京时间 (+0800) | P1 |
| TC-DOCKER-007 | 正常 | 容器停止与重启 | 容器运行 | `docker compose down` 后 `up -d` | 数据不丢失，服务恢复 | P1 |
| TC-DOCKER-008 | 正常 | network_mode host | 容器运行 | `docker inspect` | NetworkMode=host (NAS内网直接访问) | P2 |
| TC-DOCKER-009 | 异常 | pip 镜像源可用 | Dockerfile 用清华源 | 构建时 | pip install 从 pypi.tuna.tsinghua.edu.cn 拉取成功 | P1 |
| TC-DOCKER-010 | 正常 | 容器名 dividend-portfolio | — | `docker ps` | container_name=dividend-portfolio | P2 |

---

## 三、风险关注点

| # | 风险项 | 影响 | 缓解措施 |
|---|--------|------|----------|
| R1 | akshare API 不稳定/限流 | 数据拉取失败，页面空白 | 已有多源降级(EM→新浪→腾讯)+缓存+重试，需验证降级链路 |
| R2 | NAS 环境 push2.eastmoney 被阻断 | A股行情 PE/PB 缺失 | 已有腾讯报价回退+THS财务数据计算PE/PB，需验证回退路径 |
| R3 | 幸存者偏差未声明 | 回测结果虚高 | Tab6 底部已有声明文案，需验证显示 |
| R4 | Kelly p 为主观估计非统计概率 | 仓位建议可能误导 | Tab8 假设声明已标注，需验证声明完整 |
| R5 | 并发请求过多拖垮 NAS | API 超时/限流 | Semaphore(3) 限流+指数退避，需验证并发控制 |
| R6 | 缓存 TTL 过期但 API 不可用 | 数据缺失 | allow_expired 降级读取机制，需验证 |
| R7 | 压力测试 Monte Carlo 随机性 | 每次结果不同 | 非确定性测试，需固定随机种子或只验证范围 |
| R8 | 腾讯行情 GBK 解码 | 中文乱码 | _fetch_tx_quotes 用 gbk 解码，需验证中文名称 |
| R9 | 除零风险(波动率/夏普/比值) | 程序崩溃 | 已有 if vol>0 / if annual_vol>0 等保护，需验证边界 |
| R10 | 李氏评分权重为经验值未回测 | 评分不代表真实质量 | Tab8 已声明，需验证 |

---

## 四、回归测试范围

每次发版需回归的核心路径（优先级从高到低）：

1. **数据层全链路**: cache TTL → market 多源降级 → fundamental 财务拉取
2. **计算引擎三件套**: kelly.run → lishing.calc → signals.buy_signal/sell_signal
3. **市场诊断联动**: regime.detect_regime → Tab5 阈值调整 → 买入信号
4. **成本分层**: cost_tier.calc_overall_tier → Tab10 显示
5. **回测三场景**: backtest_scenarios → Tab6 指标+净值曲线
6. **组合优化+压力测试**: optimizer.optimize → stress 四场景 → Tab7
7. **预警系统**: alerts.check_alerts → Tab8 配置+历史
8. **UI 烟雾测试**: 10 个 Tab 均能加载不崩溃

---

## 五、测试执行清单

```
□ 环境准备: docker compose up -d --build
□ 烟雾测试: 10 个 Tab 逐一打开不崩溃 (TC-UI-001~040 抽查)
□ 单元测试: 16 个模块核心函数 (TC-CACHE~ALERT)
□ 边界测试: Kelly 钳位 / 李氏红线 / 信号阈值边界
□ 异常测试: API 失败降级 / 除零保护 / 空数据处理
□ 回归测试: 上述第四节核心路径
□ 部署测试: Docker 构建/启动/持久化/重启
□ 验收: 对照验收标准(P0 全过 + P1 无阻断)
```

---

## 六、备注

- 本文档基于 PLAN.md(v4.0 设计文档) + 实际代码(16个模块文件)编写
- 实际代码已超出 PLAN.md 设计：新增 regime.py(市场诊断)、cost_tier.py(成本分层)、fund.py(REIT/ETF)、health_monitor.py
- Tab 编号从 PLAN.md 的 8 个扩展为实际的 10 个
- Kelly 引擎新增波动率校准(vol_adjust)，PLAN.md 中无此逻辑
- 李氏评分实际用 calc() 函数(非 PLAN.md 的 calc_score)，评分档位略有调整(≥80绿/60-79黄/<60红)
- 预警系统 alerts.py 的 check_alerts 中 lishing_scores 用 "total_score" 键，而实际 lishing.calc 返回 "total" 键 — **疑似 Bug，需研发确认**

> 提醒：可将本测试用例文档上传到项目资料库，并创建测试执行事项分配给测试同学。如发现 Bug，按标准 Bug 单格式反馈并关联对应用例 ID。

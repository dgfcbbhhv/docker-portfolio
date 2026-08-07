# 领域词汇表 (CONTEXT.md)

红利持仓分析系统的统一领域语言。新增描述/命名模块时沿用下列词汇；
出现好切缝的概念时在此登记。架构术语（模块/接口/深度/缝/适配器）
遵循 codebase-design 规范。

## 投资分层（Layer）

- **A / HK / US / REIT 四层** — 按市场与资产类别划分的持仓层，`config.json`
  的 `portfolio.<layer>` 为该层持仓代码与目标权重（`ratio`，合计约 100%）。
- **层内回撤上限 `max_drawdown`**、**层内仓位上限 `invested_cap`**（单位 %）。
- 各层有唯一 `sequence` 排序。

## 仓位与成本档位（cost_tier）

- **成本批次 batch** — 同一股票的建仓记录（日期/股数/成本）。
- **档位 tier** — 按成本股息率 vs 当前股息率之比 `ratio` 划分：
  `黄金(≥1.2)/白银(≥0.9)/警戒(其余)`。`calc_overall_tier` 是档位判定入口，
  支持注入 `stock` 以隔离全局配置（测试用）。

## 仓位与评分

- **Kelly 仓位 `kelly_final`** — 波动率调整后的目标仓位（%）。
- **当前占比 `ratio_norm`** — 当前实际仓位占比。
- **加仓空间 `gap`** = 目标 - 当前；`add_gap` 为加仓阈值。
- **李氏评分 LiShing**（`lishing.py`）— 多维度综合评分（0-100），含 `lishing_flags`
  红旗标记；US 成长股路径下 `lishing_flags` 是因子拆分而非红旗。
- **分类结果**：`可建仓/可加仓/可关注/不推送`（`daily_picks._classify`），
  由买入线 `lishing_buy`、关注线 `lishing_watch` 决定。

## 风险（risk / stress / layer_risk）

- **占位波动率 `PLACEHOLDER_VOL=20.0`** — 非真实数据；HK/US/REIT 占位波动率
  不得用于蒙特卡洛回撤，判定为「数据不足」。
- **蒙特卡洛 p95 回撤** — 层回撤验收阈值；`monte_carlo` 依赖真实波动率。
- 基础计算原语位于 `modules/risk.py`，`stress.py`（黑天鹅）与
  `layer_risk.py`（分层回撤验收）为其薄适配器。

## 预警（alerts）

- 渠道：email/wechat/telegram/webhook/bark。密钥经**环境变量注入**
  （`SMTP_*`/`TG_*`/`WECHAT_WEBHOOK` 等），`_ENV_MAP` 定义映射，不落盘。

## 调度 / 推送（scheduler / daily_picks）

- 交易时段 `is_trading_hours()` — 决定缓存 TTL（300s/1800s）。
- 每日选股推送按 A 股（16:30）与美股（06:30）分别触发。

## 数据缝（modules/data/*）

- `market.py` / `fundamental.py` / `fund.py` / `market_us.py` 为行情与基本面
  适配器，m多个真实实现（东财/腾讯/akshare/longbridge/yfinance/baostock）→ 真缝。
- `cache.py` — 本地行情/财务缓存层。
- `app_cache.py` — Streamlit `@st.cache_data` 包装，缓存策略住在数据缝。

## 未决记录（待清理）

- 根目录 `format_aihot.py`/`parse_aihot.py`/`check_div.py` 为一次性本地脚本
  （硬编码绝对路径），不属于应用领域。
- 架构重构（如 app.py 18-tab 拆分）应在这里先记 ADR 再执行。
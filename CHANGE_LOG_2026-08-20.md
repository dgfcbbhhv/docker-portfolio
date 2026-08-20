# 正金字塔加仓层级 + 芒格清单 Q0 — 变更记录

> 记录日期：2026-08-20
> 项目：`/vol1/1000/docker-portfolio/`（NAS xihe）
> 背景：视频《加仓的学问：金字塔加仓和补仓陷阱》转写后，将"正金字塔 + 风控"翻译为系统可执行规则。

## 一、设计决策

- **不照搬"只在盈利时加仓"**：系统左侧红利逻辑（超跌买入是核心收益来源，README）与视频右侧趋势逻辑是不同流派。用系统已有门禁（评分≥70 / 新鲜度正常 / 成本档位非警戒）替换"盈利门"。
- **三块增量，分期落地**：本期落地 Q0 + 加仓层级；单笔风险预算（1.5%×总资产÷止损距离）等用户确定止损距离政策后再排期。

## 二、已实施改动

### 1. 芒格清单 Q0 参考点重置（`modules/munger_checklist.py`）
- `build_checklist` 新增可选参数 `tier`，返回新增 `q0` 字段。
- Q0 判定："若我此刻持现金，这个价格还会买吗？"——由 评分≥70 + 逻辑未破 + 档位非警戒 联合推出。
- 当 Q0=不补仓 且 q6 动作是 add/hold 时，q6 reason 追加"参考点重置：若持现金不再愿买入，不补仓"。
- 测试：`tests/test_munger_checklist.py` 新增 5 例。

### 2. 正金字塔加仓层级（新增 `modules/add_ladder.py`）
- `plan_add_ladder()` 纯函数：gap = Kelly目标 − 当前仓位，按递减权重 `[0.5,0.3,0.2]` 拆 max_steps 层。
- 硬门禁：档位警戒 / 评分≤0或<买入线 / 已达单票上限 / gap<min_gap → 整层持有。
- 逐层累计不得突破 maxpos；`add_count` 已由 cost_tiers.json 成本批次回填。
- 集成 `modules/position_manager.py`：`build_position_plan` 的"可加仓"分支追加 ladder 校验，结果取小者；entry 新增 `add_step/add_pct/ladder_alloc/ladder_cap_hit`。
- 配置：`config.json` + `modules/config.py` 新增 `add_ladder.max_steps/tiers/min_gap`（默认兜底，缺省不影响旧行为）。
- 测试：`tests/test_add_ladder.py` 新增 10 例。

### 3. UI 展示
- `render/t15.py`（仓位管理）：新增"金字塔"列，显示 `第N层 +x%（+a>+b>+c）`。
- `render/t16.py`（芒格清单）：汇总表新增 Q0 列；详情展开新增 Q0 问句；传入 tier。

## 三、验证结果

- 新增测试全绿：`test_add_ladder.py`（10）+ `test_munger_checklist.py`（含 5 例 Q0）+ `test_position_manager.py`（5）。
- 全量 `pytest tests/`：245 passed；2 failed + 9 errors 为**既有问题**（`test_daily_picks` 2 例、`test_actual_holdings` 9 例，stash 验证与本次改动无关）。
- 功能冒烟：600900（评分82/黄金）→ 可加仓，第1层 +10%（alloc [10,6,4]）；601318（评分45）→ 持有；Q0 高评分为"可补仓"、低评分"不补仓"。

## 四、遗留事项

- **单笔风险预算**（风险 = 总资产×1.5% ÷ 止损距离）待实施：需先定止损距离政策（固定 10%？ATR？按层回撤线？）。
- **加仓台账**已落地：`add_count` = `notes/cost_tiers.json` 成本批次数（每批=一次加仓），正金字塔按真实层级放量；UI 显示"已加 N 次"。
- 既有失败测试（test_daily_picks / test_actual_holdings）未在本期处理，与本改动无关。
## 五、Phase 3：单笔风险预算（2026-08-20 追加）

**决策**：用户设定止损距离 5%-10%，实现取保守侧 5%（风险最大距离）。

### 改动
- 新增 ：（单笔上限=总资产×risk_pct÷止损距离）+ （与 ladder 取小者）。
-  /  新增  段（）。
- ： 新增  参数，提供时对可加仓标的计算  并封顶 ；entry 新增 。
- ：新增单笔风险上限列。
- 测试：（7 例）+  增 2 例。

**验证**：pytest 265 passed；100 万资产 → 每笔最多亏 1.5 万（止损 5%）→ 本笔封顶 30 万/30%。

**遗留**： 需由调用方传入（t15 当前未传，UI 暂不显示风险上限，仅引擎/测试生效）。


## 五、Phase 3：单笔风险预算（2026-08-20 追加）

**决策**：用户设定止损距离 5%-10%，实现取保守侧 5%（风险最大距离）。

### 改动
- 新增 modules/risk_budget.py：risk_budget_per_add（单笔上限=总资产x risk_pct 除 stop_loss）+ effective_add_value（与 ladder 取小者）。
- config.json / config.py 新增 risk_budget 段（enabled/risk_pct=1.5/stop_loss_pct=5）。
- position_manager.py：build_position_plan 新增 capital 参数，提供时对可加仓标的计算 risk_cap_pct 并封顶 add_pct；entry 新增 risk_budget/risk_cap_pct。
- render/t15.py：新增"单笔风险上限"列。
- 测试：tests/test_risk_budget.py（7 例）+ test_position_manager 增 2 例。

**验证**：pytest 265 passed；100 万资产 → 每笔最多亏 1.5 万（止损 5%）→ 本笔封顶 30 万/30%。

**遗留（已跟进）**：capital 需由调用方传入。t15 已接入：优先 holdings.get_actual_holdings() 的 capital（broker 源为 0 时回退 paper portfolio / VIRTUAL_CAPITAL），UI 现显示总资产与单笔风险上限列。


## 六、真实占比提示 + 止损提醒（fb27345 + 修正）

### 背景
真实持仓只有 600036/601398 各 200 股（总市值约 1.28 万），属**底仓建仓阶段**。
真实资本基数暂未知，资产规模小导致单股占比虚高（600036=60%），若用"真实占比 vs 上限"
判断会误报超配、阻碍建底仓。

### 最终口径（目标权重为主，真实占比仅提示）
- `build_position_plan` 新增 `actual_holdings` 参数：current 主口径仍为目标权重（规划缺口，
  先建底仓），真实市值占比存 `current_actual` 仅供展示。
- 真实占比超单票上限时在 reasons 附提示（如"真实持仓占比 60.1% 已超单票上限 30%"），
  不阻断主口径结论——符合底仓阶段语义。
- t15：顶部注明口径，逐只"当前%"显示"目标值（真实值）"，真实超限红字提示。
- 实测：600036 目标10%→可加仓+超限提示；601398 同；600900 目标25%>目标20%→持有。

### 止损提醒（新增，独立于仓位口径）
- `modules/stop_loss_monitor.py`：真实持仓现价较 avg_cost 回撤 ≥ stop_loss_pct(5%)
  触发卖出提醒；无现价用成本价不误报。
- t15 顶部红色警示触发项；含 stop_loss_from_df 供推送复用。

### 验证
- 新增测试：position_manager 真实占比提示 3 例 + stop_loss 5 例。
- 全量 pytest **275 passed**。

### 评估后不修改
- 评分 audit/pledge 缺失是按"可得维度归一化"(st/sw) 折算，非死权重，逻辑正确。
- 加仓建议主口径维持目标权重（用户确认先建底仓，真实占比不作主判据）。


## 七、组合级回撤熔断 + 止损推送（风控闭环补齐）

### 组合级回撤熔断（新增 modules/drawdown_guard.py）
- 记录真实持仓每日总市值快照（data/portfolio_drawdown.json，gitignore 已忽略）。
- 回撤 = (当前总市值 − 历史峰值) / 历史峰值；阈值 = 各层 layers.max_drawdown 最大值（默认 10%）。
- 熔断时 position_manager 所有建仓/加仓 → "暂停（组合级回撤熔断）"，保留止损/卖出提示。
- t15 每次渲染记录快照，熔断激活时顶部红字并重算 plan。
- 补上"单笔风险预算只管单笔、组合受伤无硬闸门"的缺口。
- 测试 6 例（快照/峰值/回补解冻/阈值/熔断阻断）。

### 止损推送（daily_picks 接入）
- compose_message 增加止损段：真实持仓现价较成本回撤 ≥5% 时推送文案追加"🔴 止损提醒"。
- build_picks 在 result 附 _df 复用已有行情，避免二次拉取；无触发静默不打扰。
- 冒烟：600036 现价36（成本38.58，回撤6.7%）→ 推送含止损提醒。
- 测试：test_daily_picks/test_daily_pusher 37 例全绿。

### 验证
- 全量 pytest **281 passed**。
- 真实快照：12838.14 元，0% 回撤 → 不熔断。

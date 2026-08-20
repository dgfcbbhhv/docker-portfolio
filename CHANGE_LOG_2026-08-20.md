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
- 逐层累计不得突破 maxpos；`add_count` 参数预留台账接入（未接入前按首层放量）。
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
- **加仓台账**未落地：当前 ladder 按首层放量，`add_count` 参数已预留；后续可把真实成交（`data/execution_log.json`）的加仓笔数回填。
- 既有失败测试（test_daily_picks / test_actual_holdings）未在本期处理，与本改动无关。
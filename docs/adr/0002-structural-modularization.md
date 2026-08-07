# ADR-0002: 结构模块化 — 风险合并、缓存下沉、注入式依赖与脚手架清理

日期：2026-08-07
状态：已采纳

## 背景

代码量增长后暴露结构性债：

1. `layer_risk.py` / `stress.py` 重复实现同套"逐层波动率"业务逻辑，分歧风险高。
2. 缓存逻辑（TTL/freshness 包装）散落在入口 `app.py` 顶层脚本内，与领域代码混写。
3. `calc_overall_tier(code, price)` 依赖从模块级全局单例 `STOCK_DETAIL` 读取估值数据，
   测试中被迫对全局做 `mock.patch`，边界不干净。
4. 根目录散落一次性自动数据处理脚本（`format_aihot.py` / `parse_aihot.py` / `check_div.py`），
   含硬编码绝对路径，不属于应用代码。
5. 领域词汇（"分级""分层风险""tier""layer"）无唯一权威定义，文档/代码/对话通信成本高。

## 决策

- **C1**：共享领域逻辑抽入 `modules/risk.py`（深模块，含 `layer_risk()` 与 `stress()`），
  `layer_risk.py` / `stress.py` 改为薄适配层委托之，避免重复。
- **C2**：缓存包装从入口下沉到 `modules/data/app_cache.py`；对外用别名保持调用方零改动，
  降低"行为变更"风险。
- **C3**：`cost_tier.calc_overall_tier()` 增加可选注入形参 `stock=None`，
  默认回落全局 `STOCK_DETAIL`，测试与调用方可显式注入，去掉 patch 依赖。
- **C5**：一次性脚本与脱敏数据移出仓库根目录至 `.scratch-aihot/`（被 `.gitignore` 排除）。
- **C4**：新增 `CONTEXT.md` 建立领域词汇唯一权威（层/分级/黄金-白银-警戒档）。
- 目标：`app.py` 只保留流程编排与页面渲染，不承载层逻辑。完整 18-tab 拆分（C6）另行立项。

## 影响

- 测试基线从 158 → 169 passed（修复 4 个受真实配置耦合影响的用例并保持全绿）。
- 行为保持：调用方零改动（别名与默认参数），风险逻辑单一来源。
- 风险层/应力层后续演进只改 `modules/risk.py`。

## 备选方案（未采纳）

- 直接在 `app.py` 内联修 bug：治标，重复仍存在。
- 一次性拆分全部 18 tab（C6）：一次性变更面过大，且 Streamlit 运行时验证依赖 NAS 环境，
  风险高于收益，故拆分单独立项。
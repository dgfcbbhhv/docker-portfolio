"""正金字塔加仓层级 — 把"每次都想加"的冲动锁进分层上限。

设计（不含台账，用仓位区间近似）：
- 输入当前仓位 current_ratio 与 Kelly 目标 target_ratio，gap = target - current。
- gap 按递减权重拆成 max_steps 层（默认 40→30→20 的近似：[0.5,0.3,0.2]）。
- 任一硬门禁命中即整层持有：档位警戒 / 评分低于买入线 / 已达单票上限 / gap 不足。
- 输出建议本次加仓比例 add_pct 与后续各层上限，供 position_manager 与 UI 使用。
"""
from modules.config import DAILY_PUSH

DEFAULT_MAX_STEPS = 3
DEFAULT_TIERS = (0.5, 0.3, 0.2)
DEFAULT_MIN_GAP = 2.0

# 历史已加仓次数可由调用方注入（默认 0，即视为首层）。若未来落地加仓台账，
# 传 add_count 即可按真实层级放量，无需改本模块。


def plan_add_ladder(current_ratio, target_ratio, *, tier="数据不足",
                    lishing=0.0, buy_th=None, maxpos=30.0, min_gap=None,
                    max_steps=None, tiers=None, add_count=0):
    """计算单只标的的正金字塔加仓层级。

    Args:
        current_ratio: 当前仓位（%）。
        target_ratio: Kelly 组合约束后目标仓位（%）。
        tier: 成本档位，警戒即禁止加仓。
        lishing: 李氏评分。
        buy_th: 买入线（默认取 DAILY_PUSH.lishing_buy）。
        maxpos: 单票仓位上限（%）。
        min_gap: 最小加仓空间（%）。
        max_steps: 加仓层级总数上限。
        tiers: 各层递减权重（和应为 1）。
        add_count: 已加仓次数，用于锁定当前所在层。

    Returns:
        dict: {verdict, reason, step, add_pct, ladder_alloc, cap_hit}
    """
    buy_th = float(buy_th if buy_th is not None else DAILY_PUSH.get("lishing_buy", 70.0))
    min_gap = float(min_gap if min_gap is not None else DEFAULT_MIN_GAP)
    max_steps = int(max_steps if max_steps is not None else DEFAULT_MAX_STEPS)
    tiers = list(tiers if tiers is not None else DEFAULT_TIERS)
    tiers = tiers[:max_steps]

    current = float(current_ratio or 0.0)
    target = float(target_ratio or 0.0)
    gap = max(0.0, target - current)

    if tier == "警戒":
        return {"verdict": "持有", "reason": "成本档位警戒，禁止加仓",
                "step": None, "add_pct": 0.0, "ladder_alloc": [], "cap_hit": False}
    if lishing <= 0:
        return {"verdict": "持有", "reason": "评分不可用或触发红旗",
                "step": None, "add_pct": 0.0, "ladder_alloc": [], "cap_hit": False}
    if lishing < buy_th:
        return {"verdict": "持有", "reason": f"评分 {lishing:.0f} 低于买入线 {buy_th:.0f}",
                "step": None, "add_pct": 0.0, "ladder_alloc": [], "cap_hit": False}
    if current >= maxpos:
        return {"verdict": "持有", "reason": "已达单票仓位上限",
                "step": None, "add_pct": 0.0, "ladder_alloc": [], "cap_hit": True}
    if gap < min_gap:
        return {"verdict": "持有", "reason": f"加仓空间不足（gap {gap:.1f}% < {min_gap:.1f}%）",
                "step": None, "add_pct": 0.0, "ladder_alloc": [], "cap_hit": False}

    # 各层分配：递减权重 × 总 gap
    total_w = sum(tiers) or 1.0
    alloc = [round(gap * w / total_w, 1) for w in tiers]

    # 累计上限校验：每一层叠加后不得突破 maxpos
    capped = []
    cap_hit = False
    running = current
    for a in alloc:
        room = maxpos - running
        if room <= 0:
            capped.append(0.0)
            cap_hit = True
            break
        take = min(a, round(room, 1))
        if take < a:
            cap_hit = True
        capped.append(take)
        running += take

    # 定位当前层
    if add_count < 0:
        add_count = 0
    if add_count >= len(capped):
        return {"verdict": "持有", "reason": f"已达 {len(capped)} 层加仓上限，转为持有",
                "step": len(capped), "add_pct": 0.0, "ladder_alloc": capped, "cap_hit": True}
    add_pct = capped[add_count]
    step = add_count + 1

    if add_pct <= 0:
        return {"verdict": "持有", "reason": "已达层级上限，转为持有",
                "step": step, "add_pct": 0.0, "ladder_alloc": capped, "cap_hit": True}

    return {"verdict": "可加仓",
            "reason": f"第 {step}/{len(capped)} 层正金字塔加仓",
            "step": step, "add_pct": add_pct,
            "ladder_alloc": capped, "cap_hit": cap_hit}
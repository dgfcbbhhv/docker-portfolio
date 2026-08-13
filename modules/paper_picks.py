"""推送建议 → 模拟盘自动执行桥。

把 daily_picks 的分类建议（可建仓/可加仓/减仓）自动转成模拟盘挂单，
让虚拟组合按推送的行动建议执行 T+1 开盘成交，形成 推送→自动模拟执行 的闭环。

仅处理 A 股（paper_executor 的 T+1/涨跌停/手数成交假设面向 A 股）；
HK/US 推送建议跳过，避免生成必然「停牌/无行情」的噪音挂单。

开关与参数: config.json paper_trade 段
- auto_follow_picks: 是否启用（默认 True）
- picks_buy_pct: 无 Kelly 时可建仓/可加仓的默认目标仓位%
- picks_reduce_ratio: 减仓建议卖出当前仓位比例（默认 0.5）
"""
import datetime
import logging

logger = logging.getLogger(__name__)

from modules.config import PAPER_TRADE
from modules.paper_common import MAX_POSITION_PCT, MIN_LOT, PORTFOLIO_FILE, _save_json
from modules.paper_executor import _get_paper_portfolio
from modules.shadow_ledger import record_shadow

CREATABLE = ("可建仓", "可加仓")


def apply_picks_to_paper(picks_result, signal_date=None):
    """把 daily_picks 结果转成模拟盘挂单（幂等，picks 优先级覆盖同日信号单）。

    Returns:
        dict: {"created": [code...], "skipped": [{code, reason}...]}
    """
    if not PAPER_TRADE.get("auto_follow_picks"):
        return {"created": [], "skipped": [], "disabled": True}

    picks = picks_result.get("picks", []) or []
    signal_date = signal_date or picks_result.get("date") or datetime.date.today().isoformat()
    pf = _get_paper_portfolio()

    pending = [p for p in pf.get("pending_orders", [])
               if not (p.get("signal_date") == signal_date
                       and p.get("code") in {x.get("code") for x in picks})]

    buy_pct = min(float(PAPER_TRADE.get("picks_buy_pct", 10.0)), MAX_POSITION_PCT)
    reduce_ratio = float(PAPER_TRADE.get("picks_reduce_ratio", 0.5))
    created = []
    skipped = []

    for pick in picks:
        code = pick.get("code")
        if pick.get("market") != "A":
            reason = "非 A 股，模拟盘仅支持 A 股 T+1 成交"
            skipped.append({"code": code, "market": pick.get("market"), "reason": reason})
            record_shadow(pick, signal_date, reason)
            continue
        price = float(pick.get("price", 0) or 0)
        if price <= 0:
            skipped.append({"code": code, "reason": "无有效价格"})
            continue
        category = pick.get("category")
        pos = pf.get("positions", {}).get(code, {})
        held_shares = pos.get("shares", 0)

        if category in CREATABLE:
            kelly = float(pick.get("kelly_final", 0) or 0)
            pct = min(kelly, buy_pct) if kelly > 0 else buy_pct
            target_value = pf.get("capital", 0) * pct / 100.0
            current_value = held_shares * price
            delta = target_value - current_value
            if delta < MIN_LOT * price:
                skipped.append({"code": code, "reason": f"目标仓位 {pct:.1f}% 不足一手或已达标"})
                continue
            shares = int(delta / price / MIN_LOT) * MIN_LOT
            if shares < MIN_LOT:
                skipped.append({"code": code, "reason": "不足一手"})
                continue
            pending.append({
                "signal_date": signal_date, "code": code, "action": "buy",
                "shares": shares, "source": "daily_picks",
                "risk_flags": str(pick.get("risk_flags", "") or "").strip(),
                "reason": f"{category}（推送建议自动模拟执行，目标仓位 {pct:.1f}%）",
            })
            created.append(code)
        elif category == "减仓" and held_shares > 0:
            shares = int(held_shares * reduce_ratio / MIN_LOT) * MIN_LOT
            if shares < MIN_LOT:
                skipped.append({"code": code, "reason": "减仓后不足一手可卖"})
                continue
            pending.append({
                "signal_date": signal_date, "code": code, "action": "sell",
                "shares": shares, "source": "daily_picks",
                "reason": f"{category}（推送建议自动模拟执行，减仓比例 {int(reduce_ratio * 100)}%）",
            })
            created.append(code)
        else:
            skipped.append({"code": code, "reason": f"分类 {category} 无执行动作"})

    pf["pending_orders"] = pending
    pf["last_picks_signal_date"] = signal_date
    _save_json(PORTFOLIO_FILE, pf)

    if created:
        logger.info("[PaperPicks] 推送建议转模拟盘挂单 %s -> %s", signal_date, created)
    return {"created": created, "skipped": skipped, "disabled": False}

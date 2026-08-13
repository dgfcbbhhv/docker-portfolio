"""统一"实际持仓"账本：以真实成交（broker_trades.json）为规范源。

口径
----
- 看板 `ratio`（原始权重）= 持仓**目标**（计划）。
- 真实成交 `broker_trades.json` = 实际持仓的**权威账本**（含券商 shares_after）。
- 成本分层(cost_tiers.json)、影子账号(paper_portfolio.json)、成交对账(execution_log.json)
  都应与此对账、互通，而不是各自维护一份不一致的持仓。

对外接口（稳定，后续接券商直连只改内部实现）：
- get_actual_positions()   -> {code: {shares, avg_cost, market, currency}}
- get_actual_holdings()    -> 兼容 holdings.get_actual_holdings 的结构
- reconcile()              -> 三处(成本/影子/成交)对账报告，找出差异
"""

import json
import os

from modules.paths import data_dir, notes_dir

BROKER_PATH = os.path.join(data_dir(), "broker_trades.json")
SHADOW_PATH = os.path.join(data_dir(), "paper_portfolio.json")  # 真实持仓镜像(影子账号)，与模拟盘无关
COST_PATH = os.path.join(notes_dir(), "cost_tiers.json")
EXEC_PATH = os.path.join(data_dir(), "execution_log.json")


def _load_json(path, key="records"):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d.get(key, []) if key else d
        return d
    except (json.JSONDecodeError, OSError):
        return []


def _load_positions():
    """影子账号 positions：{code: {shares, avg_cost}}。"""
    if not os.path.exists(SHADOW_PATH):
        return {}
    try:
        with open(SHADOW_PATH, "r", encoding="utf-8") as f:
            pf = json.load(f)
        return (pf or {}).get("positions") or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _load_cost_batches():
    """成本分层批次：{code: {"batches":[...], "dividend_per_share": x}}。"""
    if not os.path.exists(COST_PATH):
        return {}
    try:
        with open(COST_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def get_actual_positions():
    """以 broker_trades 聚合实际持仓（规范源）。

    返回 {code: {shares, avg_cost, market, currency, fee_in_cost}}。
    - shares: Σbuy - Σsell（净持仓）
    - avg_cost: 加权买入成本（含费用），按买入额加权
    """
    trades = _load_json(BROKER_PATH)
    out = {}
    buys = {}
    for t in trades:
        code = t.get("code")
        if not code:
            continue
        side = str(t.get("side", "")).lower()
        try:
            shares = int(float(t.get("shares") or 0))
            price = float(t.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if shares <= 0 or price <= 0:
            continue
        fee = 0.0
        try:
            fee = float(t.get("fee_total") or 0)
        except (TypeError, ValueError):
            fee = 0.0
        b = buys.setdefault(code, {"shares": 0, "cost": 0.0, "amt": 0.0,
                                   "market": t.get("market", ""), "currency": t.get("currency", ""),
                                   "last_buy_date": None})
        if side == "buy":
            b["shares"] += shares
            b["amt"] += shares * price
            b["cost"] += shares * price + fee
            d = t.get("executed_at") or t.get("date")
            if d:
                d = str(d)[:10]
                if b.get("last_buy_date") is None or d > b["last_buy_date"]:
                    b["last_buy_date"] = d
        elif side == "sell":
            b["shares"] -= shares
            # 卖出不增加成本（成本仍在已持仓口径内）
    for code, b in buys.items():
        if b["shares"] <= 0:
            continue
        avg = b["cost"] / b["shares"] if b["shares"] > 0 else 0.0
        out[code] = {
            "shares": b["shares"],
            "avg_cost": round(avg, 4),
            "market": b.get("market", ""),
            "currency": b.get("currency", ""),
            "last_buy_date": b.get("last_buy_date"),
        }
    return out


def get_actual_holdings(positions=None):
    """兼容 holdings.get_actual_holdings 结构；broker 无数据时回退影子账号。"""
    pos = positions if positions is not None else get_actual_positions()
    if pos:
        return {"positions": pos, "cash": 0.0, "capital": 0.0, "_source": "broker"}
    shadow = _load_positions()
    if shadow:
        return {"positions": shadow, "cash": 0.0, "capital": 0.0, "_source": "paper"}
    return {}


def reconcile():
    """三处真实数据对账：成本分层 / 影子账号 / 真实成交。

    返回 {"codes": sorted, "rows": [{code, broker_shares, shadow_shares, cost_shares, ok}],
          "issues": [str]}
    """
    broker = get_actual_positions()
    shadow = _load_positions()
    cost = _load_cost_batches()

    codes = sorted(set(broker) | set(shadow) | set(cost))
    rows = []
    issues = []
    for c in codes:
        b_sh = int(broker.get(c, {}).get("shares", 0) or 0)
        s_sh = int((shadow.get(c) or {}).get("shares", 0) or 0)
        c_sh = int(sum(float(x.get("shares") or 0) for x in (cost.get(c, {}) or {}).get("batches", [])))
        ok = (b_sh == s_sh == c_sh)
        rows.append({"code": c, "broker_shares": b_sh, "shadow_shares": s_sh,
                     "cost_shares": c_sh, "ok": ok})
        if not ok:
            parts = []
            if b_sh != s_sh:
                parts.append(f"影子({s_sh})≠成交({b_sh})")
            if b_sh != c_sh:
                parts.append(f"成本分层({c_sh})≠成交({b_sh})")
            issues.append(f"{c}: " + "；".join(parts))
    return {"rows": rows, "issues": issues}


def sync_shadow_from_broker():
    """用真实成交把影子账号(paper_portfolio.json)回写一致。

    返回 {"updated": bool, "positions": {...}, "note": str}。
    影子文件不存在时按 broker 净持仓创建；已存在则覆盖 positions 与现金估值。
    """
    pos = get_actual_positions()
    if not pos:
        return {"updated": False, "positions": {}, "note": "无真实成交，跳过"}
    shadow = {}
    if os.path.exists(SHADOW_PATH):
        try:
            with open(SHADOW_PATH, "r", encoding="utf-8") as f:
                shadow = json.load(f) or {}
        except (json.JSONDecodeError, OSError):
            shadow = {}
    old = (shadow or {}).get("positions") or {}
    new_positions = {c: {"shares": v["shares"], "avg_cost": v["avg_cost"]}
                     for c, v in pos.items()}
    changed = new_positions != old
    shadow["positions"] = new_positions
    os.makedirs(os.path.dirname(SHADOW_PATH), exist_ok=True)
    tmp = SHADOW_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(shadow, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SHADOW_PATH)
    return {"updated": changed, "positions": new_positions,
            "note": "已用真实成交同步影子账号" if changed else "影子账号已与真实成交一致"}


def fix_cost_batches_from_broker():
    """按真实成交净持仓重写成本分层批次（解决卖出未扣减等历史错账）。

    以 broker 净持仓为准，为每只标的生成单一合并批次，日期取最近一笔买入日
    （避免用无效占位日期破坏 to_datetime）：
    {"date": 最近买入日, "shares": 净股数, "cost": 加权成本, "note": "真实成交"}

    返回 {"updated": bool, "codes": [str], "note": str}。
    """
    import datetime
    pos = get_actual_positions()
    if not pos:
        return {"updated": False, "codes": [], "note": "无真实成交，跳过"}
    cost = {}
    if os.path.exists(COST_PATH):
        try:
            with open(COST_PATH, "r", encoding="utf-8") as f:
                cost = json.load(f) or {}
        except (json.JSONDecodeError, OSError):
            cost = {}
    today = datetime.date.today().isoformat()
    changed = False
    for code, v in pos.items():
        entry = cost.get(code) or {}
        d = (v.get("last_buy_date") or "").strip() or today
        new_batch = [{"date": d, "shares": v["shares"],
                      "cost": v["avg_cost"], "note": "真实成交"}]
        old_batches = entry.get("batches") or []
        if old_batches != new_batch:
            entry["batches"] = new_batch
            cost[code] = entry
            changed = True
    if not changed:
        return {"updated": False, "codes": list(pos), "note": "成本分层已与真实成交一致"}
    os.makedirs(os.path.dirname(COST_PATH), exist_ok=True)
    tmp = COST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cost, f, ensure_ascii=False, indent=2)
    os.replace(tmp, COST_PATH)
    return {"updated": True, "codes": list(pos), "note": "已按真实成交重写成本批次"}

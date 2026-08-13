"""港美股影子账本 — 补齐"推送→模拟盘"执行审计盲区。

模拟盘仅支持 A 股 T+1 成交, 港美股推送建议在 paper_picks 中被跳过。
这里把每次跳过的可执行建议落成台账(append-only jsonl), 记录日期/标的/类别/
价格/Kelly/风险标记, 供审计与"若执行会怎样"回测使用。写入失败不影响主流程。
"""
import datetime
import json
import logging
import os

logger = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data", "paper")
SHADOW_FILE = os.path.join(_DIR, "shadow_ledger.jsonl")


def _existing_keys():
    """返回 {(date, code)} 集合, 用于幂等去重。"""
    keys = set()
    try:
        with open(SHADOW_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    keys.add((r.get("date"), r.get("code")))
                except Exception:
                    continue
    except OSError:
        pass
    return keys


def record_shadow(pick, signal_date, reason):
    """记录一条港美股跳过的可执行建议。返回是否写入。"""
    if not pick or not pick.get("code"):
        return False
    category = pick.get("category", "")
    if category in ("可建仓", "可加仓"):
        action = "buy"
    elif category == "减仓":
        action = "sell"
    else:
        action = "none"
    row = {
        "date": signal_date or datetime.date.today().isoformat(),
        "ts": datetime.datetime.now().isoformat(),
        "code": pick.get("code"),
        "name": pick.get("name", pick.get("code")),
        "market": pick.get("market"),
        "category": category,
        "action": action,
        "price": pick.get("price"),
        "kelly_final": pick.get("kelly_final"),
        "risk_flags": pick.get("risk_flags", ""),
        "reason": reason,
    }
    try:
        if (row["date"], row["code"]) in _existing_keys():
            return False
        os.makedirs(_DIR, exist_ok=True)
        with open(SHADOW_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        logger.warning("[shadow] 台账写入失败: %s", e)
        return False


def shadow_summary():
    """汇总台账: 总行数/按市场/按动作/最新日期。"""
    rows = []
    try:
        with open(SHADOW_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except OSError:
        pass
    if not rows:
        return {"total": 0, "by_market": {}, "by_action": {}, "latest_date": None}
    by_market = {}
    by_action = {}
    for r in rows:
        m = r.get("market") or "?"
        by_market[m] = by_market.get(m, 0) + 1
        a = r.get("action") or "?"
        by_action[a] = by_action.get(a, 0) + 1
    return {
        "total": len(rows),
        "by_market": by_market,
        "by_action": by_action,
        "latest_date": max(r.get("date") or "" for r in rows),
    }

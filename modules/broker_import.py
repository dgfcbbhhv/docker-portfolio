"""A 股券商成交导入与推送关联。
import logging

logger = logging.getLogger(__name__)
支持常见券商导出的成交明细表（如 Table_*.xlsx）：
  成交日期, 成交时间, 证券代码, 证券名称, 委托方向, 成交数量, 成交均价,
  成交金额, 佣金, 其他费用, 印花税, 过户费, 资金余额, 股份余额,
  委托编号, 成交编号, 交易市场, 币种

导入时归一化为 broker_trades，并按「日期 + 代码」自动关联推送审计中的
daily_picks push_id，未匹配的可在界面手动关联。
"""
import datetime
import json
import os
from collections import defaultdict

import pandas as pd

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
TRADES_PATH = os.path.join(_DATA_DIR, "broker_trades.json")

COLUMN_ALIASES = {
    "成交日期": "trade_date",
    "成交时间": "trade_time",
    "证券代码": "code",
    "证券名称": "name",
    "委托方向": "side_raw",
    "成交数量": "shares",
    "成交均价": "price",
    "成交金额": "amount",
    "佣金": "commission",
    "其他费用": "other_fee",
    "印花税": "stamp_tax",
    "过户费": "transfer_fee",
    "资金余额": "cash_after",
    "股份余额": "shares_after",
    "委托编号": "order_ref",
    "成交编号": "trade_ref",
    "交易市场": "market",
    "币种": "currency",
}

_FEE_COLS = ("commission", "other_fee", "stamp_tax", "transfer_fee")


def normalize_side(value):
    s = str(value or "").strip()
    if "买" in s or s.upper() in ("B", "BUY"):
        return "buy"
    if "卖" in s or s.upper() in ("S", "SELL"):
        return "sell"
    return None


def _norm_code(value):
    s = str(value or "").strip().upper()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    for suf in (".SH", ".SZ", ".BJ", ".HK", ".US"):
        if s.endswith(suf):
            s = s[:-len(suf)]
    for pref in ("SH", "SZ", "BJ", "HK"):
        if s.startswith(pref) and s[len(pref):].isdigit():
            s = s[len(pref):]
    return s.zfill(6) if s.isdigit() else s


def parse_trade_table(path):
    """解析券商成交表，返回归一化记录列表。"""
    df = pd.read_excel(path)
    df.columns = [str(c).strip() for c in df.columns]
    rename = {c: COLUMN_ALIASES[c] for c in df.columns if c in COLUMN_ALIASES}
    df = df.rename(columns=rename)

    records = []
    for _, row in df.iterrows():
        code = _norm_code(row.get("code", ""))
        side = normalize_side(row.get("side_raw"))
        if not code or not side:
            continue
        try:
            shares = int(float(row.get("shares")))
            price = float(row.get("price"))
            if shares <= 0 or price <= 0:
                continue
            amount = float(row.get("amount") or 0)
            if not amount:
                amount = shares * price
            trade_date = str(row.get("trade_date", "")).strip()[:10]
            trade_time = str(row.get("trade_time", "")).strip()
            executed_at = f"{trade_date}T{trade_time}" if trade_time else trade_date
            fee_breakdown = {}
            for col in _FEE_COLS:
                try:
                    fee_breakdown[col] = round(float(row.get(col) or 0), 4)
                except (TypeError, ValueError):
                    fee_breakdown[col] = 0.0
            records.append({
                "executed_at": executed_at,
                "code": code,
                "name": str(row.get("name", "") or code),
                "side": side,
                "shares": shares,
                "price": round(price, 4),
                "amount": round(amount, 2),
                "fee_total": round(sum(fee_breakdown.values()), 4),
                "fee_breakdown": fee_breakdown,
                "cash_after": float(row.get("cash_after") or 0),
                "shares_after": int(float(row.get("shares_after") or 0)),
                "order_ref": str(row.get("order_ref", "") or ""),
                "trade_ref": str(row.get("trade_ref", "") or ""),
                "market": str(row.get("market", "") or ""),
                "currency": str(row.get("currency", "") or ""),
                "push_id": None,
            })
        except (TypeError, ValueError):
            continue
    return records


def _load():
    if os.path.exists(TRADES_PATH):
        try:
            with open(TRADES_PATH, "r", encoding="utf-8") as f:
                return json.load(f).get("records", [])
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save(records):
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = TRADES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"records": records}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TRADES_PATH)


def match_push(record):
    """按日期 + 代码自动匹配 daily_picks 推送。"""
    from modules.push_audit import load_push_log
    day = record.get("executed_at", "")[:10]
    if not day:
        return None
    prev_day = None
    try:
        prev_day = (datetime.date.fromisoformat(day)
                    - datetime.timedelta(days=1)).isoformat()
    except ValueError:
        pass
    for rec in load_push_log(limit=500):
        meta = rec.get("meta") or {}
        if meta.get("kind") != "daily_picks":
            continue
        push_day = meta.get("date")
        if push_day not in (day, prev_day):
            continue
        codes = meta.get("codes") or []
        if record.get("code") in codes:
            return rec["push_id"]
    return None


def import_records(records):
    """去重导入，并自动关联推送。"""
    existing = _load()
    seen = {
        (r.get("trade_ref"), r.get("order_ref"), r.get("executed_at"),
         r.get("code"), r.get("side"))
        for r in existing
    }
    added = 0
    duplicates = 0
    for rec in records:
        key = (rec.get("trade_ref"), rec.get("order_ref"), rec.get("executed_at"),
               rec.get("code"), rec.get("side"))
        if key in seen:
            duplicates += 1
            continue
        rec["push_id"] = rec.get("push_id") or match_push(rec)
        rec["imported_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        existing.append(rec)
        seen.add(key)
        added += 1
    if added:
        _save(existing)
        try:
            from modules.execution_log import confirm_from_broker
            confirm_from_broker()
        except Exception as _e:
            logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    return {"added": added, "duplicates": duplicates, "records": existing[-20:]}


def load_broker_trades(limit=200):
    rows = _load()
    return rows[-limit:][::-1]


def update_push_link(trade_ref, push_id):
    """手动关联/解除推送。"""
    records = _load()
    changed = 0
    for r in records:
        if r.get("trade_ref") == trade_ref:
            r["push_id"] = push_id or None
            changed += 1
    if changed:
        _save(records)
    return changed


def _realized_pnl(trades):
    buys = defaultdict(list)
    pnl = 0.0
    fees = 0.0
    by_code = defaultdict(lambda: {"pnl": 0.0, "fees": 0.0})
    for t in sorted(trades, key=lambda x: x.get("executed_at", "")):
        fee = float(t.get("fee_total") or 0)
        fees += fee
        code = t["code"]
        if t["side"] == "buy":
            buys[code].append({
                "shares": int(t["shares"]),
                "orig_shares": int(t["shares"]),
                "price": float(t["price"]),
                "fee": fee,
            })
        elif t["side"] == "sell":
            remaining = int(t["shares"])
            code_pnl = 0.0
            while remaining > 0 and buys[code]:
                lot = buys[code][0]
                take = min(remaining, lot["shares"])
                lot_cost = lot["price"] * take
                if lot["orig_shares"] > 0:
                    lot_cost += lot["fee"] * take / lot["orig_shares"]
                code_pnl += take * float(t["price"]) - lot_cost
                remaining -= take
                lot["shares"] -= take
                if lot["shares"] <= 0:
                    buys[code].pop(0)
            code_pnl -= fee
            pnl += code_pnl
            by_code[code]["pnl"] += code_pnl
            by_code[code]["fees"] += fee
    return {"pnl": pnl, "fees": fees, "by_code": dict(by_code)}


def reconcile_report():
    """真实成交 vs 模拟成交 vs 推送 的三段对账摘要。"""
    trades = _load()
    from modules.dividend_log import dividend_summary
    div_summary = dividend_summary()
    paper = []
    pf_path = os.path.join(_DATA_DIR, "paper", "paper_portfolio.json")
    if os.path.exists(pf_path):
        try:
            with open(pf_path, "r", encoding="utf-8") as f:
                paper = json.load(f).get("trades", [])
        except (json.JSONDecodeError, OSError):
            paper = []

    linked = [t for t in trades if t.get("push_id")]
    deviations = []
    matched_paper = 0
    for t in linked:
        day = t.get("executed_at", "")[:10]
        candidates = [
            p for p in paper
            if p.get("date") == day and p.get("code") == t.get("code")
            and p.get("action") == t.get("side")
        ]
        if candidates:
            matched_paper += 1
            p = candidates[0]
            if p.get("price"):
                deviations.append(
                    (float(t["price"]) - float(p["price"])) / float(p["price"]) * 100
                )

    realized = _realized_pnl(trades)
    dividend_income = round(div_summary.get("total_income", 0), 2)
    return {
        "total_trades": len(trades),
        "linked_trades": len(linked),
        "paper_matched": matched_paper,
        "avg_price_deviation_pct": (
            round(sum(deviations) / len(deviations), 2) if deviations else None
        ),
        "realized_pnl": round(realized["pnl"], 2),
        "dividend_income": dividend_income,
        "total_profit": round(realized["pnl"] + dividend_income, 2),
        "dividend_by_code": div_summary.get("by_code", {}),
        "total_fees": round(realized["fees"], 2),
        "by_code": realized["by_code"],
    }

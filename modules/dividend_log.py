"""分红到账登记：长期持有策略的真实现金分红收入。

券商成交表通常只含买卖成交，不含分红派息；这里提供手动登记分红到账，
并把累计分红并入真实账户对账的“含分红总盈利”。
"""
import datetime
import json
import os

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
DIVIDEND_PATH = os.path.join(_DATA_DIR, "dividend_log.json")


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _load():
    if os.path.exists(DIVIDEND_PATH):
        try:
            with open(DIVIDEND_PATH, "r", encoding="utf-8") as f:
                return json.load(f).get("records", [])
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save(records):
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = DIVIDEND_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"records": records}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DIVIDEND_PATH)


def add_dividend(code, name, pay_date, amount, note=""):
    """登记一笔实际到账分红，幂等不校验（允许同日多次记录）。"""
    records = _load()
    rec = {
        "code": str(code or "").strip().upper(),
        "name": str(name or code or "").strip(),
        "pay_date": str(pay_date or "")[:10],
        "amount": round(float(amount), 2),
        "note": str(note or "").strip(),
        "recorded_at": _now_iso(),
    }
    if not rec["code"] or rec["amount"] <= 0 or not rec["pay_date"]:
        raise ValueError("code / pay_date / amount 必须有效")
    records.append(rec)
    _save(records)
    return rec


def get_dividend_log(limit=200):
    return _load()[-limit:][::-1]


def dividend_summary():
    rows = _load()
    total = round(sum(r.get("amount", 0) for r in rows), 2)
    by_code = {}
    for r in rows:
        code = r.get("code", "")
        item = by_code.setdefault(code, {"count": 0, "amount": 0.0})
        item["count"] += 1
        item["amount"] = round(item["amount"] + float(r.get("amount", 0)), 2)
    return {
        "total_income": total,
        "count": len(rows),
        "by_code": by_code,
    }

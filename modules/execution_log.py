"""人工跟单执行登记：推送 → 人工确认 → 真实成交核验。

推送发出后先记为“待执行”，用户在界面登记“已按推送下单 / 部分执行 / 忽略+原因”；
之后导入券商真实成交，系统按 push_id 自动核验哪些代码实际成交，形成执行闭环。
"""
import datetime
import json
import os

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
EXECUTION_PATH = os.path.join(_DATA_DIR, "execution_log.json")

MANUAL_STATUSES = ("executed", "partial", "ignored")


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _load():
    if os.path.exists(EXECUTION_PATH):
        try:
            with open(EXECUTION_PATH, "r", encoding="utf-8") as f:
                return json.load(f).get("records", [])
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save(records):
    os.makedirs(_DATA_DIR, exist_ok=True)
    tmp = EXECUTION_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"records": records}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, EXECUTION_PATH)


def create_pending(push_id, meta=None):
    """为一条 daily_picks 推送建立待执行记录，幂等。"""
    meta = meta or {}
    records = _load()
    if any(r.get("push_id") == push_id for r in records):
        return False
    records.append({
        "push_id": push_id,
        "date": meta.get("date"),
        "market": meta.get("market"),
        "codes": meta.get("codes") or [],
        "freshness_ok": meta.get("freshness_ok"),
        "created_at": _now_iso(),
        "manual_status": None,
        "manual_note": "",
        "manual_at": None,
        "broker_codes": [],
        "broker_confirmed_at": None,
    })
    _save(records)
    return True


def ensure_pending_from_audit():
    """扫描推送审计，把 daily_picks 推送补成待执行记录。"""
    from modules.push_audit import load_push_log
    created = 0
    for rec in load_push_log(limit=500):
        meta = rec.get("meta") or {}
        if meta.get("kind") != "daily_picks" or not rec.get("push_id"):
            continue
        if create_pending(rec["push_id"], meta):
            created += 1
    return created


def record_manual(push_id, status, note=""):
    """登记人工跟单结果：executed / partial / ignored。"""
    if status not in MANUAL_STATUSES:
        raise ValueError(f"status must be one of {MANUAL_STATUSES}")
    records = _load()
    for r in records:
        if r.get("push_id") == push_id:
            r["manual_status"] = status
            r["manual_note"] = note or ""
            r["manual_at"] = _now_iso()
            _save(records)
            return True
    return False


def confirm_from_broker():
    """用真实成交回填：按 push_id 找出实际成交的代码。"""
    from modules.broker_import import load_broker_trades
    trades = load_broker_trades(limit=2000)
    records = _load()
    changed = False
    for r in records:
        codes = r.get("codes") or []
        if not codes:
            continue
        matched = sorted({
            t.get("code") for t in trades
            if t.get("push_id") == r.get("push_id") and t.get("code") in codes
        })
        if matched and set(matched) != set(r.get("broker_codes") or []):
            r["broker_codes"] = matched
            r["broker_confirmed_at"] = _now_iso()
            changed = True
    if changed:
        _save(records)
    return changed


def execution_status(entry):
    """由人工登记 + 真实成交推导当前执行状态，真实成交优先。"""
    codes = entry.get("codes") or []
    broker_codes = entry.get("broker_codes") or []
    if broker_codes:
        if codes and set(broker_codes) >= set(codes):
            return "成交确认"
        return "部分成交确认"
    manual = entry.get("manual_status")
    if manual == "executed":
        return "已登记执行"
    if manual == "partial":
        return "部分执行"
    if manual == "ignored":
        return "已忽略"
    return "待执行"


def get_execution_log(limit=200):
    rows = _load()
    for r in rows:
        r["status"] = execution_status(r)
    return rows[-limit:][::-1]


def pending_summary():
    rows = get_execution_log(limit=500)
    total = len(rows)
    pending = sum(1 for r in rows if r["status"] == "待执行")
    confirmed = sum(1 for r in rows if "成交确认" in r["status"])
    manual = sum(1 for r in rows if r["status"] in ("已登记执行", "部分执行"))
    ignored = sum(1 for r in rows if r["status"] == "已忽略")
    return {
        "total": total,
        "pending": pending,
        "confirmed": confirmed,
        "manual": manual,
        "ignored": ignored,
    }


def pending_reminders():
    """返回尚未登记、也没有真实成交核验的推送。"""
    return [r for r in get_execution_log(limit=200) if r["status"] == "待执行"]

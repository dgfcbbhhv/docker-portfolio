"""人工跟单执行登记单元测试"""
import json

import modules.execution_log as el
import modules.push_audit as push_audit


def _tmp_paths(tmp_path, monkeypatch):
    exec_path = tmp_path / "execution_log.json"
    audit_path = tmp_path / "push_audit.jsonl"
    trades_path = tmp_path / "broker_trades.json"
    monkeypatch.setattr(el, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(el, "EXECUTION_PATH", str(exec_path))
    monkeypatch.setattr(push_audit, "AUDIT_PATH", str(audit_path))
    import modules.broker_import as bi
    monkeypatch.setattr(bi, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bi, "TRADES_PATH", str(trades_path))
    return audit_path, trades_path


def _push_meta(push_id="push-1", date="2026-04-29", codes=None):
    return {
        "kind": "daily_picks", "market": "AH", "date": date,
        "freshness_ok": True, "codes": codes or ["600036", "601398"],
    }


def test_ensure_pending_from_audit(tmp_path, monkeypatch):
    audit_path, _ = _tmp_paths(tmp_path, monkeypatch)
    push_audit.log_push({
        "push_id": "push-1", "channel": "bark", "title": "选股",
        "content": "600036", "status": "sent",
        "meta": _push_meta(),
    })
    assert el.ensure_pending_from_audit() == 1
    assert el.ensure_pending_from_audit() == 0

    rows = el.get_execution_log()
    assert len(rows) == 1
    assert rows[0]["push_id"] == "push-1"
    assert rows[0]["codes"] == ["600036", "601398"]
    assert rows[0]["status"] == "待执行"


def test_record_manual(tmp_path, monkeypatch):
    _tmp_paths(tmp_path, monkeypatch)
    assert el.create_pending("push-1", _push_meta()) is True
    assert el.create_pending("push-1", _push_meta()) is False

    assert el.record_manual("push-1", "ignored", "已经持有，不重复买入") is True
    rows = el.get_execution_log()
    assert rows[0]["status"] == "已忽略"
    assert rows[0]["manual_note"] == "已经持有，不重复买入"

    assert el.record_manual("push-1", "executed", "") is True
    assert el.get_execution_log()[0]["status"] == "已登记执行"


def test_confirm_from_broker(tmp_path, monkeypatch):
    audit_path, trades_path = _tmp_paths(tmp_path, monkeypatch)
    push_audit.log_push({
        "push_id": "push-1", "channel": "bark", "title": "选股",
        "content": "600036", "status": "sent",
        "meta": _push_meta(codes=["600036", "601398"]),
    })
    el.ensure_pending_from_audit()

    import modules.broker_import as bi
    bi._save([{
        "executed_at": "2026-04-30T09:30:00", "code": "600036", "name": "招商银行",
        "side": "buy", "shares": 100, "price": 38.34, "amount": 3834.0,
        "fee_total": 5.0, "fee_breakdown": {}, "cash_after": 0, "shares_after": 100,
        "order_ref": "", "trade_ref": "T1", "market": "上海A股", "currency": "人民币",
        "push_id": "push-1", "imported_at": "2026-04-30T10:00:00",
    }])

    assert el.confirm_from_broker() is True
    rows = el.get_execution_log()
    assert rows[0]["broker_codes"] == ["600036"]
    assert rows[0]["status"] == "部分成交确认"

    assert el.confirm_from_broker() is False


def test_pending_summary_and_reminders(tmp_path, monkeypatch):
    _tmp_paths(tmp_path, monkeypatch)
    el.create_pending("push-1", _push_meta())
    el.create_pending("push-2", _push_meta(push_id="push-2", date="2026-04-30"))
    el.record_manual("push-2", "ignored", "不加仓")

    summary = el.pending_summary()
    assert summary["total"] == 2
    assert summary["pending"] == 1
    assert summary["ignored"] == 1

    reminders = el.pending_reminders()
    assert len(reminders) == 1
    assert reminders[0]["push_id"] == "push-1"

"""推送审计日志单元测试"""
import modules.push_audit as push_audit
from modules.alerts import AlertSender


def _tmp_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(push_audit, "AUDIT_PATH", str(tmp_path / "push_audit.jsonl"))


def test_success_send_writes_audit(tmp_path, monkeypatch):
    _tmp_audit(tmp_path, monkeypatch)
    sender = AlertSender(cfg={})
    monkeypatch.setattr(sender, "_send_once", lambda ch, t, c: True)

    assert sender.send("标题", "内容", channel="bark", meta={"market": "A"}) is True

    rows = push_audit.load_push_log()
    assert len(rows) == 1
    rec = rows[0]
    assert rec["status"] == "sent"
    assert rec["channel"] == "bark"
    assert rec["meta"]["market"] == "A"
    assert rec["content_hash"] == push_audit.content_hash("标题", "内容")


def test_failed_send_writes_audit_and_failure(tmp_path, monkeypatch):
    _tmp_audit(tmp_path, monkeypatch)
    sender = AlertSender(cfg={})
    monkeypatch.setattr(sender, "_send_once", lambda ch, t, c: False)

    assert sender.send("标题", "内容", channel="email", retries=1) is False

    rows = push_audit.load_push_log()
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["error"] == "channel returned False"


def test_push_summary_counts(tmp_path, monkeypatch):
    _tmp_audit(tmp_path, monkeypatch)
    sender = AlertSender(cfg={})
    monkeypatch.setattr(sender, "_send_once", lambda ch, t, c: True)
    sender.send("a", "1", channel="bark")
    sender.send("b", "2", channel="bark")
    monkeypatch.setattr(sender, "_send_once", lambda ch, t, c: False)
    sender.send("c", "3", channel="email", retries=1)

    summary = push_audit.push_summary()
    assert summary["total"] == 3
    assert summary["sent"] == 2
    assert summary["failed"] == 1
    assert summary["delivery_rate"] == 66.7

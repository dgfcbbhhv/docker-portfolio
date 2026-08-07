"""预警发送重试与多通道降级单元测试"""
from modules.alerts import AlertSender, FAILURE_LOG_PATH


def test_send_retries_then_succeeds(monkeypatch):
    sender = AlertSender(cfg={})
    calls = {"n": 0}

    def fake(channel, title, content):
        calls["n"] += 1
        return calls["n"] >= 2

    monkeypatch.setattr(sender, "_send_once", fake)
    assert sender.send("t", "c", channel="email", retries=3, delay=0) is True
    assert calls["n"] == 2


def test_send_failure_logged(tmp_path, monkeypatch):
    sender = AlertSender(cfg={})
    log = tmp_path / "alert_failures.log"
    monkeypatch.setattr("modules.alerts.FAILURE_LOG_PATH", str(log))
    monkeypatch.setattr(sender, "_send_once", lambda c, t, x: False)
    assert sender.send("t", "c", channel="email", retries=2, delay=0) is False
    assert log.exists()
    assert "email" in log.read_text(encoding="utf-8")


def test_send_multi_falls_back_to_next_channel(monkeypatch):
    sender = AlertSender(cfg={})

    def fake_send(title, content, channel="email", retries=3):
        return channel == "webhook"

    monkeypatch.setattr(sender, "send", fake_send)
    ok, channel = sender.send_multi("t", "c", channels=["email", "webhook"])
    assert ok is True
    assert channel == "webhook"

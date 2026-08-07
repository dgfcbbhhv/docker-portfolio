"""预警系统单元测试"""
import pytest
import sys
import os
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from modules.alerts import check_alerts, log_alerts, load_alert_history, AlertSender


class TestCheckAlerts:
    def setup_method(self):
        self.portfolio = {
            "600900": {"name": "长江电力"},
            "601398": {"name": "工商银行"},
        }
        self.thresholds = {
            "price_drop": 8.0,
            "div_change": 0.5,
            "kelly_change": 5.0,
            "lishing_drop": 10.0,
        }

    def test_price_drop_alert(self):
        cur = {"600900": {"chg": -10.0, "price": 20.0}}
        alerts = check_alerts(self.portfolio, cur, {}, {}, self.thresholds)
        assert len(alerts) == 1
        assert alerts[0]["type"] == "股价跌幅"

    def test_no_alert_normal(self):
        cur = {"600900": {"chg": -2.0, "dy": 4.0, "kelly_final": 15}}
        prev = {"600900": {"dy": 4.0, "kelly_final": 15}}
        alerts = check_alerts(self.portfolio, cur, prev, {}, self.thresholds)
        assert len(alerts) == 0

    def test_kelly_change_alert(self):
        cur = {"600900": {"chg": 0, "kelly_final": 20}}
        prev = {"600900": {"kelly_final": 10}}
        alerts = check_alerts(self.portfolio, cur, prev, {}, self.thresholds)
        assert any(a["type"] == "Kelly变化" for a in alerts)


class TestAlertHistory:
    def test_log_and_load(self, tmp_path, monkeypatch):
        log_file = tmp_path / "alerts_log.csv"
        monkeypatch.setattr("modules.alerts.LOG_PATH", str(log_file))
        alerts = [{"code": "600900", "name": "长江电力", "type": "跌幅", "msg": "test"}]
        log_alerts(alerts)
        history = load_alert_history()
        assert not history.empty
        assert str(history.iloc[0]["code"]) == "600900"


class TestAlertSender:
    def test_sender_init(self):
        sender = AlertSender()
        assert isinstance(sender.cfg, dict)

    def test_sender_update_cfg(self, tmp_path, monkeypatch):
        config_file = tmp_path / "alert_config.json"
        monkeypatch.setattr("modules.alerts.CONFIG_PATH", str(config_file))
        sender = AlertSender()
        sender.update_cfg(smtp_server="test.com")
        assert sender.cfg["smtp_server"] == "test.com"

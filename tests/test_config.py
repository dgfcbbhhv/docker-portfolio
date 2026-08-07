"""配置加载单元测试"""
import json
import pytest

from modules import config as cfg_module


def test_defaults_loaded():
    assert "600900" in cfg_module.PORT_A
    assert cfg_module.MAX_TOTAL == 80.0
    assert cfg_module.ALERT_THRESHOLDS["price_drop"] == 8.0


def test_reload_from_temp_config(tmp_path, monkeypatch):
    tmp_cfg = tmp_path / "config.json"
    tmp_cfg.write_text(json.dumps({
        "kelly": {"max_total": 60.0},
        "portfolio": {"A": {"600900": {"name": "长江电力", "ratio": 100}}},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(cfg_module, "CONFIG_PATH", str(tmp_cfg))
    cfg_module.reload()
    try:
        assert cfg_module.MAX_TOTAL == 60.0
        assert cfg_module.PORT_A == {"600900": {"name": "长江电力", "ratio": 100}}
    finally:
        monkeypatch.setattr(cfg_module, "CONFIG_PATH",
                            str(cfg_module._ROOT + "/config.json"))
        cfg_module.reload()

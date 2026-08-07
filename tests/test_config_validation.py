import copy

from modules.config import get_config
from modules.config_validation import validate_config, validate_env


def _deep_copy_config():
    return copy.deepcopy(get_config())


def test_current_config_has_no_errors():
    errors, _ = validate_config(_deep_copy_config())
    assert errors == []


def test_warns_when_ratio_sum_not_100():
    cfg = _deep_copy_config()
    cfg["portfolio"]["A"]["600900"]["ratio"] = 50
    _, warnings = validate_config(cfg)
    assert any("A 层 ratio 合计" in w for w in warnings)


def test_invalid_invested_cap_is_error():
    cfg = _deep_copy_config()
    cfg["layers"]["A"]["invested_cap"] = 120
    errors, _ = validate_config(cfg)
    assert any("invested_cap" in e for e in errors)


def test_duplicate_sequence_is_error():
    cfg = _deep_copy_config()
    cfg["layers"]["A"]["sequence"] = 2
    errors, _ = validate_config(cfg)
    assert any("sequence" in e for e in errors)


def test_env_warns_when_channel_partially_configured():
    env = {"SMTP_SERVER": "smtp.example.com", "SMTP_PORT": "465",
           "SMTP_USER": "u", "ALERT_TO": "a@b.com"}
    warnings = validate_env(env)
    assert any("SMTP_PASS" in w for w in warnings)


def test_env_silent_when_channel_not_configured():
    assert validate_env({}) == []


def test_env_silent_when_channel_complete():
    env = {"SMTP_SERVER": "s", "SMTP_PORT": "465", "SMTP_USER": "u",
           "SMTP_PASS": "p", "ALERT_TO": "a@b.com"}
    assert validate_env(env) == []

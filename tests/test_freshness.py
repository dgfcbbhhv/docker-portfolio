"""数据新鲜度门禁单元测试"""
import datetime
import json

import pandas as pd
import pytest

from modules.data import freshness


def test_freshness_issues_flags_old_spot(monkeypatch):
    ages = {"spot": 3600 * 3, "fin": 3600 * 24, "div": 3600 * 24}
    monkeypatch.setattr(freshness, "_max_age_seconds",
                        lambda cat: ages.get(cat))
    cfg = {"spot_max_age_minutes": 90, "fin_max_age_days": 7,
           "div_max_age_days": 30}
    issues, status = freshness.freshness_issues(cfg)
    assert any("行情缓存" in i for i in issues)
    assert status["categories"]["spot"]["max_age_seconds"] == 3600 * 3


def test_freshness_ok(monkeypatch):
    ages = {"spot": 60, "fin": 3600 * 24, "div": 3600 * 24}
    monkeypatch.setattr(freshness, "_max_age_seconds",
                        lambda cat: ages.get(cat))
    cfg = {"spot_max_age_minutes": 90, "fin_max_age_days": 7,
           "div_max_age_days": 30}
    issues, _ = freshness.freshness_issues(cfg)
    assert issues == []


def test_freshness_flags_old_hist(monkeypatch):
    ages = {"spot": 60, "fin": 3600 * 24, "div": 3600 * 24,
            "hist": 86400 * 30}
    monkeypatch.setattr(freshness, "_max_age_seconds",
                        lambda cat: ages.get(cat))
    cfg = {"spot_max_age_minutes": 90, "fin_max_age_days": 7,
           "div_max_age_days": 30, "hist_max_age_days": 7}
    issues, _ = freshness.freshness_issues(cfg)
    assert any("历史行情缓存" in i for i in issues)


def test_load_status_reads_saved(monkeypatch, tmp_path):
    import os
    fake_path = os.path.join(tmp_path, "data_status.json")
    monkeypatch.setattr(freshness, "STATUS_PATH", fake_path)
    with open(fake_path, "w", encoding="utf-8") as f:
        json.dump({"collected_at": "x"}, f)
    assert freshness.load_status() == {"collected_at": "x"}


def test_load_status_missing_returns_none(monkeypatch, tmp_path):
    import os
    fake_path = os.path.join(tmp_path, "data_status.json")
    monkeypatch.setattr(freshness, "STATUS_PATH", fake_path)
    assert freshness.load_status() is None


def test_flag_rows_marks_manual_and_existing():
    df = pd.DataFrame([
        {"code": "600900", "name": "长江电力", "data_flags": "price_invalid;",
         "manual_dividend": False, "manual_price": False},
        {"code": "AAPL", "name": "苹果", "data_flags": "",
         "manual_dividend": True, "manual_price": True},
    ])
    out = freshness.flag_rows(df)
    assert "price_invalid" in out.iloc[0]["data_flags"]
    assert "manual_dividend" in out.iloc[1]["data_flags"]
    assert "manual_price" in out.iloc[1]["data_flags"]

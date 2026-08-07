"""分红到账登记单元测试"""
import pytest

import modules.dividend_log as dl


def _tmp_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dl, "DIVIDEND_PATH", str(tmp_path / "dividend_log.json"))


def test_add_and_summary(tmp_path, monkeypatch):
    _tmp_paths(tmp_path, monkeypatch)
    dl.add_dividend("600036", "招商银行", "2026-07-10", 120.50, "中期分红")
    dl.add_dividend("600036", "招商银行", "2026-08-03", 130.25, "")

    summary = dl.dividend_summary()
    assert summary["count"] == 2
    assert summary["total_income"] == pytest.approx(250.75, abs=0.01)
    assert summary["by_code"]["600036"]["count"] == 2

    rows = dl.get_dividend_log()
    assert rows[0]["amount"] == 130.25
    assert rows[1]["pay_date"] == "2026-07-10"


def test_add_requires_valid_input(tmp_path, monkeypatch):
    _tmp_paths(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        dl.add_dividend("", "", "2026-07-10", 100)
    with pytest.raises(ValueError):
        dl.add_dividend("600036", "招商银行", "2026-07-10", -1)

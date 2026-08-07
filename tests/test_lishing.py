"""李氏评分引擎单元测试"""
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from modules.lishing import calc, grade_label, load_note, save_note, DIMENSIONS


class TestGradeLabel:
    def test_extremely_safe(self):
        assert "极度安全" in grade_label(90)

    def test_meets_lishing(self):
        assert "符合李氏" in grade_label(75)

    def test_cautious(self):
        assert "谨慎" in grade_label(60)

    def test_high_risk(self):
        assert "高风险" in grade_label(40)


class TestLishingCalc:
    def test_redline_audit_fail(self):
        row = {"code": "000001", "audit": False}
        result = calc(row)
        assert result["total"] == 0
        assert result["status"] == "red"
        assert "审计" in result["msg"]

    def test_redline_st(self):
        row = {"code": "000001", "name": "*ST某某", "audit": True}
        result = calc(row)
        assert result["total"] == 0
        assert result["status"] == "red"

    def test_normal_stock(self):
        row = {
            "code": "600900",
            "pe": 12,
            "pb": 0.8,
            "debt": 45,
            "roe": 18,
            "dy": 4.5,
            "div_years": 5,
            "ocf_ratio": 120,
            "mcap": 5000,
            "audit": True,
        }
        result = calc(row)
        assert result["total"] > 70
        assert result["status"] in ("normal", "warning")
        assert result["actual_weight"] > 0

    def test_missing_data_weight_reclaim(self):
        row = {
            "code": "600900",
            "pe": 12,
            "audit": True,
        }
        result = calc(row)
        assert result["actual_weight"] < 100
        assert len(result["missing_dims"]) > 0

    def test_low_pe_scores_high(self):
        row = {"pe": 10, "audit": True}
        result = calc(row)
        pe_detail = result["details"].get("pe", {})
        assert pe_detail.get("raw") == 1.0

    def test_high_debt_scores_low(self):
        row = {"pe": 15, "debt": 80, "audit": True}
        result = calc(row)
        debt_detail = result["details"].get("debt", {})
        assert debt_detail.get("raw") == 0.0


class TestNoteManagement:
    def test_save_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modules.lishing.NOTES_DIR", str(tmp_path))
        save_note("TEST01", {"rpt_ratio": 5.0, "mgmt": "good"})
        data = load_note("TEST01")
        assert data["rpt_ratio"] == 5.0
        assert data["mgmt"] == "good"

    def test_load_nonexistent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modules.lishing.NOTES_DIR", str(tmp_path))
        data = load_note("NONEXISTENT")
        assert data == {}

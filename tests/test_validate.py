import pytest
import modules.data.validate as v


def _mk(d):
    return lambda codes: dict(d)


def test_strip_prefix():
    assert v._strip_prefix("sh600900") == "600900"
    assert v._strip_prefix("sz000001") == "000001"
    assert v._strip_prefix("hk00700") == "00700"
    assert v._strip_prefix("rt_hk09988") == "09988"
    assert v._strip_prefix("AAPL") == "AAPL"


def test_validate_ok(monkeypatch):
    monkeypatch.setattr(v, "_SOURCES", {
        "A": ("sina", _mk({"600900": 10.00}), "tx", _mk({"600900": 10.03}), 1.0),
    })
    out = v.validate_spot_by_market({"A": ["600900"]})
    assert out["summary"]["total"] == 1
    assert out["summary"]["ok"] == 1
    assert out["summary"]["mismatch"] == 0
    assert out["rows"][0]["status"] == "ok"
    assert abs(out["rows"][0]["diff_pct"] - 0.3) < 0.01
    assert out["summary"]["coverage"] == 1.0


def test_validate_mismatch(monkeypatch):
    monkeypatch.setattr(v, "_SOURCES", {
        "A": ("sina", _mk({"600900": 12.0}), "tx", _mk({"600900": 10.0}), 1.0),
    })
    out = v.validate_spot_by_market({"A": ["600900"]})
    assert out["rows"][0]["status"] == "mismatch"
    assert out["summary"]["mismatch"] == 1
    assert out["summary"]["ok"] == 0
    assert out["rows"][0]["diff_pct"] == 20.0


def test_validate_missing_src(monkeypatch):
    monkeypatch.setattr(v, "_SOURCES", {
        "A": ("sina", _mk({"600900": 10.0}), "tx", _mk({}), 1.0),
    })
    out = v.validate_spot_by_market({"A": ["600900"]})
    assert len(out["rows"]) == 1
    assert out["rows"][0]["status"] == "only_s1"
    assert out["summary"]["ok"] == 0


def test_validate_sanity_range():
    issues = v.validate_sanity({"A": ["600900"]}, lambda c, m: 999.0)
    assert len(issues) == 1
    assert "股息率" in issues[0]
    clean = v.validate_sanity({"A": ["600900"]}, lambda c, m: 6.5)
    assert clean == []
    none_ok = v.validate_sanity({"A": ["600900"]}, lambda c, m: None)
    assert none_ok == []


def test_validate_recheck_resolves(monkeypatch):
    monkeypatch.setattr(v, "_SOURCES", {
        "A": ("sina", _mk({"600900": 12.0}), "tx", _mk({"600900": 10.0}), 1.0),
    })
    monkeypatch.setattr(v, "_recheck", lambda code, mkt: (10.02, 10.00))
    out = v.validate_spot_by_market({"A": ["600900"]})
    assert out["rows"][0]["status"] == "ok(recheck)"
    assert out["summary"]["ok"] == 1
    assert out["summary"]["mismatch"] == 0
    assert out["rows"][0]["diff_pct"] == 0.2


def test_validate_recheck_confirms_mismatch(monkeypatch):
    monkeypatch.setattr(v, "_SOURCES", {
        "A": ("sina", _mk({"600900": 12.0}), "tx", _mk({"600900": 10.0}), 1.0),
    })
    monkeypatch.setattr(v, "_recheck", lambda code, mkt: (12.5, 10.0))
    out = v.validate_spot_by_market({"A": ["600900"]})
    assert out["rows"][0]["status"] == "mismatch"
    assert out["summary"]["mismatch"] == 1
    assert out["summary"]["ok"] == 0


def test_validate_recheck_fetch_fail_keeps_mismatch(monkeypatch):
    monkeypatch.setattr(v, "_SOURCES", {
        "A": ("sina", _mk({"600900": 12.0}), "tx", _mk({"600900": 10.0}), 1.0),
    })
    monkeypatch.setattr(v, "_recheck", lambda code, mkt: (None, None))
    out = v.validate_spot_by_market({"A": ["600900"]})
    assert out["rows"][0]["status"] == "mismatch"


def test_adjudicate_marks_suspect(monkeypatch):
    monkeypatch.setattr(v, "_SOURCES", {
        "A": ("src1", _mk({"600900": 30.96}), "src2", _mk({"600900": 31.33}), 1.0),
    })
    monkeypatch.setattr(v, "_AUTHORITY", {
        "A": ("auth", lambda codes: {"600900": 31.33}, 1.0),
    })
    monkeypatch.setattr(v, "_recheck", lambda code, mkt: (30.96, 31.33))
    out = v.validate_spot_by_market({"A": ["600900"]})
    r = out["rows"][0]
    assert r["status"] == "mismatch"
    assert r["suspect"] == "src1"
    assert r["authority"] == "auth"
    assert r["ap"] == 31.33


def test_adjudicate_no_suspect_when_both_dev(monkeypatch):
    monkeypatch.setattr(v, "_SOURCES", {
        "A": ("src1", _mk({"600900": 30.0}), "src2", _mk({"600900": 32.0}), 1.0),
    })
    monkeypatch.setattr(v, "_AUTHORITY", {
        "A": ("auth", lambda codes: {"600900": 31.0}, 1.0),
    })
    monkeypatch.setattr(v, "_recheck", lambda code, mkt: (30.0, 32.0))
    out = v.validate_spot_by_market({"A": ["600900"]})
    assert out["rows"][0]["suspect"] == "src1"


def test_validate_empty():
    out = v.validate_spot_by_market({})
    assert out["rows"] == []
    assert out["summary"]["total"] == 0
    assert out["summary"]["coverage"] == 0.0
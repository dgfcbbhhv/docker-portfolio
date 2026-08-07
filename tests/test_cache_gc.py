"""缓存 GC（过期/孤儿清理）单元测试"""
import datetime
import os

import pandas as pd

from modules.data import cache


def test_gc_stale_removes_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_DIR", str(tmp_path))
    cat_dir = os.path.join(tmp_path, "spot")
    os.makedirs(cat_dir, exist_ok=True)

    def _write(key, age_hours):
        p = cache._p("spot", key)
        df = pd.DataFrame([{"price": 1.0}])
        df["_ts"] = (datetime.datetime.now()
                     - datetime.timedelta(hours=age_hours)).isoformat()
        df.to_parquet(p, index=False)

    _write("fresh_key", age_hours=0.1)
    _write("stale_key", age_hours=48)  # spot TTL 1h

    removed = cache.gc_stale(ttl_multiplier=2.0)
    assert removed == 1
    assert os.path.exists(cache._p("spot", "fresh_key"))
    assert not os.path.exists(cache._p("spot", "stale_key"))


def test_gc_orphans_removes_unused(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_DIR", str(tmp_path))

    def _write(cat, key):
        p = cache._p(cat, key)
        df = pd.DataFrame([{"v": 1.0}])
        df["_ts"] = datetime.datetime.now().isoformat()
        df.to_parquet(p, index=False)

    _write("fin", "600900")
    _write("fin", "000001")

    removed = cache.gc_orphans({"fin": {"600900"}})
    assert removed == 1
    assert os.path.exists(cache._p("fin", "600900"))
    assert not os.path.exists(cache._p("fin", "000001"))


def test_active_keys_covers_portfolio(monkeypatch):
    cfg = {
        "portfolio": {
            "A": {"600900": {}, "601398": {}},
            "HK": {"00002": {}},
            "US": {"AAPL": {}},
            "REIT": {"508098": {}},
        },
        "watch_list": {},
    }
    keys = cache.active_keys(cfg)
    assert "600900_div" in keys["div"]
    assert "00002_div_hk" in keys["div"]
    assert "AAPL_div_us" in keys["div"]
    assert "508098_div_reit" in keys["div"]
    assert "600900" in keys["fin"]
    assert "hk_00002" in keys["fin"]
    assert "a_all" in keys["spot"]

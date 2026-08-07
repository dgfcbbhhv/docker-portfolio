"""缓存层单元测试"""
import pytest
import sys
import os
import pandas as pd
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from modules.data.cache import load, save, clear_cache, validate_spot


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("modules.data.cache._CACHE_DIR", str(tmp_path))
    return tmp_path


class TestCacheOperations:
    def test_save_and_load(self, cache_dir):
        df = pd.DataFrame({"code": ["600900"], "price": [25.0]})
        save("spot", "test_key", df)
        loaded = load("spot", "test_key")
        assert loaded is not None
        assert loaded.iloc[0]["code"] == "600900"

    def test_load_nonexistent(self, cache_dir):
        result = load("spot", "nonexistent")
        assert result is None

    def test_clear_category(self, cache_dir):
        df = pd.DataFrame({"code": ["600900"]})
        save("spot", "key1", df)
        save("spot", "key2", df)
        clear_cache("spot")
        assert load("spot", "key1") is None
        assert load("spot", "key2") is None

    def test_clear_all(self, cache_dir):
        df = pd.DataFrame({"code": ["600900"]})
        save("spot", "key1", df)
        save("fin", "key2", df)
        clear_cache()
        assert load("spot", "key1") is None
        assert load("fin", "key2") is None


class TestValidateSpot:
    def test_valid_data(self):
        df = pd.DataFrame({
            "price": [10.0, 20.0],
            "pe": [15.0, 20.0],
            "pb": [1.5, 2.0],
            "mcap": [100, 200],
        })
        result = validate_spot(df)
        assert "_flags" in result.columns
        assert all(f == "" for f in result["_flags"])

    def test_invalid_price(self):
        df = pd.DataFrame({
            "price": [0, 10.0],
            "pe": [15.0, 20.0],
            "pb": [1.5, 2.0],
            "mcap": [100, 200],
        })
        result = validate_spot(df)
        assert "price_invalid" in result["_flags"].iloc[0]

    def test_extreme_pe(self):
        df = pd.DataFrame({
            "price": [10.0],
            "pe": [600],
            "pb": [1.5],
            "mcap": [100],
        })
        result = validate_spot(df)
        assert "pe_extreme" in result["_flags"].iloc[0]

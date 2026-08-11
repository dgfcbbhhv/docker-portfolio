"""market.py 乱码列名回归测试。"""
import pandas as pd
import pytest


def test_fetch_hist_uses_cn_close_fallback(monkeypatch):
    """列名非 close(如 akshare 中文列名) 时, 应回退中文 '收盘' 而非乱码键。"""
    from modules.data import market

    df = pd.DataFrame({"date": ["2026-01-01", "2026-01-02"],
                       "开盘": [10.0, 10.2], "收盘": [10.1, 10.3]})

    def _fake_hist_em_or_tx(code, start, end):
        return df

    def _fake_retry(fn, *a, **kw):
        return fn()

    monkeypatch.setattr(market, "_fetch_hist_em_or_tx", _fake_hist_em_or_tx)
    monkeypatch.setattr(market, "retry", _fake_retry)
    monkeypatch.setattr(market, "save", lambda *a, **kw: None)
    monkeypatch.setattr(market, "load", lambda *a, **kw: None)

    vol = market.fetch_vol("600036", "A")
    assert vol is not None and vol > 0


def test_market_py_has_no_mojibake_key():
    """源码不应残留 GBK 乱码列名(鏀剁洏 等)。"""
    src = open("modules/data/market.py", encoding="utf-8").read()
    assert "鏀剁洏" not in src
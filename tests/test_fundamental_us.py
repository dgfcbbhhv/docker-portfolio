"""美股基本面（yfinance）字段抽取测试。"""
import pytest

import modules.data.fundamental as fd


class _FakeCF:
    def __init__(self, ocf, ni):
        self._d = {"Operating Cash Flow": ocf, "Net Income": ni}

    @property
    def empty(self):
        return False

    @property
    def loc(self):
        class _Loc:
            def __init__(self, parent):
                self._p = parent

            def __getitem__(self, row):
                if row not in self._p._d:
                    raise KeyError(row)
                val = self._p._d[row]

                class _ILoc:
                    def __getitem__(self, _):
                        return val

                class _R:
                    @property
                    def iloc(self):
                        return _ILoc()

                return _R()
        return _Loc(self)


class _FakeTicker:
    def __init__(self, info, cf):
        self._info = info
        self._cf = cf

    @property
    def info(self):
        return self._info

    @property
    def cashflow(self):
        return self._cf


def _mk_info(**kw):
    base = {
        "trailingPE": 28.0, "priceToBook": 8.0, "returnOnEquity": 0.35,
        "trailingEps": 6.2, "bookValue": 20.0, "sharesOutstanding": 15e9,
        "debtToEquity": 60.0,
    }
    base.update(kw)
    return base


def test_fetch_one_fin_us(monkeypatch):
    def fake_yf():
        class _YF:
            def Ticker(self, code):
                return _FakeTicker(_mk_info(), _FakeCF(1.1e11, 9.5e10))
        return _YF()
    monkeypatch.setattr("modules.data.market_us._get_yf", fake_yf)
    code, info = fd._fetch_one_fin_us("AAPL")
    assert code == "AAPL"
    assert info["pe"] == 28.0
    assert info["pb"] == 8.0
    assert info["roe"] == 35.0          # 0.35 -> 35%
    assert info["eps"] == 6.2
    assert info["bps"] == 20.0
    assert info["total_shares"] == 15e9
    # debtToEquity=60% -> 负债率 60/160 = 37.5%
    assert abs(info["debt"] - 37.5) < 0.01
    # ocf_ratio = 1.1e11 / 9.5e10 *100 ≈ 115.8%
    assert abs(info["ocf_ratio"] - 115.79) < 0.2


def test_fetch_one_fin_us_missing_fields(monkeypatch):
    def fake_yf():
        class _YF:
            def Ticker(self, code):
                return _FakeTicker(_mk_info(trailingPE=None, priceToBook=None,
                                            returnOnEquity=None, debtToEquity=None),
                                   _FakeCF(None, 1))
        return _YF()
    monkeypatch.setattr("modules.data.market_us._get_yf", fake_yf)
    code, info = fd._fetch_one_fin_us("MSFT")
    assert info["pe"] is None
    assert info["pb"] is None
    assert info["roe"] is None
    assert info["debt"] is None


def test_fetch_one_fin_us_yf_missing_fmp_fallback(monkeypatch):
    """yfinance 不可用时降级 FMP 兜底，FMP 有数据则返回真实字段。"""
    monkeypatch.setattr("modules.data.market_us._get_yf", lambda: None)
    monkeypatch.setattr("modules.data.fmp.fetch_us_finance",
                        lambda code: {"pe": 37.82, "roe": 76.33})
    code, info = fd._fetch_one_fin_us("NVDA")
    assert info["pe"] is not None


def test_fetch_one_fin_us_yf_and_fmp_missing(monkeypatch):
    """yfinance 与 FMP 均不可用时，字段应保持 None，不推断。"""
    monkeypatch.setattr("modules.data.market_us._get_yf", lambda: None)
    monkeypatch.setattr("modules.data.fmp.fetch_us_finance",
                        lambda code: {})
    code, info = fd._fetch_one_fin_us("NVDA")
    assert info["pe"] is None
    assert info["roe"] is None
"""数据源字段 schema 测试。"""
import modules.data.fields as f


def test_secid_a():
    assert f.eastmoney_secid("A", "600938") == "1.600938"
    assert f.eastmoney_secid("A", "000001") == "0.000001"


def test_secid_hk():
    assert f.eastmoney_secid("HK", "00700") == "116.00700"


def test_price_scales():
    assert f.eastmoney_price_scale("A") == 100
    assert f.eastmoney_price_scale("HK") == 1000


def test_decode_a():
    # A股 f43=3133 -> 31.33
    assert abs(f.decode_eastmoney_price("A", 3133) - 31.33) < 1e-9


def test_decode_hk():
    # 港股 f43=77600 -> 77.6
    assert abs(f.decode_eastmoney_price("HK", 77600) - 77.6) < 1e-9


def test_decode_invalid():
    assert f.decode_eastmoney_price("A", None) == 0.0
    assert f.decode_eastmoney_price("A", "abc") == 0.0
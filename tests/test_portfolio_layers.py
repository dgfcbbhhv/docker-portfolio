from modules.portfolio_engine import apply_layer_caps

LAYERS = {
    "A": {"sequence": 1, "enabled": True, "invested_cap": 80, "max_drawdown": 10},
    "HK": {"sequence": 2, "enabled": True, "invested_cap": 60, "max_drawdown": 8},
}


def _basic_rows():
    return [
        {"group": "A股持仓", "ratio": 25, "kelly_factor_adj": 5.0},
        {"group": "A股持仓", "ratio": 25, "kelly_factor_adj": 15.0},
        {"group": "港股持仓", "ratio": 30, "kelly_factor_adj": 20.0},
    ]


def test_normalizes_ratio_and_weight():
    rows, _ = apply_layer_caps(_basic_rows(), LAYERS)
    a = [r for r in rows if r["layer"] == "A"]
    assert [r["ratio_norm"] for r in a] == [50.0, 50.0]
    assert [r["weight"] for r in a] == [40.0, 40.0]


def test_caps_each_layer_independently():
    rows = [
        {"group": "A股持仓", "ratio": 25, "kelly_factor_adj": 50.0},
        {"group": "A股持仓", "ratio": 25, "kelly_factor_adj": 50.0},
        {"group": "港股持仓", "ratio": 30, "kelly_factor_adj": 40.0},
        {"group": "港股持仓", "ratio": 30, "kelly_factor_adj": 40.0},
    ]
    rows, summaries = apply_layer_caps(rows, LAYERS)
    assert round(sum(r["kelly_portfolio_adj"] for r in rows if r["layer"] == "A"), 1) == 80.0
    assert round(sum(r["kelly_portfolio_adj"] for r in rows if r["layer"] == "HK"), 1) == 60.0
    by_layer = {s["layer"]: s for s in summaries}
    assert by_layer["A"]["cash"] == 0.0
    assert by_layer["HK"]["cash"] == 0.0


def test_derives_layer_from_group_label():
    rows, _ = apply_layer_caps(
        [{"group": "港股持仓", "ratio": 10, "kelly_factor_adj": 1.0}],
        LAYERS,
    )
    assert rows[0]["layer"] == "HK"

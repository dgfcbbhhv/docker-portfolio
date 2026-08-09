"""分层压力测试与回撤验收。

原则：
- 单日暴跌和行业崩溃只依赖层内目标权重，可直接计算
- 蒙特卡洛回撤依赖真实波动率；港股/美股/REIT 数据不足时标记为不可评估
- 不把 20% 默认波动率当成真实数据

基础计算委托 modules.risk（与 stress 共用，避免两套实现漂移）。
"""

from modules.risk import (
    CORR_BASE,
    HORIZON_DAYS,
    N_SIMS,
    PLACEHOLDER_VOL,
    industry_collapse_loss,
    industry_of,
    monte_carlo_p95_drawdown,
    single_day_loss,
    vol_is_usable,
)


def _rows_for_layer(df, layer):
    rows = []
    sub = df[df["layer"] == layer]
    for _, r in sub.iterrows():
        weight = float(r.get("weight") or 0)
        vol = float(r.get("vol") or 0)
        rows.append({
            "code": r.get("code"),
            "name": r.get("name"),
            "weight": weight,
            "vol": vol,
            "industry": industry_of(str(r.get("code", ""))),
        })
    return rows


def build_layer_risk_report(df, layers):
    if df is None or df.empty:
        return []
    report = []
    for layer in sorted(layers, key=lambda k: layers[k].get("sequence", 99)):
        if layer not in set(df["layer"].tolist()):
            continue
        rows = _rows_for_layer(df, layer)
        for r in rows:
            r["_layer"] = layer
        cfg = layers.get(layer, {})
        invested_cap = float(cfg.get("invested_cap", 80.0) or 80.0)
        max_drawdown = float(cfg.get("max_drawdown", 10.0) or 10.0)
        weight_sum = round(sum(r["weight"] for r in rows), 1)
        if not rows or weight_sum <= 0:
            report.append({
                "layer": layer,
                "sequence": cfg.get("sequence", 1),
                "invested_cap": invested_cap,
                "max_drawdown": max_drawdown,
                "single_day_10": None,
                "single_day_20": None,
                "worst_industry": None,
                "mc_p95_drawdown": None,
                "verdict": "数据不足",
                "note": "层内无有效持仓权重",
            })
            continue
        vol_ok = all(vol_is_usable(r["vol"], layer) for r in rows)
        mc = monte_carlo_p95_drawdown(rows) if vol_ok else None
        if mc is None:
            verdict = "数据不足"
            note = "波动率数据不足，未生成蒙特卡洛回撤"
        else:
            verdict = "通过" if mc <= max_drawdown else "超线"
            note = ""
        report.append({
            "layer": layer,
            "sequence": cfg.get("sequence", 1),
            "invested_cap": invested_cap,
            "max_drawdown": max_drawdown,
            "single_day_10": single_day_loss(rows, -10.0),
            "single_day_20": single_day_loss(rows, -20.0),
            "worst_industry": industry_collapse_loss(rows),
            "mc_p95_drawdown": mc,
            "verdict": verdict,
            "note": note,
        })
    return report

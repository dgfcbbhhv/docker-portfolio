"""纯计算引擎：不依赖 Streamlit，供 App 与后台预警进程共用同一套口径。"""
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

from modules.kelly import run as kelly_run, apply_portfolio_cap
from modules.lishing import calc as lishing_calc
from modules.signals import evaluate_growth


def _default_factor_multiplier(code):
    try:
        from modules.factor_selector import get_kelly_multiplier
        return get_kelly_multiplier(code)
    except Exception:
        return 1.0


def _layer_for_label(label):
    if not label:
        return "A"
    if "港股" in label:
        return "HK"
    if "美股" in label:
        return "US"
    if "REIT" in label:
        return "REIT"
    return "A"


def _ratio_sum(portfolio):
    return sum(float(cfg.get("ratio", 0) or 0) for cfg in portfolio.values())


def apply_layer_caps(rows, layers=None):
    if layers is None:
        try:
            from modules.config import LAYERS
            layers = LAYERS
        except Exception:
            layers = {}
    grouped = {}
    for row in rows:
        layer = row.get("layer") or _layer_for_label(row.get("group", ""))
        grouped.setdefault(layer, []).append(row)
    all_rows = []
    summaries = []
    for layer, group in grouped.items():
        cfg = layers.get(layer, {})
        invested_cap = float(cfg.get("invested_cap", 80.0) or 80.0)
        max_drawdown = float(cfg.get("max_drawdown", 10.0) or 10.0)
        sequence = cfg.get("sequence", 1)
        enabled = bool(cfg.get("enabled", True))
        ratio_sum = sum(float(r.get("ratio", 0) or 0) for r in group)
        for r in group:
            ratio = float(r.get("ratio", 0) or 0)
            ratio_norm = round(ratio / ratio_sum * 100, 1) if ratio_sum > 0 else 0.0
            r["ratio_norm"] = ratio_norm
            r["layer"] = layer
            r["layer_sequence"] = sequence
            r["layer_enabled"] = enabled
            r["layer_invested_cap"] = invested_cap
            r["layer_max_drawdown"] = max_drawdown
            r["weight"] = round(float(ratio_norm or 0) * invested_cap / 100.0, 1)
        group, total_before, scale = apply_portfolio_cap(group, invested_cap)
        total_after = round(float(sum(r.get("kelly_portfolio_adj", 0) or 0 for r in group)), 1)
        cash = round(max(0.0, invested_cap - total_after), 1)
        for r in group:
            r["layer_cash"] = cash
            r["layer_kelly_total"] = total_after
        all_rows.extend(group)
        summaries.append({
            "layer": layer,
            "sequence": sequence,
            "enabled": enabled,
            "invested_cap": invested_cap,
            "max_drawdown": max_drawdown,
            "kelly_before_cap": round(total_before, 1),
            "kelly_after_cap": total_after,
            "cash": cash,
            "scale": round(float(scale), 4),
        })
    summaries.sort(key=lambda s: (s.get("sequence", 99), s["layer"]))
    return all_rows, summaries


def build_rows(portfolio, fins, divs, vols, spot, label,
               manual_div_yields=None, manual_prices=None, manual_us_growth=None,
               frac=0.25, maxpos=30.0, factor_multiplier_fn=None,
               earnings=None):
    manual_div_yields = manual_div_yields or {}
    vols = vols or {}
    manual_prices = manual_prices or {}
    manual_us_growth = manual_us_growth or {}
    earnings = earnings or {}
    multiplier = factor_multiplier_fn or _default_factor_multiplier
    layer = _layer_for_label(label)
    ratio_sum = _ratio_sum(portfolio)
    rows = []

    for code, cfg in portfolio.items():
        s = spot[spot["c"] == code]
        if s.empty:
            continue
        s = s.iloc[0]
        fi = fins.get(code, {})
        ear = earnings.get(code, {}) or {}
        vol = vols.get(code, 0.2)
        div_info = divs.get(code, {"ttm": 0, "lfy": 0})
        if isinstance(div_info, dict):
            ttm_div = div_info.get("ttm", 0) or 0
            lfy_div = div_info.get("lfy", 0) or 0
        else:
            ttm_div = float(div_info) if div_info else 0
            lfy_div = ttm_div

        p = float(s["price"]) if pd.notna(s.get("price")) and s.get("price", 0) > 0 else 0
        manual_price = False
        if p <= 0 and code in manual_prices:
            p = manual_prices[code]
            s["price"] = p
            manual_price = True

        _ov = manual_div_yields.get(code, {})
        auto_div_available = (ttm_div > 0 or lfy_div > 0)
        # 优先级：自动拉取的每股分红 > manual_div_yields 兜底
        manual_dividend = False
        if not auto_div_available and _ov:
            # API 无数据时，用 config.json 的手动股息率兜底
            manual_dividend = True
            if p > 0:
                if "ttm" in _ov:
                    ttm_div = _ov["ttm"] * p / 100
                if "lfy" in _ov:
                    lfy_div = _ov["lfy"] * p / 100
            dy_ttm = _ov.get("ttm", 0) or _ov.get("lfy", 0) or 0.0
            dy_lfy = _ov.get("lfy", 0) or _ov.get("ttm", 0) or 0.0
        else:
            dy_ttm = round((ttm_div / p * 100), 2) if p > 0 and ttm_div > 0 else 0.0
            dy_lfy = round((lfy_div / p * 100), 2) if p > 0 and lfy_div > 0 else 0.0

        kr = kelly_run(
            p,
            float(s.get("pb")) if pd.notna(s.get("pb")) and s.get("pb") > 0 else None,
            float(s.get("pe")) if pd.notna(s.get("pe")) and s.get("pe") > 0 else None,
            fi.get("roe"), fi.get("eps"), fi.get("debt"), ttm_div, vol, frac, maxpos,
        )
        mcap_val = s.get("mcap", 0) or 0
        if mcap_val <= 0 and fi.get("total_shares") and p > 0:
            mcap_val = round(p * fi["total_shares"] / 1e8, 2)

        growth_factors = []
        if "美股" in label:
            price_above_ma20 = ma20_above_ma60 = price_above_ma60 = ma60_above_ma120 = None
            pe_pctl = pb_pctl = None
            pe_val = s.get("pe") if pd.notna(s.get("pe")) else fi.get("pe")
            pb_val = s.get("pb") if pd.notna(s.get("pb")) else fi.get("pb")
            try:
                from modules.data.market_us import fetch_us_hist
                hist = fetch_us_hist(code, days=250)
                if hist is not None and len(hist) >= 60:
                    closes = hist["close"].values
                    ma20 = np.mean(closes[-20:])
                    ma60 = np.mean(closes[-60:])
                    ma120 = np.mean(closes[-120:]) if len(closes) >= 120 else None
                    price_above_ma20 = p > ma20
                    ma20_above_ma60 = ma20 > ma60
                    price_above_ma60 = p > ma60
                    ma60_above_ma120 = (ma60 > ma120) if ma120 else None
            except Exception:
                pass
            gr_fb = manual_us_growth.get(code, {})
            if pe_val and not pd.isna(pe_val):
                if pe_val < 25:
                    pe_pctl = 20
                elif pe_val < 35:
                    pe_pctl = 40
                elif pe_val < 50:
                    pe_pctl = 60
                elif pe_val < 80:
                    pe_pctl = 75
                else:
                    pe_pctl = 90
            elif gr_fb:
                pe_pctl = gr_fb.get("pe_pctl")
            if price_above_ma20 is None and gr_fb:
                price_above_ma20 = gr_fb.get("price_above_ma20")
            if ma20_above_ma60 is None and gr_fb:
                ma20_above_ma60 = gr_fb.get("ma20_above_ma60")
            if price_above_ma60 is None and gr_fb:
                price_above_ma60 = gr_fb.get("price_above_ma60")
            if ma60_above_ma120 is None and gr_fb:
                ma60_above_ma120 = gr_fb.get("ma60_above_ma120")
            _gr = evaluate_growth(
                kelly_final=kr["kelly_final"], vol_adj_factor=kr.get("vol_adj_factor", 1.0),
                p_signal=kr.get("p_signal"),
                pe_pctl=pe_pctl, pb_pctl=pb_pctl,
                price_above_ma20=price_above_ma20, ma20_above_ma60=ma20_above_ma60,
                price_above_ma60=price_above_ma60, ma60_above_ma120=ma60_above_ma120,
            )
            _fl = [f.get("name", "?") + ":" + str(f.get("score", 0)) + "/" + str(f.get("max", 1))
                   for f in _gr.get("factors", [])]
            ls = {"total": _gr.get("score", 0), "flags": _fl, "use_rpt": False}
            growth_factors = _gr.get("factors", [])
        else:
            ls = lishing_calc({
                "code": code,
                "pe": s.get("pe") if pd.notna(s.get("pe")) else fi.get("pe"),
                "pb": s.get("pb") if pd.notna(s.get("pb")) else fi.get("pb"),
                "debt": fi.get("debt"), "roe": fi.get("roe"), "dy": dy_ttm,
                "div_years": fi.get("div_years", 0), "ocf_ratio": fi.get("ocf_ratio"),
                "mcap": mcap_val, "audit": fi.get("audit", True),
                "earn_flags": ear.get("flags", []),
            }, "HK" if "港股" in label else "A")

        rows.append({
            "code": code, "name": cfg["name"], "group": label, "ratio": cfg["ratio"],
            "layer": layer,
            "ratio_raw": cfg.get("ratio", 0),
            "ratio_norm": round(float(cfg.get("ratio", 0) or 0) / ratio_sum * 100, 1) if ratio_sum > 0 else 0.0,
            "price": p, "chg": s.get("chg", 0),
            "pe": s.get("pe") if pd.notna(s.get("pe")) else fi.get("pe"),
            "pb": s.get("pb") if pd.notna(s.get("pb")) else fi.get("pb"),
            "dy": dy_ttm, "dy_ttm": dy_ttm, "dy_lfy": dy_lfy, "mcap": mcap_val,
            "roe": fi.get("roe"), "vol": round(vol * 100, 1),
            "lishing": ls["total"], "lishing_flags": "; ".join(ls["flags"]),
            "lishing_rpt": ls["use_rpt"], "growth_factors": growth_factors,
            "earn_np_yoy": ear.get("np_yoy"), "earn_rev_yoy": ear.get("rev_yoy"),
            "earn_gross_margin": ear.get("gross_margin"), "earn_net_margin": ear.get("net_margin"),
            "earn_grade": ear.get("grade"), "earn_flags": "; ".join(ear.get("flags", [])),
            **{k: kr[k] for k in ("p_signal", "b_odds", "kelly_raw", "kelly_opt",
                                  "kelly_final", "kelly_vol_adj", "vol_adj_factor")},
            "kelly_factor_adj": min(kr["kelly_vol_adj"] * multiplier(code), maxpos),
            "data_flags": str(s.get("_flags", "") or ""),
            "manual_dividend": manual_dividend,
            "manual_price": manual_price,
        })
    return rows


def build_reit_rows(portfolio, spot, label, manual_div_yields=None, divs=None, vols=None):
    manual_div_yields = manual_div_yields or {}
    vols = vols or {}
    divs = divs or {}
    layer = _layer_for_label(label)
    ratio_sum = _ratio_sum(portfolio)
    rows = []
    for code, cfg in portfolio.items():
        s = spot[spot["c"] == code]
        if s.empty:
            continue
        s = s.iloc[0]
        p = float(s["price"]) if pd.notna(s.get("price")) and s.get("price", 0) > 0 else 0.0
        _ov = manual_div_yields.get(code, {})
        dv = divs.get(code) or {}
        lfy_div = float(dv.get("lfy", 0) or 0)
        ttm_div = float(dv.get("ttm", 0) or 0)
        dy = 0.0
        manual_dividend = bool(_ov)
        if p > 0 and ttm_div > 0:
            dy = round(ttm_div / p * 100, 2)
        elif p > 0 and lfy_div > 0:
            dy = round(lfy_div / p * 100, 2)
        else:
            dy = _ov.get("lfy", 0) or _ov.get("ttm", 0) or 0.0
        rows.append({
            "code": code, "name": cfg["name"], "group": label, "ratio": cfg["ratio"],
            "layer": layer,
            "ratio_raw": cfg.get("ratio", 0),
            "ratio_norm": round(float(cfg.get("ratio", 0) or 0) / ratio_sum * 100, 1) if ratio_sum > 0 else 0.0,
            "price": p, "chg": s.get("chg", 0),
            "pe": None, "pb": None, "dy": dy, "mcap": 0,
            "vol": round(vols.get(code, 0) * 100, 1), "lishing": 0, "lishing_flags": "", "lishing_rpt": False,
            "growth_factors": [],
            "p_signal": 0, "b_odds": 0, "kelly_raw": 0, "kelly_opt": 0,
            "kelly_final": 0, "kelly_vol_adj": 0, "vol_adj_factor": 1.0,
            "kelly_factor_adj": 0,
            "data_flags": str(s.get("_flags", "") or ""),
            "manual_dividend": manual_dividend,
            "manual_price": False,
        })
    return rows


def build_portfolio_df(cfg=None, portfolio_map=None):
    """后台预警专用入口：拉取 A/HK/US/REIT 行情并计算同一套持仓指标。

    Args:
        cfg: 配置对象(兼容旧调用, 可为 None)。
        portfolio_map: 可选覆盖, 形如 {"A": {...}, "HK": {...}, "US": {...}, "REIT": {...}},
            默认使用 config.json 的 portfolio。选股推送用 watch_list 扩展池时传入。

    Returns:
        (DataFrame, meta)  meta 包含各市场是否取到数据。
    """
    from modules.config import (MANUAL_DIV_YIELDS, MANUAL_PRICES, MANUAL_US_GROWTH,
                                FRAC, MAXPOS, MAX_TOTAL)
    from modules.data.market import fetch_spot_a, fetch_spot_hk, fetch_vols
    from modules.data.fundamental import fetch_fins, fetch_divs, fetch_fins_hk, fetch_divs_hk, fetch_divs_us, fetch_divs_reit
    from modules.earnings import fetch_earnings
    from modules.data.market_us import fetch_us_spot
    from modules.data.fund import fetch_market_prices

    if portfolio_map is not None:
        PORT_A = portfolio_map.get("A", {})
        PORT_HK = portfolio_map.get("HK", {})
        PORT_REIT = portfolio_map.get("REIT", {})
        PORT_US = portfolio_map.get("US", {})
    else:
        from modules.config import PORT_A, PORT_HK, PORT_REIT, PORT_US
        PORT_A, PORT_HK, PORT_REIT, PORT_US = PORT_A, PORT_HK, PORT_REIT, PORT_US

    ca = list(PORT_A.keys())
    chk = list(PORT_HK.keys())
    rows = []
    meta = {}

    with ThreadPoolExecutor(max_workers=6) as pool:
        f_spot_a = pool.submit(fetch_spot_a, ca)
        f_fins = pool.submit(fetch_fins, ca)
        f_earn = pool.submit(fetch_earnings, ca)
        f_divs = pool.submit(fetch_divs, ca)
        f_vols = pool.submit(fetch_vols, ca)
        f_spot_hk = pool.submit(fetch_spot_hk, chk)
        f_fins_hk = pool.submit(fetch_fins_hk, chk)
        f_divs_hk = pool.submit(fetch_divs_hk, chk)
        f_vols_hk = pool.submit(fetch_vols, chk, "H")
        f_spot_us = pool.submit(fetch_us_spot, list(PORT_US.keys()))
        f_divs_us = pool.submit(fetch_divs_us, list(PORT_US.keys()))
        sa = f_spot_a.result()
        fa = f_fins.result()
        ea = f_earn.result()
        da = f_divs.result()
        va = f_vols.result()
        sh = f_spot_hk.result()
        fh = f_fins_hk.result()
        dh = f_divs_hk.result()
        vh = f_vols_hk.result()
        su = f_spot_us.result()
        du = f_divs_us.result()

    meta["spot_a"] = not sa.empty
    meta["spot_hk"] = not sh.empty
    meta["spot_us"] = not su.empty

    if not sa.empty:
        rows += build_rows(PORT_A, fa, da, va, sa, "A股持仓",
                           MANUAL_DIV_YIELDS, MANUAL_PRICES, MANUAL_US_GROWTH,
                           FRAC, MAXPOS, earnings=ea)
    if not sh.empty:
        rows += build_rows(PORT_HK, fh, dh, vh, sh, "港股持仓",
                           MANUAL_DIV_YIELDS, MANUAL_PRICES, MANUAL_US_GROWTH,
                           FRAC, MAXPOS)
    if not su.empty:
        rows += build_rows(PORT_US, {}, du, {}, su, "美股",
                           MANUAL_DIV_YIELDS, MANUAL_PRICES, MANUAL_US_GROWTH,
                           FRAC, MAXPOS)

    reit_codes = list(PORT_REIT.keys())
    if reit_codes:
        try:
            reit_mkt = fetch_market_prices(reit_codes)
            reit_divs = fetch_divs_reit(reit_codes)
            reit_vols = fetch_vols(reit_codes)
            reit_df = pd.DataFrame([
                {"c": c, "price": q.get("price", 0), "chg": q.get("chg", 0)}
                for c, q in reit_mkt.items() if q
            ])
            meta["spot_reit"] = not reit_df.empty
            if not reit_df.empty:
                rows += build_reit_rows(PORT_REIT, reit_df, "REIT持仓", MANUAL_DIV_YIELDS, reit_divs, reit_vols)
        except Exception:
            meta["spot_reit"] = False

    rows, layer_summaries = apply_layer_caps(rows)
    meta["layers"] = layer_summaries
    meta["kelly_total_before_cap"] = round(sum(s["kelly_before_cap"] for s in layer_summaries), 1)
    meta["kelly_cap_scale"] = min((s.get("scale") or 1) for s in layer_summaries) if layer_summaries else 1.0

    df = pd.DataFrame(rows)
    if df.empty:
        return df, meta
    o = {"A股持仓": 0, "港股持仓": 2, "美股": 3, "REIT持仓": 4}
    df["_s"] = df["group"].map(o)
    return df.sort_values("_s").drop(columns="_s"), meta


def alert_snapshot(df):
    """把持仓 DataFrame 转成预警进程需要的 {code: {...}} 快照。"""
    snap = {}
    if df is None or df.empty:
        return snap
    for _, r in df.iterrows():
        snap[r["code"]] = {
            "name": r.get("name", r["code"]),
            "group": r.get("group", ""),
            "chg": float(r.get("chg", 0) or 0),
            "dy": float(r.get("dy", 0) or 0),
            "kelly_final": float(r.get("kelly_final", 0) or 0),
            "lishing_score": float(r.get("lishing", 0) or 0),
            "price": float(r.get("price", 0) or 0),
            "earn_np_yoy": r.get("earn_np_yoy"),
            "earn_rev_yoy": r.get("earn_rev_yoy"),
            "earn_grade": r.get("earn_grade"),
        }
    return snap

"""红利候选股分级扫描（借鉴 tick-stock-panel 的「指标流水线 + 选股引擎」思路，但复用本项目既有引擎）。

流程（分级，控制数据成本）:
  1. 全市场 A 股批量快照 (modules.market_universe, 基于 akshare, 缓存 24h)
  2. 廉价粗筛 (流动性 / 估值 / 排除 ST / 排除北交所) -> 幸存者
  3. 分批小批量拉股息率 (避开源对大批量请求的限速) -> 按真实股息率收窄到高股息股
  4. 复用 build_portfolio_df 计算评分 / Kelly / 红线（仅对高股息幸存者）
  5. 红利质量过滤 (李氏评分 / 低波动 / Kelly>0 / 无红线) + 综合排名
  6. 写出 data/dividend_candidates.json（含 Kelly 建议仓位，可直接喂建仓决策）

数据缺失一律写"数据不足"，不做推断填充。
"""
import datetime
import json
import logging
import os

import pandas as pd

import modules.config as _cfg
from modules.config import SCREENER, WATCH_LIST
from modules.market_universe import bulk_a_spot
from modules.portfolio_engine import build_portfolio_df

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_OUT = os.path.join(_DATA_DIR, "dividend_candidates.json")


def _chunked_divs(codes, chunk=15):
    """分批拉股息率，规避源对大批量请求的限速/部分失败。"""
    from modules.data.fundamental import fetch_divs
    out = {}
    for i in range(0, len(codes), chunk):
        ch = codes[i:i + chunk]
        try:
            r = fetch_divs(ch)
        except Exception as e:
            logger.warning("[dividend_screener] fetch_divs chunk failed: %s", e)
            continue
        if isinstance(r, dict):
            out.update(r)
    return out


def _cheap_prefilter(spot):
    """用全市场批量快照做廉价粗筛（覆盖全部 A 股，而非仅缓存持仓）。"""
    cfg = SCREENER
    max_pe = float(cfg.get("max_pe", 25))
    max_pb = float(cfg.get("max_pb", 3.0))
    min_mcap = float(cfg.get("min_mcap", 50.0))
    sp = spot.set_index("code")
    survivors, mcap_of = {}, {}
    for c, r in sp.iterrows():
        try:
            pe = float(r.get("pe") or 0)
            pb = float(r.get("pb") or 0)
            mcap = float(r.get("mcap") or 0)
        except (TypeError, ValueError):
            continue
        if pe <= 0 or pe > max_pe:
            continue
        if pb <= 0 or pb > max_pb:
            continue
        if mcap < min_mcap:
            continue
        name = str(r.get("name") or c)
        survivors[c] = name
        mcap_of[c] = mcap
    for c, item in (WATCH_LIST.get("A", {}) or {}).items():
        if isinstance(item, dict):
            survivors.setdefault(str(c), item.get("name", str(c)))
            mcap_of.setdefault(str(c), 0)
    logger.info("[dividend_screener] prefilter -> %d survivors", len(survivors))
    return survivors, mcap_of


def _norm_mcap(survivors, mcap_of):
    cap = int(SCREENER.get("cap", 400))
    if len(survivors) <= cap:
        return survivors
    ranked = sorted(survivors.keys(), key=lambda c: mcap_of.get(c, 0), reverse=True)
    kept = ranked[:cap]
    return {c: survivors[c] for c in kept}


def _passes_quality(df):
    cfg = SCREENER
    min_lishing = float(cfg.get("min_lishing", 60))
    max_vol = float(cfg.get("max_vol", 45))
    red_keywords = set(cfg.get("red_flags", ["审计", "ST", "退市", "现金流", "违规"]))

    sub = df.copy()
    sub = sub[sub["lishing"] >= min_lishing]
    sub = sub[sub["vol"] <= max_vol]
    sub = sub[sub["kelly_final"] > 0]

    def _has_red(flags):
        if not flags:
            return False
        s = str(flags)
        return any(k in s for k in red_keywords)

    sub = sub[~sub["lishing_flags"].apply(_has_red)]
    return sub


def _composite_score(row, cfg):
    w_dy = float(cfg.get("w_dy", 1.0))
    w_ls = float(cfg.get("w_lishing", 0.5))
    w_kelly = float(cfg.get("w_kelly", 0.3))
    dy_n = min(row["dy"] / max(float(cfg.get("min_dy", 3.0)), 0.1), 3.0)
    ls_n = row["lishing"] / 100.0
    kelly_n = min(row["kelly_final"] / 30.0, 1.0)
    return w_dy * dy_n + w_ls * ls_n + w_kelly * kelly_n


def _reason(r):
    parts = [f"股息率{r['dy']:.2f}%", f"李氏{r['lishing']:.0f}", f"Kelly{r['kelly_final']:.1f}%"]
    if pd.notna(r.get("vol")):
        parts.append(f"年化波动{r['vol']:.0f}%")
    return " · ".join(parts)


def scan_dividend_candidates():
    cfg = SCREENER
    if not cfg.get("enabled", True):
        logger.info("[dividend_screener] disabled")
        return None
    try:
        spot = bulk_a_spot()
    except Exception as e:
        logger.warning("[dividend_screener] bulk spot failed: %s", e)
        return None
    universe_size = len(spot)
    price_map = dict(zip(spot["code"], spot["price"]))

    survivors, mcap_of = _cheap_prefilter(spot)
    survivors = _norm_mcap(survivors, mcap_of)
    if not survivors:
        logger.warning("[dividend_screener] no survivors from prefilter")
        return None

    # tier-1.5: 分批拉股息率，按真实股息率收窄到高股息股
    min_dy = float(cfg.get("min_dy", 3.0))
    div_raw = _chunked_divs(list(survivors.keys()), chunk=15)
    dy_map = {}
    for c in survivors:
        d = div_raw.get(c)
        ttm = (d.get("ttm") or d.get("lfy") or 0) if isinstance(d, dict) else 0
        p = price_map.get(c)
        dy_map[c] = round(ttm / p * 100, 2) if (p and p > 0 and ttm and ttm > 0) else 0.0

    watch_codes = set(str(c) for c in (WATCH_LIST.get("A", {}) or {}))
    survivors2 = {c: survivors[c] for c in survivors
                  if dy_map.get(c, 0) >= min_dy or c in watch_codes}
    if not survivors2:
        survivors2 = survivors  # 兜底：仍跑引擎，最终由质量过滤判定
    logger.info("[dividend_screener] high-dividend survivors: %d", len(survivors2))

    # 注入可靠股息率，避免 build_portfolio_df 再次大批量拉取导致部分归零
    saved_div = _cfg.MANUAL_DIV_YIELDS
    try:
        _cfg.MANUAL_DIV_YIELDS = {
            c: {"ttm": dy_map[c]} for c in survivors2 if dy_map.get(c, 0) > 0
        }
        portfolio_map = {"A": {c: {"name": n, "ratio": 0} for c, n in survivors2.items()}}
        df, meta = build_portfolio_df(portfolio_map=portfolio_map)
    finally:
        _cfg.MANUAL_DIV_YIELDS = saved_div

    if df is None or df.empty:
        logger.warning("[dividend_screener] build_portfolio_df empty")
        return None

    qual = _passes_quality(df)
    if qual.empty:
        logger.info("[dividend_screener] 0 candidates after quality filter")
        candidates = []
    else:
        qual = qual.copy()
        qual["score"] = qual.apply(lambda r: _composite_score(r, cfg), axis=1)
        qual = qual.sort_values("score", ascending=False).head(int(cfg.get("top_n", 30)))
        candidates = []
        for _, r in qual.iterrows():
            candidates.append({
                "code": r["code"], "name": r["name"],
                "price": round(float(r["price"]), 2) if pd.notna(r["price"]) else None,
                "dy": round(float(r["dy"]), 2),
                "lishing": round(float(r["lishing"]), 1),
                "kelly_final": round(float(r["kelly_final"]), 1),
                "pe": None if pd.isna(r["pe"]) else round(float(r["pe"]), 1),
                "pb": None if pd.isna(r["pb"]) else round(float(r["pb"]), 2),
                "mcap": round(float(r["mcap"]), 1) if pd.notna(r["mcap"]) else None,
                "vol": round(float(r["vol"]), 1),
                "roe": None if pd.isna(r["roe"]) else round(float(r["roe"]), 1),
                "reason": _reason(r),
            })

    payload = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "criteria": {k: cfg.get(k) for k in ("min_dy", "min_lishing", "max_vol",
                                             "max_pe", "max_pb", "min_mcap", "top_n", "cap")},
        "universe_size": universe_size,
        "prefilter_size": len(survivors),
        "high_div_size": len(survivors2),
        "count": len(candidates),
        "candidates": candidates,
    }
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_OUT, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("[dividend_screener] write %s failed: %s", _OUT, e)
    logger.info("[dividend_screener] wrote %d candidates -> %s", len(candidates), _OUT)
    return payload


def run_dividend_scan(worker=None):
    """供 alert_worker 调度调用：扫描并推送 Top 候选摘要。"""
    payload = scan_dividend_candidates()
    if not payload or not payload.get("candidates"):
        logger.info("[dividend_screener] no candidates to push")
        return payload
    top = payload["candidates"][: int(SCREENER.get("push_top", 10))]
    lines = [f"红利候选扫描 {payload['generated_at']} | 候选 {payload['count']}/{payload['high_div_size']} (全市场 {payload['universe_size']})"]
    for c in top:
        lines.append(f"{c['code']} {c['name']}: {c['reason']}")
    content = "\n".join(lines)
    title = "[红利候选] 每日扫描 Top%d" % len(top)
    if worker is not None:
        try:
            worker.sender.send_multi(title, content)
        except Exception as e:
            logger.warning("[dividend_screener] push failed: %s", e)
    else:
        logger.info("%s\n%s", title, content)
    return payload

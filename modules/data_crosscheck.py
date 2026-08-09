"""每日数据源交叉核对报告：长桥 vs 新浪/东财/腾讯 实时报价一致性。

用法:
  python -m modules.data_crosscheck --dry-run          # 生成报告不推送
  python -m modules.data_crosscheck                     # 生成并推送(默认)
  python -m modules.data_crosscheck --date 2026-07-09   # 指定日期(测试用)

流程:
  - 范围: watchlist + 持仓, 按市场分组(A/HK/US)。
  - 各源拉价: A=长桥/东财/新浪/腾讯; HK=长桥/新浪HK/腾讯; US=长桥。
  - 以长桥为主基准(缺长桥则取可用源中位数), 逐源算相对偏差%。
  - 生成 markdown 到 data/reports/crosscheck-<date>.md, 可选推送。

交易日判断复用 daily_pusher 日历(非交易日跳过)。
"""
import argparse
import datetime
import json
import logging
import os
import statistics
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logger = logging.getLogger("data_crosscheck")

DATA_DIR = os.path.join(_ROOT, "data")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
STATE_PATH = os.path.join(DATA_DIR, "data_crosscheck_state.json")

DEFAULT_PCT = 2.0

# 各市场参与比对的源 (key, 显示名)。长桥固定第一列。
MKT_SOURCES = {
    "A": [("longbridge", "长桥"), ("eastmoney", "东财"), ("sina", "新浪"), ("tencent", "腾讯")],
    "HK": [("longbridge", "长桥"), ("sina_hk", "新浪HK"), ("tencent", "腾讯")],
    "US": [("longbridge", "长桥")],
}


def _load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def _already_done(day):
    return _load_state().get("last_run_date") == day


def _mark_done(day, summary):
    st = _load_state()
    st["last_run_date"] = day
    st["last_summary"] = summary
    _save_state(st)


def target_codes():
    from modules.config import PORT_A, PORT_HK, PORT_REIT, PORT_US, WATCH_LIST
    groups = {"A": [], "HK": [], "US": []}
    seen = {"A": set(), "HK": set(), "US": set()}

    def add(mkt, code):
        code = str(code).strip()
        if code and code not in seen[mkt]:
            seen[mkt].add(code)
            groups[mkt].append(code)

    for code, info in (WATCH_LIST or {}).items():
        add(str(info.get("market", "A")).upper(), code)
    # PORT_* 字典以 code 为键, value 含 name/ratio, 不含 code 字段
    for code in (PORT_A or {}):
        add("A", code)
    for code in (PORT_HK or {}):
        add("HK", code)
    for code in (PORT_US or {}):
        add("US", code)
    for code in (PORT_REIT or {}):
        add("A", code)
    return groups


def _fetch_all(mkt, codes):
    """并发拉各源报价, 返回 {source: {code: price}}。失败源为空 dict。"""
    import concurrent.futures as cf

    from modules.data import longbridge as _lb
    from modules.data.fallback import _fetch_eastmoney_spot
    from modules.data.market import _fetch_sina, _fetch_sina_hk, _fetch_tx_quotes

    def _l():
        return {r["c"]: float(r["price"]) for r in _lb.fetch_spot(codes) if r.get("price")}

    def _a():
        df = _fetch_eastmoney_spot(codes)
        if df is None or df.empty:
            return {}
        return {str(r["c"]): float(r["price"])
                for _, r in df.iterrows() if r.get("price")}

    def _sa():
        df = _fetch_sina(codes)
        if df is None or df.empty:
            return {}
        return {str(r["c"]): float(r["price"])
                for _, r in df.iterrows() if r.get("price")}

    def _sh():
        df = _fetch_sina_hk(codes)
        if df is None or df.empty:
            return {}
        return {str(r["c"]): float(r["price"])
                for _, r in df.iterrows() if r.get("price")}

    def _tx():
        raw = _fetch_tx_quotes(codes)
        return {c: float(q["price"]) for c, q in raw.items() if q.get("price")}

    fns = {
        "longbridge": _l, "eastmoney": _a, "sina": _sa,
        "sina_hk": _sh, "tencent": _tx,
    }
    keys = [k for k, _ in MKT_SOURCES[mkt]]
    out = {}
    with cf.ThreadPoolExecutor(max_workers=len(keys)) as ex:
        futures = {k: ex.submit(fns[k]) for k in keys}
        for k, f in futures.items():
            try:
                out[k] = f.result(timeout=60)
            except Exception as e:
                logger.warning("[%s] 源 %s 失败: %s", mkt, k, e)
                out[k] = {}
    return out


def _primary(by_source, code):
    """基准价: 长桥优先, 否则可用源中位数。"""
    lbv = by_source.get("longbridge", {}).get(code)
    vals = []
    if lbv:
        vals.append(float(lbv))
    for k, src in by_source.items():
        if k == "longbridge":
            continue
        v = src.get(code)
        if v:
            vals.append(float(v))
    if not vals:
        return None
    if lbv:
        return float(lbv)
    return statistics.median(vals)


def build_report(day=None, threshold_pct=DEFAULT_PCT):
    today = day or datetime.date.today()
    date_s = today.isoformat()
    groups = target_codes()
    total = sum(len(v) for v in groups.values())

    lines = []
    lines.append(f"# 行情多源交叉核对 — {date_s}")
    lines.append("")
    lines.append(f"> 生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"> 范围: watchlist+持仓 共 {total} 只 · 偏差阈值 >{threshold_pct}%")
    lines.append("")

    issues = []
    for mkt in ("A", "HK", "US"):
        codes = groups.get(mkt, [])
        if not codes:
            continue
        by = _fetch_market(mkt, codes)
        lines.append(f"## {mkt} 市场")
        lines.append("")
        hdr = ["代码", "长桥"]
        names = [n for k, n in MKT_SOURCES[mkt] if k != "longbridge"]
        hdr += names
        hdr.append("基准")
        lines.append("| " + " | ".join(hdr) + " |")
        lines.append("|" + "---|" * len(hdr))
        for code in codes:
            pri = _primary(by, code)
            row = []
            for k, name in MKT_SOURCES[mkt]:
                v = by.get(k, {}).get(code)
                if v is None:
                    row.append("—")
                    continue
                if pri and abs(float(v) - pri) / pri * 100 > threshold_pct:
                    row.append(f"**{v:.2f}**")
                    issues.append(f"{mkt}/{code}: {name} {v:.2f} 偏离基准 {pri:.2f} "
                                  f"({abs(float(v)-pri)/pri*100:.2f}%)")
                else:
                    row.append(f"{v:.2f}")
            pri_s = f"{pri:.2f}" if pri else "—"
            lines.append(f"| {code} | " + " | ".join(row) + f" | {pri_s} |")
        lines.append("")

    lines.append("---")
    lines.append("## 差异问题汇总")
    if issues:
        for i in issues:
            lines.append(f"- ⚠ {i}")
    else:
        lines.append("- ✅ 各源报价一致(阈值内)")
    lines.append("")
    return "\n".join(lines)


def _fetch_market(mkt, codes):
    # REIT(沪 50xxxx)东财接口返回份额面值而非净值, 排除东财源以免假偏差
    if mkt == "A" and any(c.startswith(("50", "51")) for c in codes):
        MKT_SOURCES["A"] = [("longbridge", "长桥"), ("sina", "新浪"), ("tencent", "腾讯")]
    else:
        MKT_SOURCES["A"] = [("longbridge", "长桥"), ("eastmoney", "东财"),
                            ("sina", "新浪"), ("tencent", "腾讯")]
    return _fetch_all(mkt, codes)


def run(day=None, dry_run=False, threshold_pct=DEFAULT_PCT):
    from modules.alerts import AlertSender
    from modules.config import ALERT_CHANNELS
    from modules.daily_pusher import _is_trading_day_ah

    today = day or datetime.date.today()
    date_s = today.isoformat()

    if not _is_trading_day_ah(today):
        logger.info("%s 非交易日, 跳过交叉核对", date_s)
        return False, None

    if not dry_run and _already_done(date_s):
        logger.info("%s 已生成, 跳过重复", date_s)
        return True, None

    text = build_report(today, threshold_pct=threshold_pct)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, f"crosscheck-{date_s}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    logger.info("报告已写入 %s", path)

    if dry_run:
        print("=" * 60)
        print(text)
        print("=" * 60)
        return False, text

    ok, ch = AlertSender().send_multi(
        "行情多源交叉核对 " + date_s, text, channels=ALERT_CHANNELS,
        meta={"kind": "data_crosscheck"})
    if ok:
        _mark_done(date_s, {"sent_at": datetime.datetime.now().isoformat(),
                            "via": ch})
    logger.info("交叉核对推送 %s: 通道 %s", ok, ch)
    return ok, text


def main():
    parser = argparse.ArgumentParser(description="数据源交叉核对报告")
    parser.add_argument("--dry-run", action="store_true", help="只生成不推送")
    parser.add_argument("--date", help="YYYY-MM-DD (测试用)")
    parser.add_argument("--threshold", type=float, default=DEFAULT_PCT,
                        help=f"偏差阈值%% (默认 {DEFAULT_PCT})")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    day = None
    if args.date:
        day = datetime.date.fromisoformat(args.date)
    run(day=day, dry_run=args.dry_run, threshold_pct=args.threshold)


if __name__ == "__main__":
    main()
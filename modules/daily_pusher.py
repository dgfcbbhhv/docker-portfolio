"""每日选股推送入口。

用法:
  python -m modules.daily_pusher --dry-run --market AH   # 生成文案不发送
  python -m modules.daily_pusher --market AH             # 生成并推送
  python -m modules.daily_pusher --market US

交易日判断:
  - A/H: akshare 交易日历(缓存到 data/trading_calendar.json), 失败则退化为周一~周五
  - 美股: 配置节假日清单 + 周末

数据新鲜度异常时不推选股, 只发"数据不可靠, 请先刷新"提醒。
"""
import argparse
import datetime
import json
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logger = logging.getLogger("daily_pusher")

from modules.alerts import AlertSender
from modules.config import ALERT_CHANNELS
from modules.daily_picks import build_picks, compose_message
from modules.data.freshness import freshness_issues

DATA_DIR = os.path.join(_ROOT, "data")
CAL_PATH = os.path.join(DATA_DIR, "trading_calendar.json")
PUSH_STATE_PATH = os.path.join(DATA_DIR, "daily_push_state.json")


def _load_push_state():
    if os.path.exists(PUSH_STATE_PATH):
        try:
            with open(PUSH_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_push_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PUSH_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PUSH_STATE_PATH)


def _already_pushed(market, date, kind="picks"):
    """当日该市场/类型是否已推送过（防重复轰炸）。"""
    st = _load_push_state()
    rec = st.get("%s:%s" % (market, kind))
    return bool(rec and rec.get("date") == date)


def _mark_pushed(market, date, kind="picks", **extra):
    st = _load_push_state()
    rec = dict(extra)
    rec["date"] = date
    rec["sent_at"] = datetime.datetime.now().isoformat()
    st["%s:%s" % (market, kind)] = rec
    _save_push_state(st)

# 美股主要节假日(2025-2026), 按日期跳过
US_HOLIDAYS = {
    (1, 1), (1, 19), (2, 16), (5, 25), (6, 19), (7, 4),
    (9, 1), (11, 27), (12, 25),
}


def _is_trading_day_ah(day):
    """A/H 交易日判断: akshare 交易日历 + 周末兜底。"""
    if day.weekday() >= 5:
        return False
    cal = _load_calendar(day.year)
    if cal is not None:
        s = day.strftime("%Y-%m-%d")
        return s in cal
    return True  # 日历不可用则按周一~五


def _load_calendar(year):
    if os.path.exists(CAL_PATH):
        try:
            with open(CAL_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("year") == year:
                return set(data.get("days", []))
        except Exception as _e:
            logger.warning(f"[{__name__}] %s", _e, exc_info=True)
    days = _fetch_calendar(year)
    if days is not None:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CAL_PATH, "w", encoding="utf-8") as f:
            json.dump({"year": year, "days": sorted(days)}, f)
    return days


def _fetch_calendar(year):
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        days = set(df["trade_date"].astype(str))
        return days
    except Exception as e:
        logger.warning("交易日历获取失败: %s", e)
        return None


def _is_trading_day_us(day):
    if day.weekday() >= 5:
        return False
    return (day.month, day.day) not in US_HOLIDAYS


def _freshness_ok():
    try:
        issues, _ = freshness_issues()
        return not issues
    except Exception:
        logger.exception("freshness check failed; treating data as unreliable")
        return False


def build_and_send(market="AH", dry_run=False, day=None, skip_trading_check=False):
    """生成并推送(或仅 dry-run)。返回 (sent_ok, message)。

    Args:
        day: 指定日期(默认今天)。
        skip_trading_check: 跳过交易日判断(测试用)。
    """
    today = day or datetime.date.today()
    today_s = today.isoformat()
    if not dry_run and _already_pushed(market, today_s):
        logger.info("%s %s 当日已推送, 跳过重复", market, today_s)
        return True, None
    if not skip_trading_check:
        # 美股收盘对应北京时间次日凌晨; 06:30 推送时, 美股交易日 = 北京日期-1天
        check_day = today - datetime.timedelta(days=1) if market == "US" else today
        is_trading = (_is_trading_day_ah if market == "AH" else _is_trading_day_us)(check_day)
        if not is_trading:
            logger.info("%s %s 非交易日, 跳过", market, check_day)
            return False, None

    if not _freshness_ok():
        msg = "【数据异常】今日数据新鲜度异常，选股不可靠，请先到界面刷新数据后再查看。"
        if not dry_run and not _already_pushed(market, today_s, "warn"):
            AlertSender().send_multi(
                "选股推送-数据异常", msg, channels=ALERT_CHANNELS,
                meta={"kind": "daily_picks", "market": market,
                      "date": today_s, "freshness_ok": False})
            _mark_pushed(market, today_s, "warn")
        logger.warning("数据新鲜度异常, 选股推送降级为提醒")
        return True, msg

    result = build_picks(include_announcements=(market == "AH"))
    if market == "US":
        result["picks"] = [p for p in result.get("picks", []) if p["market"] == "US"]
    else:
        result["picks"] = [p for p in result.get("picks", []) if p["market"] in ("A", "HK")]
    result["date"] = today_s

    msg = compose_message(result, market=market)
    if not msg:
        logger.info("%s 今日无符合条件标的", market)
        return False, None

    if dry_run:
        logger.info("=" * 60)
        logger.info(msg)
        logger.info("=" * 60)
        logger.info(f"[dry-run] 候选池 {len(result.get('picks', []))} 只，"
              f"跳过 {len(result.get('skipped', []))} 只数据不足")
        return False, msg

    ok, channel = AlertSender().send_multi(
        "今日价值投资选股", msg, channels=ALERT_CHANNELS,
        meta={"kind": "daily_picks", "market": market, "date": today_s,
              "freshness_ok": True, "picks": len(result.get("picks", [])),
              "codes": [p.get("code") for p in result.get("picks", [])]})
    if ok:
        _mark_pushed(market, today_s, "picks", picks=len(result.get("picks", [])),
                     channel=channel)
        try:
            from modules.execution_log import ensure_pending_from_audit
            ensure_pending_from_audit()
        except Exception:
            logger.exception("execution log sync failed")
    logger.info("选股推送 %s: %s (%d 只, 通道 %s)", market, ok,
                len(result.get("picks", [])), channel)
    return ok, msg


def main():
    parser = argparse.ArgumentParser(description="每日选股推送")
    parser.add_argument("--market", choices=["AH", "US"], default="AH")
    parser.add_argument("--dry-run", action="store_true", help="只生成文案不发送")
    parser.add_argument("--date", help="指定日期 YYYY-MM-DD (测试用, 跳过交易日判断)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    day = None
    if args.date:
        day = datetime.date.fromisoformat(args.date)
    build_and_send(args.market, dry_run=args.dry_run, day=day,
                   skip_trading_check=bool(args.date))


if __name__ == "__main__":
    main()

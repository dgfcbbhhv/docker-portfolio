"""后台预警进程（独立于 Streamlit）。

- 交易时段每 60 秒、非交易时段每 300 秒拉取一次持仓并检查预警
- 发送失败自动重试 + 多通道降级
- 去重冷却，避免同一预警反复轰炸
- 写入心跳文件供容器健康检查

启动方式: python -m modules.alert_worker
"""
import datetime
import json
import logging.handlers
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from modules.netfix import enable_ipv4_preference

enable_ipv4_preference()
from modules.emfix import enable_em_http

enable_em_http()
from modules.alerts import AlertSender, check_alerts, log_alerts
from modules.config import (
    ALERT_CHANNELS,
    ALERT_COOLDOWN_MINUTES,
    ALERT_RETRIES,
    ALERT_THRESHOLDS,
    PORT_A,
    PORT_HK,
    PORT_REIT,
    PORT_US,
    get_config,
)
from modules.data.cache import active_keys, gc_orphans, gc_stale
from modules.data.freshness import freshness_issues
from modules.portfolio_engine import alert_snapshot, build_portfolio_df
from modules.scheduler import Scheduler, is_trading_hours

DATA_DIR = os.path.join(_ROOT, "data")
STATE_PATH = os.path.join(DATA_DIR, "alert_worker_state.json")
HEARTBEAT_PATH = os.path.join(DATA_DIR, "worker_heartbeat.json")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("alert_worker")


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


def _write_heartbeat():
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HEARTBEAT_PATH, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time(), "iso": datetime.datetime.now().isoformat()}, f)


class AlertWorker:
    def __init__(self, cfg=None):
        self.cfg = cfg or get_config()
        self.state = _load_state()
        self.sender = AlertSender()

    def _portfolio_map(self):
        port = {}
        for mapping in (PORT_A, PORT_HK, PORT_US, PORT_REIT):
            for code, item in mapping.items():
                port[code] = {"name": item.get("name", code)}
        return port

    def _dedup(self, alerts):
        cooldown = ALERT_COOLDOWN_MINUTES * 60
        now = time.time()
        sent = self.state.setdefault("sent", {})
        out = []
        for a in alerts:
            key = "%s|%s" % (a.get("code"), a.get("type"))
            last = sent.get(key)
            if last and (now - last) < cooldown:
                continue
            out.append(a)
        return out

    def _mark_sent(self, alerts):
        now = time.time()
        sent = self.state.setdefault("sent", {})
        for a in alerts:
            key = "%s|%s" % (a.get("code"), a.get("type"))
            sent[key] = now

    def _send_alerts(self, alerts, cur):
        for a in alerts:
            code = a["code"]
            price = cur.get(code, {}).get("price", 0)
            title = "[预警] %s %s" % (a.get("name", code), a["type"])
            content = "%s\n当前价: %.2f\n时间: %s" % (
                a["msg"], price, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            ok, channel = self.sender.send_multi(
                title, content, channels=ALERT_CHANNELS, retries=ALERT_RETRIES)
            if ok:
                logger.info("sent alert %s via %s", a["type"], channel)
            else:
                logger.error("all alert channels failed for %s", title)
            self._mark_sent([a])

    def _maybe_send_data_failure(self, meta):
        missing = [k for k, v in meta.items() if k.startswith("spot_") and not v]
        if not missing:
            return
        key = "data|failure|%s" % ",".join(sorted(missing))
        now = time.time()
        sent = self.state.setdefault("sent", {})
        if sent.get(key) and now - sent[key] < ALERT_COOLDOWN_MINUTES * 60:
            return
        title = "[数据异常] 行情源全部失败"
        content = "缺失市场: %s\n请检查网络或数据源。" % ", ".join(missing)
        self.sender.send_multi(title, content, channels=ALERT_CHANNELS,
                               retries=ALERT_RETRIES)
        sent[key] = now

    def _check_data_freshness(self):
        """交叉验证数据新鲜度：与 App 端同一套门禁，独立运行。
        写入心跳/状态供健康检查读取；严重过期时发送预警。
        """
        try:
            issues, status = freshness_issues()
        except Exception as e:
            logger.warning("freshness check failed: %s", e)
            return
        self.state["last_freshness"] = {
            "ts": time.time(),
            "iso": datetime.datetime.now().isoformat(),
            "issues": issues,
        }
        _write_heartbeat()
        if not issues:
            return
        logger.warning("data freshness issues: %s", "; ".join(issues))
        key = "data|freshness|%s" % "|".join(sorted(issues))
        now = time.time()
        sent = self.state.setdefault("sent", {})
        if sent.get(key) and now - sent[key] < ALERT_COOLDOWN_MINUTES * 60:
            return
        title = "[数据新鲜度] 缓存过期"
        content = "\n".join(issues) + "\n请到界面点击刷新或检查数据源。"
        self.sender.send_multi(title, content, channels=ALERT_CHANNELS,
                               retries=ALERT_RETRIES)
        sent[key] = now

    def run_cycle(self):
        started = time.time()
        _write_heartbeat()  # 长循环期间保持心跳新鲜，避免健康检查误杀
        try:
            gc_stale(ttl_multiplier=2.0)
            gc_orphans(active_keys())
            self._check_data_freshness()
            df, meta = build_portfolio_df(self.cfg)
            if df.empty:
                logger.warning("cycle produced empty portfolio data: %s", meta)
                self._maybe_send_data_failure(meta)
                self.state["last_cycle_ok"] = False
                return
            cur = alert_snapshot(df)
            prev = self.state.get("last_snapshot", {})
            scores = {code: {"total": v.get("lishing_score", 0)}
                      for code, v in cur.items()}
            alerts = check_alerts(self._portfolio_map(), cur, prev, scores,
                                  ALERT_THRESHOLDS)
            alerts = self._dedup(alerts)
            if alerts:
                log_alerts(alerts)
                self._send_alerts(alerts, cur)
            self.state["last_snapshot"] = cur
            self.state["last_cycle"] = {
                "ts": time.time(),
                "iso": datetime.datetime.now().isoformat(),
                "alerts": len(alerts),
                "meta": meta,
            }
            self.state["last_cycle_ok"] = True
            logger.info("cycle ok: %d stocks, %d alerts, %.1fs",
                        len(df), len(alerts), time.time() - started)
        except Exception as e:
            logger.exception("cycle failed: %s", e)
            self.state["last_error"] = {
                "ts": time.time(),
                "iso": datetime.datetime.now().isoformat(),
                "error": str(e),
            }
            self.state["last_cycle_ok"] = False
        finally:
            _save_state(self.state)

    def _refresh_task(self):
        now = time.time()
        last = self.state.get("last_cycle", {}).get("ts", 0)
        if is_trading_hours() or now - last >= 300:
            self.run_cycle()

    def _push_daily_picks(self, market):
        from modules.config import DAILY_PUSH
        from modules.daily_pusher import build_and_send
        if not DAILY_PUSH.get("enabled"):
            return
        try:
            build_and_send(market=market)
        except Exception as e:
            logger.exception("daily picks push failed (%s): %s", market, e)

    def _run_crosscheck(self):
        from modules.config import CROSSCHECK
        if not CROSSCHECK.get("enabled"):
            return
        try:
            from modules.data_crosscheck import run
            ok, text = run()
            logger.info("data crosscheck: ok=%s", ok)
        except Exception as e:
            logger.exception("data crosscheck failed: %s", e)

    def _paper_daily_check(self):
        """模拟盘每日快照尝试: 复用 check_and_run_daily 的门禁+去重+锁。
        未到计划时间/非交易日/今日已跑 都会快速返回, 不消耗行情请求。"""
        try:
            from modules.paper_trader import check_and_run_daily
            res = check_and_run_daily()
            if res.get("status") in ("success", "failed"):
                logger.info("paper daily run: %s (%s)", res.get("status"),
                            res.get("reason") or res.get("error") or "")
            elif res.get("status") != "waiting":
                logger.info("paper daily check: %s (%s)", res.get("status"),
                            res.get("reason") or "")
        except Exception as e:
            logger.exception("paper daily check failed: %s", e)

    def _weekly_picks_report(self):
        """每周生成推送建议可信度报告（回测 + 执行闭环审计）。"""
        try:
            from modules.picks_evaluator import build_report
            report = build_report()
            if report.get("evaluation", {}).get("error"):
                logger.warning("weekly picks report skipped: %s",
                               report["evaluation"]["error"])
            else:
                n = report["evaluation"].get("total_records", 0)
                broken = report.get("execution_audit", {}).get("broken", [])
                logger.info("weekly picks report: %d 条建议, 执行异常 %d 条",
                            n, len(broken))
        except Exception as e:
            logger.exception("weekly picks report failed: %s", e)

    def run_forever(self):
        from modules.config import CROSSCHECK, DAILY_PUSH
        scheduler = Scheduler()
        scheduler.add_task("refresh_and_check", self._refresh_task, 60,
                           run_immediately=True)
        scheduler.add_task("heartbeat", _write_heartbeat, 60)
        scheduler.add_task("paper_daily", self._paper_daily_check, 300,
                           run_immediately=True)
        if DAILY_PUSH.get("enabled"):
            scheduler.add_daily_task("daily_push_ah", lambda: self._push_daily_picks("AH"),
                                     DAILY_PUSH.get("time_ah", "16:30"))
            scheduler.add_daily_task("daily_push_us", lambda: self._push_daily_picks("US"),
                                     DAILY_PUSH.get("time_us", "06:30"))
        scheduler.add_weekly_task("weekly_picks_report", self._weekly_picks_report,
                                  "18:30", 4)
        if CROSSCHECK.get("enabled"):
            scheduler.add_daily_task("crosscheck", self._run_crosscheck,
                                     CROSSCHECK.get("time", "16:40"))
        scheduler.start()
        logger.info("alert worker started")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("stopping alert worker")
            scheduler.stop()


def main():
    log_dir = os.path.join(DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "alert_worker.log"), maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)
    worker = AlertWorker()
    worker.run_forever()


if __name__ == "__main__":
    main()

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

    def _weekly_reit_report(self):
        """每周REIT分派率报告: 分派率/溢价率/最近分红, 推送+落盘。"""
        from modules.config import REIT_MONITOR
        if not REIT_MONITOR.get("enabled"):
            return
        try:
            from modules.reit_monitor import format_report, reit_report
            report = reit_report(codes=REIT_MONITOR.get("codes"))
            title = "[REIT监控] 分派率周报"
            content = format_report(report)
            content += "\n时间: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            ok, channel = self.sender.send_multi(
                title, content, channels=ALERT_CHANNELS, retries=ALERT_RETRIES)
            if ok:
                logger.info("reit weekly report sent via %s", channel)
            else:
                logger.error("reit weekly report all channels failed")
        except Exception as e:
            logger.exception("reit weekly report failed: %s", e)

    def _daily_signal_validation(self):
        """每日信号验证: 回溯 signal_snapshots 计算真实胜率, 有样本时推送。

        与 validate_signals() 一致。仅在有未来收益样本时推送, 避免无样本轰炸。
        """
        try:
            from modules.paper_trader import validate_signals
            result = validate_signals()
        except Exception as e:
            logger.exception("daily signal validation failed: %s", e)
            return

        total = result.get("total_signals", 0)
        if not total:
            logger.info("daily signal validation: 暂无未来收益样本 (total=%s)", total)
            return

        buy = result.get("buy_signals", {})
        sell = result.get("sell_signals", {})
        hold = result.get("hold_signals", {})

        def _row(sig):
            n = sig.get("total", 0)
            win = sig.get("win_rate", 0)
            avg = sig.get("avg_ret", 0)
            return n, win, avg

        bn, bw, ba = _row(buy)
        sn, sw, sa = _row(sell)
        hn, hw, ha = _row(hold)

        try:
            with open(os.path.join(DATA_DIR, "paper", "signal_snapshots.json"),
                      "r", encoding="utf-8") as f:
                _snaps = json.load(f)
            _snap_days = [k for k, v in _snaps.items() if isinstance(v, list) and v]
            snap_line = "快照: %d 天 (最后 %s)" % (len(_snap_days), max(_snap_days))
        except Exception:
            snap_line = "快照: 读取失败"

        title = "[信号验证] 每日胜率回测"
        content = (
            "真实信号胜率 (基于历史快照):\n"
            f"买入: {bn} 个, 胜率 {bw}%, 平均 {ba}%\n"
            f"卖出: {sn} 个, 胜率 {sw}%, 平均 {sa}%\n"
            f"持有: {hn} 个, 平均 {ha}%\n"
            f"{snap_line}\n"
            f"时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        try:
            ok, channel = self.sender.send_multi(
                title, content, channels=ALERT_CHANNELS, retries=ALERT_RETRIES)
            if ok:
                logger.info("daily signal validation sent via %s (%d 信号)", channel, total)
            else:
                logger.error("daily signal validation all channels failed")
        except Exception as e:
            logger.exception("daily signal validation push failed: %s", e)

    HEALTH_STALE_WEEKDAYS = 3
    VALIDATE_EXPECT_DAYS = 25

    @staticmethod
    def _weekdays_between(d1, d2):
        """d2 比 d1 晚多少个工作日(不含周末)。d1/d2 为 datetime。"""
        if d1 is None or d2 is None:
            return 999
        start = d1.date() if hasattr(d1, "date") else d1
        end = d2.date() if hasattr(d2, "date") else d2
        n = 0
        cur = start
        while cur < end:
            cur += datetime.timedelta(days=1)
            if cur.weekday() < 5:
                n += 1
        return n

    def _daily_health_check(self):
        """数据管线健康探针(只读): 快照停更 / 模拟盘停摆 / schedule 被关 / 验证长期无样本。
        异常时推送 [健康检查]; 每次把状态写入 data/paper/pipeline_health.json 供观测。"""
        issues = []
        stats = {"ts": time.time(), "iso": datetime.datetime.now().isoformat()}
        try:
            # 1) 模拟盘 schedule 开关
            sched = {}
            try:
                with open(os.path.join(DATA_DIR, "paper", "schedule_config.json"),
                          "r", encoding="utf-8") as f:
                    sched = json.load(f)
            except Exception as e:
                issues.append("schedule_config.json 读取失败: %s" % e)
            stats["schedule_enabled"] = bool(sched.get("enabled", False))
            if not stats["schedule_enabled"]:
                issues.append("模拟盘 schedule_config.enabled=false (快照链路可能已中断)")

            # 2) 信号快照积累
            snaps = {}
            try:
                with open(os.path.join(DATA_DIR, "paper", "signal_snapshots.json"),
                          "r", encoding="utf-8") as f:
                    snaps = json.load(f)
            except Exception as e:
                issues.append("signal_snapshots.json 读取失败: %s" % e)
            snap_days = sorted(k for k, v in snaps.items() if isinstance(v, list) and v)
            stats["snapshot_days"] = len(snap_days)
            stats["last_snapshot_date"] = snap_days[-1] if snap_days else None
            if not snap_days:
                issues.append("signal_snapshots 为空 (无任何快照)")
            else:
                try:
                    last = datetime.datetime.strptime(snap_days[-1], "%Y-%m-%d")
                    gap = self._weekdays_between(last, datetime.datetime.now())
                    stats["snapshot_stale_weekdays"] = gap
                    if gap >= self.HEALTH_STALE_WEEKDAYS:
                        issues.append("信号快照已停更 %d 个工作日 (最后 %s)"
                                      % (gap, snap_days[-1]))
                except Exception:
                    pass

            # 3) 模拟盘账本
            paper = {}
            try:
                with open(os.path.join(DATA_DIR, "paper", "paper_portfolio.json"),
                          "r", encoding="utf-8") as f:
                    paper = json.load(f)
            except Exception as e:
                issues.append("paper_portfolio.json 读取失败: %s" % e)
            stats["paper_positions"] = len(paper.get("positions", {}))
            stats["paper_trades"] = len(paper.get("trades", []))
            lu = paper.get("last_updated")
            if lu:
                try:
                    last = datetime.datetime.fromisoformat(lu)
                    gap = self._weekdays_between(last, datetime.datetime.now())
                    stats["paper_stale_weekdays"] = gap
                    if gap >= self.HEALTH_STALE_WEEKDAYS:
                        issues.append("模拟盘账本已停更 %d 个工作日 (最后 %s)" % (gap, lu))
                except Exception:
                    pass

            # 4) 验证样本积累 (超过预期仍为 0 → 告警)
            total = 0
            try:
                from modules.paper_trader import validate_signals
                total = int(validate_signals().get("total_signals", 0) or 0)
            except Exception as e:
                issues.append("validate_signals 执行失败: %s" % e)
            stats["validate_total"] = total
            if snap_days and total == 0 and len(snap_days) >= self.VALIDATE_EXPECT_DAYS:
                issues.append("已积累 %d 个交易日快照仍无验证样本 (total_signals=0)"
                              % len(snap_days))

            # 5) FMP 缓存规模 (配额使用参考)
            try:
                fmp_dir = os.path.join(DATA_DIR, "fmp_hist")
                stats["fmp_cached"] = len([f for f in os.listdir(fmp_dir)
                                           if f.endswith(".parquet")]) if os.path.isdir(fmp_dir) else 0
            except Exception:
                stats["fmp_cached"] = 0

            # 6) FMP 配额用量
            try:
                from modules.data.fmp import fmp_usage_today
                stats["fmp_requests_today"] = fmp_usage_today()
            except Exception:
                stats["fmp_requests_today"] = 0
            fmp_limit = int((self.cfg.get("daily_push", {}) or {})
                            .get("fmp_limit", 200) or 200)
            if stats["fmp_requests_today"] > fmp_limit:
                issues.append("FMP 今日请求 %d 次, 超过限额 %d"
                              % (stats["fmp_requests_today"], fmp_limit))

            stats["issues"] = issues
            self.state["last_pipeline_health"] = stats
            try:
                out = os.path.join(DATA_DIR, "paper", "pipeline_health.json")
                tmp = out + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(stats, f, ensure_ascii=False, indent=2)
                os.replace(tmp, out)
            except Exception as e:
                logger.warning("pipeline health write failed: %s", e)

            if not issues:
                logger.info("pipeline health ok: %d 快照天, last=%s, validate=%d",
                            stats.get("snapshot_days", 0),
                            stats.get("last_snapshot_date"), total)
                return

            key = "pipeline|health|%s" % "|".join(sorted(issues))
            now = time.time()
            sent = self.state.setdefault("sent", {})
            if sent.get(key) and now - sent[key] < ALERT_COOLDOWN_MINUTES * 60:
                return
            title = "[健康检查] 数据管线异常"
            content = "\n".join("- " + i for i in issues)
            content += "\n\n快照 %s 天 | 最后快照 %s | 验证样本 %s" % (
                stats.get("snapshot_days"), stats.get("last_snapshot_date") or "-", total)
            ok, channel = self.sender.send_multi(title, content, channels=ALERT_CHANNELS,
                                                 retries=ALERT_RETRIES)
            if ok:
                logger.info("pipeline health alert sent via %s", channel)
            else:
                logger.error("pipeline health alert all channels failed")
            sent[key] = now
        except Exception as e:
            logger.exception("pipeline health check failed: %s", e)

    def run_forever(self):
        from modules.config import CROSSCHECK, DAILY_PUSH, REIT_MONITOR
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
        if REIT_MONITOR.get("enabled"):
            scheduler.add_weekly_task("reit_monitor", self._weekly_reit_report,
                                      REIT_MONITOR.get("time", "18:45"),
                                      REIT_MONITOR.get("weekday", 5))
        if CROSSCHECK.get("enabled"):
            scheduler.add_daily_task("crosscheck", self._run_crosscheck,
                                     CROSSCHECK.get("time", "16:40"))
        scheduler.add_daily_task("daily_signal_validation", self._daily_signal_validation,
                                 DAILY_PUSH.get("time_validate", "18:00"))
        scheduler.add_daily_task("daily_health_check", self._daily_health_check,
                                 DAILY_PUSH.get("time_health", "18:10"))
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

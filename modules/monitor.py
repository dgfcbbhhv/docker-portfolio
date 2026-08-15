"""独立健康监控（容器级值班员）。

补上 Docker 不会主动喊人的空缺，作为 alert_worker 之外的第三容器运行：
  1. 主程序存活：HTTP 探测 http://127.0.0.1:8501/_stcore/health
  2. worker 心跳：worker_heartbeat.json 超过 15 分钟未更新视为 worker 死亡
  3. 数据新鲜度兜底：仅当 worker 心跳过期时才检查（worker 活着时归 worker
     自己发 [数据新鲜度] 告警，避免双份通知）
  4. NAS 磁盘空间：数据卷剩余空间 + data 目录体积（阈值可配）
  5. 定时任务执行验证：每日 A/H、US 选股推送与 REIT 周报是否按时落地
     （读取 daily_push_state.json / reit_monitor.json，非交易日自动跳过）
  6. 静默功能探针：模拟盘每日验证状态（schedule_config.json 的
     last_run/last_run_status）+ 成交对账/影子账户文件完整性

告警规则：
- 正常->故障、故障->恢复两个时刻发通知；连续 CONFIRM_FAILURES 次失败才确认故障
- 发送后进入 SILENT_SEC 抑制窗口，防 crash-loop 刷屏
- 确认故障后若持续超过 ESCALATE_MIN 分钟，逐级补发升级提醒（默认 60/180 分钟）
通道复用 AlertSender.send_multi（config 里 channels = [bark, email]）。
状态持久化 data/monitor_state.json，心跳 data/monitor_heartbeat.json 供健康检查。

启动方式: python -m modules.monitor
"""
import datetime
import json
import logging
import logging.handlers
import os
import shutil
import sys
import time
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from modules.alerts import AlertSender
from modules.config import ALERT_CHANNELS, ALERT_RETRIES

DATA_DIR = os.path.join(_ROOT, "data")
STATE_PATH = os.path.join(DATA_DIR, "monitor_state.json")
HEARTBEAT_PATH = os.path.join(DATA_DIR, "monitor_heartbeat.json")
WORKER_HEARTBEAT_PATH = os.path.join(DATA_DIR, "worker_heartbeat.json")
PUSH_STATE_PATH = os.path.join(DATA_DIR, "daily_push_state.json")
CAL_PATH = os.path.join(DATA_DIR, "trading_calendar.json")
REIT_REPORT_PATH = os.path.join(DATA_DIR, "reports", "reit_monitor.json")
PAPER_SCHEDULE_PATH = os.path.join(DATA_DIR, "paper", "schedule_config.json")
EXECUTION_PATH = os.path.join(DATA_DIR, "execution_log.json")
TRADE_PATH = os.path.join(_ROOT, "notes", "trades.json")
WEB_HEALTH_URL = "http://127.0.0.1:8501/_stcore/health"

INTERVAL_SEC = int(os.environ.get("MONITOR_INTERVAL_MIN", "5")) * 60
HEARTBEAT_MAX_SEC = int(os.environ.get("MONITOR_HEARTBEAT_MAX_SEC", "900"))
CONFIRM_FAILURES = int(os.environ.get("MONITOR_CONFIRM_FAILURES", "2"))
SILENT_SEC = int(os.environ.get("MONITOR_SILENT_MIN", "30")) * 60

# —— 磁盘空间 ——
DISK_FREE_MIN_GB = float(os.environ.get("MONITOR_DISK_FREE_MIN_GB", "10"))
DISK_FREE_MIN_PCT = float(os.environ.get("MONITOR_DISK_FREE_MIN_PCT", "10"))
DATA_MAX_GB = float(os.environ.get("MONITOR_DATA_MAX_GB", "20"))
DATA_SIZE_INTERVAL_CYCLES = max(1, int(os.environ.get("MONITOR_DATA_SIZE_INTERVAL", "6")))

# —— 定时任务验证 ——
SCHEDULE_CHECK_ENABLED = os.environ.get("MONITOR_SCHEDULE_CHECK", "1") == "1"
TASK_GRACE_MIN = int(os.environ.get("MONITOR_TASK_GRACE_MIN", "120"))
REIT_TIME_DEFAULT = os.environ.get("MONITOR_REIT_TIME", "18:45")
# 美股主要节假日（2025-2026），与 daily_pusher.US_HOLIDAYS 保持一致
US_HOLIDAYS = {(1, 1), (1, 19), (2, 16), (5, 25), (6, 19),
               (7, 4), (9, 1), (11, 27), (12, 25)}

# —— 故障升级 ——
ESCALATE_MIN = [int(x) for x in
                os.environ.get("MONITOR_ESCALATE_MIN", "60,180").split(",")
                if x.strip()]

_LABELS = {
    "web": "主程序 Streamlit",
    "worker_heartbeat": "告警 worker 心跳",
    "data_freshness": "数据新鲜度",
    "disk": "NAS 磁盘空间",
    "scheduled_tasks": "定时任务执行",
    "silent_features": "静默功能（模拟盘/对账/影子）",
}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("monitor")


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


def check_web():
    try:
        with urllib.request.urlopen(WEB_HEALTH_URL, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def check_worker_heartbeat():
    try:
        with open(WORKER_HEARTBEAT_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return (time.time() - float(d["ts"])) < HEARTBEAT_MAX_SEC
    except Exception:
        return False


def check_data_freshness():
    try:
        from modules.data.freshness import freshness_issues
        issues, _ = freshness_issues()
    except Exception as e:
        logger.warning("freshness check failed: %s", e)
        return ["新鲜度检查异常: %s" % e]
    return issues


def check_disk(state):
    """数据卷剩余空间 + data 目录体积。体积遍历带周期节流，避免每次全量扫描。"""
    try:
        usage = shutil.disk_usage(DATA_DIR)
        free_gb = usage.free / 1024 ** 3
        total_gb = usage.total / 1024 ** 3
        free_pct = usage.free / usage.total * 100 if usage.total else 0

        ds = state.setdefault("disk_state", {})
        ds["size_cycles"] = ds.get("size_cycles", 0) + 1
        if ds["size_cycles"] % DATA_SIZE_INTERVAL_CYCLES == 1 or "last_size_gb" not in ds:
            total = 0
            for _root, _dirs, files in os.walk(DATA_DIR):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(_root, f))
                    except OSError:
                        pass
            ds["last_size_gb"] = round(total / 1024 ** 3, 2)
        data_gb = ds.get("last_size_gb", 0)

        issues = []
        if free_gb < DISK_FREE_MIN_GB or free_pct < DISK_FREE_MIN_PCT:
            issues.append("数据卷剩余 %.1f GB (%.1f%%)，低于阈值 %.0f GB / %.0f%%"
                          % (free_gb, free_pct, DISK_FREE_MIN_GB, DISK_FREE_MIN_PCT))
        if data_gb > DATA_MAX_GB:
            issues.append("data 目录 %.2f GB，超过阈值 %.0f GB" % (data_gb, DATA_MAX_GB))

        base = "剩余 %.1f GB / 共 %.1f GB (%.1f%%)，data 目录 %.2f GB" % (
            free_gb, total_gb, free_pct, data_gb)
        return {"ok": not issues, "detail": "；".join(issues + [base]) if issues else base}
    except Exception as e:
        logger.warning("disk check failed: %s", e)
        return {"ok": False, "detail": "磁盘检查异常: %s" % e}


def _read_json_config():
    try:
        with open(os.path.join(_ROOT, "config.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_calendar(year):
    """读取缓存的 A/H 交易日历；不可用返回 None（回退为仅排除周末）。"""
    try:
        with open(CAL_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("year") == year:
            return set(data.get("days", []))
    except Exception:
        pass
    return None


def _is_trading_day_ah(day):
    if day.weekday() >= 5:
        return False
    cal = _load_calendar(day.year)
    if cal is not None:
        return day.strftime("%Y-%m-%d") in cal
    return True


def _is_trading_day_us(day):
    if day.weekday() >= 5:
        return False
    return (day.month, day.day) not in US_HOLIDAYS


def _hhmm_passed(now, hhmm, grace_min):
    try:
        h, m = hhmm.strip().split(":")
        target = datetime.datetime(now.year, now.month, now.day, int(h), int(m))
        return now >= target + datetime.timedelta(minutes=grace_min)
    except (ValueError, AttributeError):
        return False


def _load_push_state():
    try:
        with open(PUSH_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def check_scheduled_tasks():
    """验证今日固定任务是否落地：A/H、US 每日选股推送 + REIT 周报。"""
    if not SCHEDULE_CHECK_ENABLED:
        return {"ok": True, "detail": "定时任务检查未启用（MONITOR_SCHEDULE_CHECK=0）"}

    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    parts = []
    issues = []

    cfg = _read_json_config()
    dp = cfg.get("daily_push", {}) or {}
    rm = cfg.get("reit_monitor", {}) or {}
    push_enabled = bool(dp.get("enabled", True))
    time_ah = str(dp.get("time_ah", "16:30"))
    time_us = str(dp.get("time_us", "06:30"))
    reit_enabled = bool(rm.get("enabled", True))
    reit_weekday = int(rm.get("weekday", 4)) % 7
    reit_time = str(os.environ.get("MONITOR_REIT_TIME") or rm.get("time", REIT_TIME_DEFAULT))

    st = _load_push_state()

    if push_enabled and _is_trading_day_ah(now.date()) and _hhmm_passed(now, time_ah, TASK_GRACE_MIN):
        rec = st.get("AH:picks") or {}
        if rec.get("date") == today:
            parts.append("A/H 选股已推送")
        else:
            issues.append("A/H 每日选股 %s 未推送（期望 %s 后 %d 分钟内完成）"
                          % (today, time_ah, TASK_GRACE_MIN))
    else:
        parts.append("A/H 选股今日不适用或未到检查时间")

    if push_enabled and _is_trading_day_us(now.date()) and _hhmm_passed(now, time_us, TASK_GRACE_MIN):
        rec = st.get("US:picks") or {}
        if rec.get("date") == today:
            parts.append("US 选股已推送")
        else:
            issues.append("US 每日选股 %s 未推送（期望 %s 后 %d 分钟内完成）"
                          % (today, time_us, TASK_GRACE_MIN))
    else:
        parts.append("US 选股今日不适用或未到检查时间")

    if reit_enabled and now.weekday() == reit_weekday and _hhmm_passed(now, reit_time, TASK_GRACE_MIN):
        try:
            mtime = os.path.getmtime(REIT_REPORT_PATH)
            if datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d") == today:
                parts.append("REIT 周报已生成")
            else:
                issues.append("REIT 周报 %s 未更新（期望周%s %s 后 %d 分钟内生成）"
                              % (today, "一二三四五六日"[reit_weekday], reit_time, TASK_GRACE_MIN))
        except OSError:
            issues.append("REIT 周报文件缺失: data/reports/reit_monitor.json")
    else:
        parts.append("REIT 周报今日不适用或未到检查时间")

    detail = "；".join(issues + parts) if issues else "；".join(parts)
    return {"ok": not issues, "detail": detail}


def check_silent_features():
    """静默功能探针：模拟盘每日验证 + 成交对账/影子账户文件完整性。

    模拟盘是唯一有固定日更产出的后台管道（schedule_config.json 记录每日
    last_run/last_run_status）；成交对账与影子账户为事件驱动，无交易就不更新，
    因此只在文件存在但解析失败（损坏）时报警，避免无交易日误报。
    """
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    parts = []
    issues = []

    cfg = None
    cfg_missing = False
    try:
        with open(PAPER_SCHEDULE_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg_missing = True
    except Exception as e:
        issues.append("模拟盘 schedule_config.json 损坏: %s" % e)

    if _is_trading_day_ah(now.date()):
        run_time = str((cfg or {}).get("run_time", "15:30"))
        if _hhmm_passed(now, run_time, TASK_GRACE_MIN):
            if cfg is None:
                if cfg_missing:
                    issues.append("模拟盘 schedule_config.json 缺失，每日验证可能从未运行")
            else:
                last_run = cfg.get("last_run")
                status = cfg.get("last_run_status")
                if last_run == today and status == "success":
                    parts.append("模拟盘今日已验证成功")
                elif last_run == today:
                    issues.append("模拟盘今日运行失败: %s" % (status or "未知"))
                else:
                    issues.append("模拟盘今日未运行（期望 %s 后 %d 分钟内完成）"
                                  % (run_time, TASK_GRACE_MIN))
        else:
            parts.append("模拟盘未到检查时间（%s）" % run_time)
    else:
        parts.append("模拟盘今日不适用（非交易日）")

    for label, path in (("成交对账", EXECUTION_PATH), ("影子账户", TRADE_PATH)):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    json.load(f)
                parts.append("%s 文件正常" % label)
            except Exception as e:
                issues.append("%s 文件损坏: %s" % (label, e))
        else:
            parts.append("%s 暂无数据（跳过）" % label)

    detail = "；".join(issues + parts) if issues else "；".join(parts)
    return {"ok": not issues, "detail": detail}


def run_cycle(state):
    _write_heartbeat()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    results = {}

    web_ok = check_web()
    results["web"] = {
        "ok": web_ok,
        "detail": "主程序健康检查通过" if web_ok else "HTTP 探测 %s 失败" % WEB_HEALTH_URL,
    }

    worker_ok = check_worker_heartbeat()
    results["worker_heartbeat"] = {
        "ok": worker_ok,
        "detail": ("心跳在 %d 秒阈值内" % HEARTBEAT_MAX_SEC
                   if worker_ok else "心跳超过 %d 秒未更新，worker 疑似死亡" % HEARTBEAT_MAX_SEC),
    }

    if worker_ok:
        results["data_freshness"] = {
            "ok": True,
            "detail": "worker 存活，数据新鲜度由其自身检查",
        }
    else:
        issues = check_data_freshness()
        results["data_freshness"] = {
            "ok": not issues,
            "detail": "；".join(issues) if issues else "数据新鲜度正常",
        }

    results["disk"] = check_disk(state)
    results["scheduled_tasks"] = check_scheduled_tasks()
    results["silent_features"] = check_silent_features()

    state["last_check"] = {"ts": time.time(), "iso": now, "checks": results}
    return results


def _ensure_info(info):
    info.setdefault("consecutive", 0)
    info.setdefault("confirmed", False)
    info.setdefault("silent_until", 0)
    info.setdefault("recovery_pending", False)
    info.setdefault("fault_since", 0)
    info.setdefault("escalated", 0)
    return info


def _apply_transitions(state, results):
    now = time.time()
    items = state.setdefault("items", {})
    to_send = []
    for name, r in results.items():
        info = _ensure_info(items.get(name) or {})
        items[name] = info
        if r["ok"]:
            info["consecutive"] = 0
            info["fault_since"] = 0
            info["escalated"] = 0
            if info["confirmed"]:
                info["confirmed"] = False
                if now < info["silent_until"]:
                    # 抑制窗口内恢复：挂起，窗口过期后补发"已恢复"
                    info["recovery_pending"] = True
                    logger.info("%s 恢复但处于抑制窗口，延迟通知", name)
                else:
                    info["silent_until"] = now + SILENT_SEC
                    to_send.append(("recover", name, r, None))
            elif info["recovery_pending"] and now >= info["silent_until"]:
                info["recovery_pending"] = False
                info["silent_until"] = now + SILENT_SEC
                to_send.append(("recover", name, r, None))
        else:
            info["consecutive"] += 1
            if info["recovery_pending"]:
                info["recovery_pending"] = False
            if info["consecutive"] >= CONFIRM_FAILURES and not info["confirmed"]:
                info["confirmed"] = True
                info["fault_since"] = now
                info["escalated"] = 0
                if now < info["silent_until"]:
                    logger.info("%s 故障确认但处于抑制窗口，跳过通知", name)
                else:
                    info["silent_until"] = now + SILENT_SEC
                    to_send.append(("fault", name, r, None))
            elif info["confirmed"]:
                # 持续故障升级提醒：逐级补发，避免长时间静默
                fault_since = info.get("fault_since") or now
                elapsed_min = (now - fault_since) / 60
                for idx, level_min in enumerate(ESCALATE_MIN):
                    if elapsed_min >= level_min and info.get("escalated", 0) <= idx:
                        info["escalated"] = idx + 1
                        to_send.append(("escalate", name, r, {"elapsed_min": int(elapsed_min)}))
                        break
    return to_send


def _send(kind, name, r, sender, extra=None):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    label = _LABELS.get(name, name)
    if kind == "fault":
        title = "[监控] %s 故障" % label
        content = "监控项: %s\n状态: %s\n时间: %s\n请检查 NAS 上的 docker 服务。" % (
            label, r["detail"], now)
    elif kind == "escalate":
        dur = int((extra or {}).get("elapsed_min", 0))
        title = "[监控] %s 故障持续中" % label
        content = "监控项: %s\n状态: 已持续约 %d 分钟\n%s\n时间: %s\n请尽快检查。" % (
            label, dur, r["detail"], now)
    else:
        title = "[监控] %s 已恢复" % label
        content = "监控项: %s\n状态: 已恢复\n时间: %s" % (label, now)
    return sender.send_multi(title, content, channels=ALERT_CHANNELS,
                             retries=ALERT_RETRIES)


def run_forever():
    state = _load_state()
    sender = AlertSender()
    logger.info("monitor started (interval=%ds confirm=%d silent=%ds escalate=%s)",
                INTERVAL_SEC, CONFIRM_FAILURES, SILENT_SEC, ESCALATE_MIN)
    while True:
        try:
            results = run_cycle(state)
            for kind, name, r, extra in _apply_transitions(state, results):
                ok, ch = _send(kind, name, r, sender, extra)
                logger.info("alert %s %s -> ok=%s channel=%s", kind, name, ok, ch)
        except Exception as e:
            logger.exception("cycle failed: %s", e)
        finally:
            _save_state(state)
        time.sleep(INTERVAL_SEC)


def main():
    log_dir = os.path.join(DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "monitor.log"), maxBytes=5 * 1024 * 1024,
        backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)
    run_forever()


if __name__ == "__main__":
    main()

"""独立健康监控（容器级值班员）。

补上 Docker 不会主动喊人的空缺，作为 alert_worker 之外的第三容器运行：
  1. 主程序存活：HTTP 探测 http://127.0.0.1:8501/_stcore/health
  2. worker 心跳：worker_heartbeat.json 超过 15 分钟未更新视为 worker 死亡
  3. 数据新鲜度兜底：仅当 worker 心跳过期时才检查（worker 活着时归 worker
     自己发 [数据新鲜度] 告警，避免双份通知）

告警只在两个时刻发：正常->故障、故障->恢复。
连续 2 次检查失败才判定故障；发送后进入 30 分钟抑制窗口，防 crash-loop 刷屏。
通道复用 AlertSender.send_multi（config 里 channels = [bark, email]）。
状态持久化 data/monitor_state.json，心跳 data/monitor_heartbeat.json 供健康检查。

启动方式: python -m modules.monitor
"""
import datetime
import json
import logging
import logging.handlers
import os
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
WEB_HEALTH_URL = "http://127.0.0.1:8501/_stcore/health"

INTERVAL_SEC = int(os.environ.get("MONITOR_INTERVAL_MIN", "5")) * 60
HEARTBEAT_MAX_SEC = int(os.environ.get("MONITOR_HEARTBEAT_MAX_SEC", "900"))
CONFIRM_FAILURES = int(os.environ.get("MONITOR_CONFIRM_FAILURES", "2"))
SILENT_SEC = int(os.environ.get("MONITOR_SILENT_MIN", "30")) * 60

_LABELS = {
    "web": "主程序 Streamlit",
    "worker_heartbeat": "告警 worker 心跳",
    "data_freshness": "数据新鲜度",
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

    state["last_check"] = {"ts": time.time(), "iso": now, "checks": results}
    return results


def _ensure_info(info):
    info.setdefault("consecutive", 0)
    info.setdefault("confirmed", False)
    info.setdefault("silent_until", 0)
    info.setdefault("recovery_pending", False)
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
            if info["confirmed"]:
                info["confirmed"] = False
                if now < info["silent_until"]:
                    # 抑制窗口内恢复：挂起，窗口过期后补发"已恢复"
                    info["recovery_pending"] = True
                    logger.info("%s 恢复但处于抑制窗口，延迟通知", name)
                else:
                    info["silent_until"] = now + SILENT_SEC
                    to_send.append(("recover", name, r))
            elif info["recovery_pending"] and now >= info["silent_until"]:
                info["recovery_pending"] = False
                info["silent_until"] = now + SILENT_SEC
                to_send.append(("recover", name, r))
        else:
            info["consecutive"] += 1
            if info["recovery_pending"]:
                info["recovery_pending"] = False
            if info["consecutive"] >= CONFIRM_FAILURES and not info["confirmed"]:
                info["confirmed"] = True
                if now < info["silent_until"]:
                    logger.info("%s 故障确认但处于抑制窗口，跳过通知", name)
                else:
                    info["silent_until"] = now + SILENT_SEC
                    to_send.append(("fault", name, r))
    return to_send


def _send(kind, name, r, sender):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    label = _LABELS.get(name, name)
    if kind == "fault":
        title = "[监控] %s 故障" % label
        content = "监控项: %s\n状态: %s\n时间: %s\n请检查 NAS 上的 docker 服务。" % (
            label, r["detail"], now)
    else:
        title = "[监控] %s 已恢复" % label
        content = "监控项: %s\n状态: 已恢复\n时间: %s" % (label, now)
    return sender.send_multi(title, content, channels=ALERT_CHANNELS,
                             retries=ALERT_RETRIES)


def run_forever():
    state = _load_state()
    sender = AlertSender()
    logger.info("monitor started (interval=%ds confirm=%d silent=%ds)",
                INTERVAL_SEC, CONFIRM_FAILURES, SILENT_SEC)
    while True:
        try:
            results = run_cycle(state)
            for kind, name, r in _apply_transitions(state, results):
                ok, ch = _send(kind, name, r, sender)
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
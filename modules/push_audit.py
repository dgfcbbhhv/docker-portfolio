"""推送审计日志：记录每次推送的内容、通道、发送结果与上下文。

使用 JSONL 追加写入，避免并发场景下整文件重写丢失记录。
字段说明：
  push_id        推送唯一 ID，用于真实成交回填关联
  ts             发送完成时间
  channel        实际尝试的通道
  content_hash   标题+内容 SHA-256，用于核对消息是否被篡改
  content_preview 内容前 200 字符，方便界面快速浏览
  status         sent / failed
  retries        重试次数
  error          失败原因
  latency_ms     发送耗时
  meta           调用方附加上下文（市场、快照日期、数据源等）
"""

import datetime
import hashlib
import json
import os
import uuid

_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
AUDIT_PATH = os.path.join(_DATA_DIR, "push_audit.jsonl")


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def make_push_id():
    return uuid.uuid4().hex[:12]


def content_hash(title, content):
    return hashlib.sha256(
        f"{title or ''}\n{content or ''}".encode("utf-8")
    ).hexdigest()


def log_push(record):
    """追加一条推送审计记录，返回 push_id。"""
    os.makedirs(_DATA_DIR, exist_ok=True)
    rec = {
        "push_id": record.get("push_id") or make_push_id(),
        "ts": record.get("ts") or _now_iso(),
        "channel": record.get("channel"),
        "title": record.get("title"),
        "content_hash": record.get("content_hash")
        or content_hash(record.get("title", ""), record.get("content", "")),
        "content_preview": (record.get("content") or "")[:200],
        "status": record.get("status", "unknown"),
        "retries": int(record.get("retries", 0) or 0),
        "error": record.get("error"),
        "latency_ms": record.get("latency_ms"),
        "meta": record.get("meta") or {},
    }
    with open(AUDIT_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec["push_id"]


def load_push_log(limit=200):
    """读取最近 N 条审计记录，按时间倒序返回。"""
    if not os.path.exists(AUDIT_PATH):
        return []
    rows = []
    try:
        with open(AUDIT_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows[-limit:][::-1]


def push_summary(limit=1000):
    """推送送达率统计。"""
    rows = load_push_log(limit=limit)
    total = len(rows)
    sent = sum(1 for r in rows if r.get("status") == "sent")
    return {
        "total": total,
        "sent": sent,
        "failed": total - sent,
        "delivery_rate": round(sent / total * 100, 1) if total else None,
    }

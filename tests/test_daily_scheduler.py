"""每日定时任务调度单元测试"""
import datetime

import pytest

from modules.scheduler import Scheduler, _parse_hhmm


def test_parse_hhmm():
    assert _parse_hhmm("16:30") == (16, 30)
    assert _parse_hhmm("00:00") == (0, 0)
    assert _parse_hhmm("6:5") == (6, 5)
    assert _parse_hhmm("16:60") is None
    assert _parse_hhmm("24:00") is None
    assert _parse_hhmm("abc") is None
    assert _parse_hhmm(None) is None


def test_add_daily_task_invalid():
    s = Scheduler()
    with pytest.raises(ValueError):
        s.add_daily_task("x", lambda: None, "25:00")


def test_should_run_daily_at_time():
    s = Scheduler()
    s.add_daily_task("push", lambda: None, "16:30")
    task = s._tasks["push"]
    # 时间未到
    assert s._should_run_daily(task, datetime.datetime(2026, 7, 31, 10, 0)) is False
    # 准点到
    assert s._should_run_daily(task, datetime.datetime(2026, 7, 31, 16, 30)) is True
    # 到点后补跑
    assert s._should_run_daily(task, datetime.datetime(2026, 7, 31, 23, 59)) is True


def test_should_run_daily_dedup():
    s = Scheduler()
    s.add_daily_task("push", lambda: None, "16:30")
    task = s._tasks["push"]
    # 当天已跑过 → 不再触发
    task["last_run"] = "2026-07-31"
    assert s._should_run_daily(task, datetime.datetime(2026, 7, 31, 18, 0)) is False
    # 次日重新触发
    assert s._should_run_daily(task, datetime.datetime(2026, 8, 1, 16, 30)) is True


def test_daily_task_execution(monkeypatch):
    """到点后通过调度循环执行一次, 当天去重。"""
    s = Scheduler()
    executed = []
    s.add_daily_task("push", lambda: executed.append(1), "16:30")
    # 模拟到点时刻
    now = datetime.datetime(2026, 7, 31, 16, 30, 0)
    for name, task in s._tasks.items():
        if task["type"] == "daily" and s._should_run_daily(task, now):
            task["func"]()
            task["last_run"] = now.date().isoformat()
    assert executed == [1]
    # 同日再次模拟 → 不重复
    for name, task in s._tasks.items():
        if task["type"] == "daily" and s._should_run_daily(task, now):
            task["func"]()
            task["last_run"] = now.date().isoformat()
    assert executed == [1]

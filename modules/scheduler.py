"""定时任务调度器

功能:
- 定时刷新行情数据
- 定时检查预警条件
- 定时生成报告
- 每天固定时刻执行(如 16:30 推送选股)

使用:
- 在app.py中初始化调度器
- 支持交易时段自动刷新
"""
import datetime
import threading
import time
import logging

logger = logging.getLogger(__name__)


def _parse_hhmm(s):
    """解析 'HH:MM' 为 (hour, minute)。非法格式返回 None。"""
    try:
        h, m = s.strip().split(":")
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return h, m
    except (ValueError, AttributeError):
        return None


class Scheduler:
    def __init__(self):
        self._tasks = {}
        self._running = False
        self._thread = None
    
    def add_task(self, name, func, interval_seconds, run_immediately=False):
        """添加定时任务(按固定间隔秒执行)"""
        self._tasks[name] = {
            "func": func,
            "type": "interval",
            "interval": interval_seconds,
            "time": None,
            "last_run": None,
            "run_immediately": run_immediately,
        }
        logger.info(f"添加定时任务: {name}, 间隔: {interval_seconds}秒")

    def add_daily_task(self, name, func, hhmm):
        """添加每日固定时刻任务, hhmm 形如 '16:30'。
        每天到点执行一次; 错过该时刻则在下一分钟补跑一次(进程重启后不丢当天任务)。
        """
        parsed = _parse_hhmm(hhmm)
        if parsed is None:
            raise ValueError(f"非法时刻: {hhmm!r}, 应为 'HH:MM'")
        self._tasks[name] = {
            "func": func,
            "type": "daily",
            "interval": None,
            "time": (parsed[0], parsed[1]),
            "last_run": None,   # iso date 字符串, 当天已跑则记录当天日期
            "run_immediately": False,
        }
        logger.info(f"添加每日任务: {name}, 时刻: {hhmm}")

    def remove_task(self, name):
        """移除定时任务"""
        if name in self._tasks:
            del self._tasks[name]
            logger.info(f"移除定时任务: {name}")
    
    def start(self):
        """启动调度器"""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("调度器已启动")
    
    def stop(self):
        """停止调度器"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("调度器已停止")
    
    def _run(self):
        """调度器主循环"""
        while self._running:
            now = datetime.datetime.now()
            
            for name, task in self._tasks.items():
                should_run = False
                
                if task["type"] == "daily":
                    should_run = self._should_run_daily(task, now)
                else:
                    if task["last_run"] is None:
                        if task["run_immediately"]:
                            should_run = True
                        else:
                            task["last_run"] = now
                    else:
                        elapsed = (now - task["last_run"]).total_seconds()
                        if elapsed >= task["interval"]:
                            should_run = True
                
                if should_run:
                    try:
                        logger.info(f"执行定时任务: {name}")
                        task["func"]()
                        task["last_run"] = (now.date().isoformat()
                                            if task["type"] == "daily" else now)
                    except Exception as e:
                        logger.error(f"定时任务 {name} 执行失败: {e}")
            
            time.sleep(10)  # 每10秒检查一次

    def _should_run_daily(self, task, now):
        """每日任务: 到点(含错过补跑)且当天未执行过。"""
        h, m = task["time"]
        today = now.date().isoformat()
        if task["last_run"] == today:
            return False
        target = datetime.time(h, m)
        # 当前时刻 >= 目标时刻(秒级容差, 时钟刚好跳变时下个循环补跑)
        return now.time().replace(microsecond=0) >= target

    def is_trading_hours(self):
        """判断当前是否为交易时段"""
        now = datetime.datetime.now()
        
        # 周末不交易
        if now.weekday() >= 5:
            return False
        
        # 交易时段: 9:30-11:30, 13:00-15:00
        current_time = now.time()
        morning_start = datetime.time(9, 30)
        morning_end = datetime.time(11, 30)
        afternoon_start = datetime.time(13, 0)
        afternoon_end = datetime.time(15, 0)
        
        return (morning_start <= current_time <= morning_end or 
                afternoon_start <= current_time <= afternoon_end)
    
    def get_next_trading_time(self):
        """获取下一个交易时段开始时间"""
        now = datetime.datetime.now()
        
        # 如果是交易日且在交易时段内，返回当前时间
        if self.is_trading_hours():
            return now
        
        # 计算下一个交易日
        next_day = now
        while next_day.weekday() >= 5:
            next_day += datetime.timedelta(days=1)
        
        # 如果是当天但已过交易时段，计算下一个交易日
        if (next_day.date() == now.date() and 
            now.time() > datetime.time(15, 0)):
            next_day += datetime.timedelta(days=1)
            while next_day.weekday() >= 5:
                next_day += datetime.timedelta(days=1)
        
        # 返回下一个交易日上午9:30
        return datetime.datetime.combine(next_day.date(), datetime.time(9, 30))


def is_trading_hours():
    """静态版本：判断当前是否为交易时段"""
    now = datetime.datetime.now()
    
    if now.weekday() >= 5:
        return False
    
    current_time = now.time()
    morning_start = datetime.time(9, 30)
    morning_end = datetime.time(11, 30)
    afternoon_start = datetime.time(13, 0)
    afternoon_end = datetime.time(15, 0)
    
    return (morning_start <= current_time <= morning_end or 
            afternoon_start <= current_time <= afternoon_end)

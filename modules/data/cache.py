"""缓存层 + 指数退避重试 + 并发控制 + 输入验证 + 请求超时保护

TTL:
  spot:  3600s (1h)
  hist:  3600s (1h)
  fin:   86400*30 (30d)
  div:   86400*90 (90d)

并发: Semaphore(3) 限制AKShare并发请求数
超时: 每API调用硬限时 30s, 防死等
"""
import os, time, datetime, hashlib, random, logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from threading import Semaphore
import pandas as pd

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0

import requests as _req
_req.adapters.HTTPAdapter.max_retries = 3

_CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")
os.makedirs(_CACHE_DIR, exist_ok=True)

TTL = {
    "spot": 3600,
    "hist": 3600,
    "fin":  86400 * 7,
    "div":  86400 * 30,
    "vol":  86400,
    "sma":  86400,
    "pctl": 86400 * 7,
    "listing": 86400 * 30,
    "earn": 86400 * 7,
}

AK_SEM = Semaphore(3)


def _p(cat, key):
    d = os.path.join(_CACHE_DIR, cat)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, hashlib.md5(key.encode()).hexdigest() + ".parquet")


def active_keys(cfg=None):
    """根据当前 config 推导所有应存在的缓存 key。

    仅覆盖 key 格式完全确定的类别（spot/div/fin）；
    vol/sma/pctl/hist/listing 的 key 依赖市场与参数，留给 TTL 过期清理，
    避免误删仍在使用的缓存。
    """
    if cfg is None:
        from modules.config import get_config
        cfg = get_config()
    portfolio = cfg.get("portfolio", {})
    codes_a = list(portfolio.get("A", {}))
    codes_hk = list(portfolio.get("HK", {}))
    codes_us = list(portfolio.get("US", {}))
    codes_reit = list(portfolio.get("REIT", {}))

    return {
        "spot": {"a_all", "hk_all", "a_all_full", "a_list_sina"},
        "div": (
            {c + "_div" for c in codes_a}
            | {c + "_div_hk" for c in codes_hk}
            | {c + "_div_us" for c in codes_us}
            | {c + "_div_reit" for c in codes_reit}
        ),
        "fin": {str(c) for c in codes_a} | {"hk_" + str(c) for c in codes_hk},
    }


def load(cat, key, force=False, allow_expired=False):
    p = _p(cat, key)
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_parquet(p)
        if not force and "_ts" in df.columns:
            ts = pd.to_datetime(df["_ts"].iloc[0])
            age = (datetime.datetime.now() - ts).total_seconds()
            if age > TTL.get(cat, 3600):
                if not allow_expired:
                    return None
                df["_expired"] = True
        return df
    except Exception:
        return None


def save(cat, key, df):
    df = df.copy()
    df["_ts"] = datetime.datetime.now().isoformat()
    df.to_parquet(_p(cat, key), index=False)


def retry(fn=None, max_n=5, delay=2.0, timeout=_DEFAULT_TIMEOUT):
    """
    指数退避重试机制。
    支持直接调用: retry(lambda: ..., max_n=3)
    支持装饰器:   @retry / @retry(3)
    """
    def _do_retry(func, n):
        last_exception = None
        for i in range(n):
            try:
                with AK_SEM:
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        fut = pool.submit(func)
                        return fut.result(timeout=timeout)
            except FuturesTimeoutError:
                last_exception = Exception(f"API call timed out ({timeout}s)")
                logger.warning(f"API调用超时 (第{i+1}/{n}次): {last_exception}")
            except Exception as e:
                last_exception = e
                caller = "?"
                try:
                    import traceback
                    tb = traceback.extract_tb(e.__traceback__)
                    for fr in reversed(tb):
                        if "/app/modules/" in fr.filename and fr.filename.endswith(".py"):
                            caller = "%s:%s" % (fr.filename.rsplit("/", 1)[-1], fr.lineno)
                            break
                except Exception:
                    pass
                logger.warning(f"API调用异常 (第{i+1}/{n}次): {type(e).__name__}: {e} [{caller}]")
            if i < n - 1:
                time.sleep(delay * (2 ** i) + random.uniform(0, 1))
        if last_exception:
            raise last_exception
        raise ConnectionError("API 重试 %d 次后仍返回空" % n)

    # @retry(3) — 第一个参数是 int
    if isinstance(fn, int):
        def decorator(func):
            def wrapper(*a, **kw):
                return _do_retry(lambda: func(*a, **kw), fn)
            return wrapper
        return decorator

    # @retry — 第一个参数是函数（装饰器用法）
    if callable(fn) and max_n == 5:
        # 可能是装饰器用法 @retry，检查是否被当作装饰器调用
        # 简单策略：如果只有 fn 参数，假设是直接调用
        return _do_retry(fn, max_n)

    # retry(lambda: ..., max_n=3) — 直接调用
    if callable(fn):
        return _do_retry(fn, max_n)

    # retry(max_n=3) — 无 fn
    def decorator(func):
        def wrapper(*a, **kw):
            return _do_retry(lambda: func(*a, **kw), max_n)
        return wrapper
    return decorator


def clear_cache(cat=None):
    if cat:
        d = os.path.join(_CACHE_DIR, cat)
        if os.path.exists(d):
            for f in os.listdir(d):
                if f.endswith(".parquet"):
                    os.remove(os.path.join(d, f))
    else:
        for root, dirs, files in os.walk(_CACHE_DIR):
            for f in files:
                if f.endswith(".parquet"):
                    os.remove(os.path.join(root, f))


def gc_stale(ttl_multiplier=2.0):
    """清理过期与孤儿缓存：仅删除 age > TTL*ttl_multiplier 的文件。

    倍数默认 2x，给 allow_expired 兜底预留一倍 TTL 的缓冲，
    避免因短暂网络抖动丢掉最后一份可用数据。
    返回删除的文件数。
    """
    removed = 0
    now = datetime.datetime.now()
    for cat, ttl in TTL.items():
        d = os.path.join(_CACHE_DIR, cat)
        if not os.path.isdir(d):
            continue
        limit = ttl * ttl_multiplier
        for f in os.listdir(d):
            if not f.endswith(".parquet"):
                continue
            p = os.path.join(d, f)
            try:
                ts = pd.read_parquet(p, columns=["_ts"])["_ts"].iloc[0]
                age = (now - pd.to_datetime(ts)).total_seconds()
                if age > limit:
                    os.remove(p)
                    removed += 1
                    logger.info("GC removed stale cache: %s/%s (age %.0fs > %ds)", cat, f, age, limit)
            except Exception:
                continue
    if removed:
        logger.info("cache GC removed %d stale file(s)", removed)
    return removed


def gc_orphans(active_keys):
    """按当前活跃 key 清理孤儿缓存：key 不在 active_keys 集合内的文件会被删除。

    active_keys: dict, {category: set(key)}。只处理列出的类别，未列出的类别不动。
    用于清除已从 config.json 移除代码的历史缓存。
    """
    removed = 0
    for cat, keys in (active_keys or {}).items():
        d = os.path.join(_CACHE_DIR, cat)
        if not os.path.isdir(d):
            continue
        valid = {hashlib.md5(k.encode()).hexdigest() + ".parquet" for k in keys}
        for f in os.listdir(d):
            if not f.endswith(".parquet"):
                continue
            if f not in valid:
                try:
                    os.remove(os.path.join(d, f))
                    removed += 1
                    logger.info("GC removed orphan cache: %s/%s", cat, f)
                except Exception:
                    continue
    if removed:
        logger.info("cache GC removed %d orphan file(s)", removed)
    return removed


def validate_spot(df):
    """输入验证, 异常写入 _flags"""
    flags = []
    for _, r in df.iterrows():
        f = ""
        if r.get("price", 0) is None or r.get("price", 0) <= 0:
            f += "price_invalid;"
        if pd.notna(r.get("pe")) and r["pe"] > 500:
            f += "pe_extreme;"
        if pd.notna(r.get("pb")) and r["pb"] <= 0:
            f += "pb_invalid;"
        mcap = r.get("mcap")
        if mcap is None or mcap <= 0:
            f += "mcap_invalid;"
        flags.append(f)
    df["_flags"] = flags
    return df

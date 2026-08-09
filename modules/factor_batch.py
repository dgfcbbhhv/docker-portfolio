"""Alpha 因子批量训练后台线程（共享给 app.py 与 render/t9）。
import logging

logger = logging.getLogger(__name__)
训练/进化计算放后台线程执行，脚本侧轮询进度；线程内不调用任何 st.* API。
模块级可变状态 `FACTOR_BATCH` 由 app 与渲染侧共享同一对象。
"""

import datetime
import threading

FACTOR_BATCH = {"running": False, "done": False, "total": 0, "i": 0,
                "results": {}, "ts": None, "error": None}
FACTOR_BATCH_LOCK = threading.Lock()


def _run_batch(watch_codes, do_train):
    from modules.config import WATCH_LIST
    from modules.data.app_cache import fetch_factor_history_cached as _hist
    from modules.factor_selector import get_factor_dynamic_bonus
    from modules.learner import get_learner

    with FACTOR_BATCH_LOCK:
        FACTOR_BATCH.update(running=True, done=False, total=len(watch_codes),
                            i=0, results={}, ts=None, error=None)
    try:
        learner = get_learner()
        for idx, code in enumerate(watch_codes):
            with FACTOR_BATCH_LOCK:
                FACTOR_BATCH["i"] = idx + 1
            name = WATCH_LIST[code]["name"]
            market = WATCH_LIST[code]["market"]
            try:
                hist_df = _hist(code, market)
                if hist_df is None or hist_df.empty:
                    with FACTOR_BATCH_LOCK:
                        FACTOR_BATCH["results"][code] = {"name": name, "error": "无历史数据", "hist_df": None}
                    continue
                if do_train or not learner.is_trained(code):
                    learner.train(code, hist_df)
                else:
                    learner.update(code, hist_df)
                result = learner.get_learned_recommendation(code, hist_df)
                train_info = learner.get_training_info(code)
                bonus, _ = get_factor_dynamic_bonus(code, hist_df)
                evolution = learner.compute_evolution(code, hist_df)
                evo_summary = learner.get_evolution_summary(evolution) if evolution else None
                with FACTOR_BATCH_LOCK:
                    FACTOR_BATCH["results"][code] = {
                        "name": name, "market": market, "hist_df": hist_df,
                        "result": result, "train_info": train_info, "bonus": bonus,
                        "evolution": evolution, "evo_summary": evo_summary}
            except Exception as e:
                with FACTOR_BATCH_LOCK:
                    FACTOR_BATCH["results"][code] = {"name": name, "error": str(e), "hist_df": None}
        with FACTOR_BATCH_LOCK:
            FACTOR_BATCH["ts"] = datetime.datetime.now().isoformat()
    except Exception as e:
        with FACTOR_BATCH_LOCK:
            FACTOR_BATCH["error"] = str(e)
    finally:
        with FACTOR_BATCH_LOCK:
            FACTOR_BATCH["done"] = True
            FACTOR_BATCH["running"] = False
            FACTOR_BATCH["i"] = FACTOR_BATCH["total"]


def start_batch(watch_codes, do_train):
    if FACTOR_BATCH["running"]:
        return False
    threading.Thread(target=_run_batch, args=(watch_codes, do_train),
                     daemon=True, name="factor-batch").start()
    return True

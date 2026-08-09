"""render 包共享状态与渲染边界防御。

- `_renderable_df` + 对 st 的猴补丁：在渲染边界统一兜底，防止"文本+数字混排"列
  触发 pyarrow 推断失败导致页面崩溃。import 本模块即应用该补丁。
- `df()/layer_summaries()`：从 st.session_state 读取当前数据。
"""

import streamlit as st
import pandas as pd
import logging
logger = logging.getLogger(__name__)


def _renderable_df(df):
    """防御性序列化清理：外部数据列可能把 `—`/缺省文本与数字混在一个 object 列里，
    pyarrow 推断 int64 时会抛 ArrowInvalid。这里将"文本+数字混排"的列统一转字符串。"""
    try:
        if df is None or getattr(df, "empty", True) or isinstance(df, str):
            return df
    except Exception:
        return df
    try:
        out = df.copy()
    except Exception:
        return df
    for col in out.columns:
        try:
            s = out[col]
            if s.dtype != object:
                continue
            non_null = s.dropna()
            if len(non_null) == 0:
                continue
            has_str = any(v.__class__ is str for v in non_null.tolist())
            has_num = any(isinstance(v, (int, float)) and not isinstance(v, bool)
                          for v in non_null.tolist())
            if has_str and has_num:
                out[col] = s.astype(str)
        except Exception:
            continue
    return out


def apply_st_safe():
    """对 st 的 DataFrame 相关 API 加渲染边界防御补丁（幂等）。"""
    for _df_api in ("dataframe", "data_editor", "line_chart", "area_chart",
                    "bar_chart", "scatter_chart"):
        _orig = getattr(st, _df_api, None)
        if _orig is None:
            continue

        def _safe(*_args, _name=_df_api, _orig=_orig, **kwargs):
            args = list(_args)
            if args and isinstance(args[0], pd.DataFrame):
                args[0] = _renderable_df(args[0])
            return _orig(*args, **kwargs)

        _safe.__name__ = _df_api
        setattr(st, _df_api, _safe)


def df():
    return st.session_state.get("df")


def layer_summaries():
    return st.session_state.get("layer_summaries", [])

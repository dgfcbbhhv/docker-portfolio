"""统一路径解析，消除散落的 __file__ 相对路径。

原则
----
- 运行根目录（repo_root）可由环境变量 `PORTFOLIO_ROOT` 覆盖，否则取 modules 的上一级。
- data/notes 目录统一由此解析，模块不再各自 `__file__/../..` 拼路径
  （那样在本地与容器层级不同时会错位）。
- 容器里 repo_root 一般是 /app；本地是仓库根。
"""

import os


def repo_root():
    """仓库根目录：优先环境变量 PORTFOLIO_ROOT，否则取本模块上一级。"""
    env = os.environ.get("PORTFOLIO_ROOT")
    if env:
        return os.path.abspath(env)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def data_dir():
    """data 目录（行情缓存、broker_trades、execution_log、paper_portfolio 等）。"""
    env = os.environ.get("PORTFOLIO_DATA_DIR")
    if env:
        return os.path.abspath(env)
    return os.path.join(repo_root(), "data")


def notes_dir():
    """notes 目录（cost_tiers、trades 等人工维护数据）。"""
    return os.path.join(repo_root(), "notes")

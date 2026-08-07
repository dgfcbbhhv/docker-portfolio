# ADR-0001: 依赖锁定与镜像源选择

日期：2026-08-07
状态：已采纳

## 背景

Dockerfile 构建依赖 `pip install`。历史存在两个真实问题：

1. `requirements.lock.txt` 是从**旧生产容器** `pip freeze` 生成的完整冻结清单，
   其内容（例如 `akracer==0.0.14`）在声明的 tuna 镜像源
   （`pypi.tuna.tsinghua.edu.cn`）**不可安装**，导致 `docker compose build` 失败。
   该 lock 从未被真实构建成功使用过（此前是 UTF-16 坏文件，之后是不可装的伪锁）。
2. tuna 镜像源对部署机 IP 返回 HTTP 200 但正文为封锁页（"抱歉，您目前无法访问此页面"），
   pip 解析结果全部为 `from versions: none`。

## 决策

1. **镜像源改用官方 PyPI**（`pypi.org/simple`），Dockerfile 中移除
   `-i https://pypi.tuna.tsinghua.edu.cn/simple` 参数。
   实测 tuna 对 NAS IP 封锁、pypi.org HTTP 200 正常。
   代价：国际带宽较慢（约 200KB/s），完整构建约 20 分钟，但稳定可完成。
2. **`requirements.lock.txt` 的生成来源定义为"从干净镜像冻结"**：
   基于官方 PyPI 用 `requirements.txt` 构建成功后的运行容器执行
   `python -m pip freeze` 导出。该集合与生产运行状态一一对应，全部可从 pypi.org 安装。
   不要再用手工或旧环境 freeze 冒充锁。

## 关键事实（实测）

- `akracer==0.0.14` 是 `akshare` 的**真实传递依赖**（`Required-by: akshare`），
  官方 PyPI 存在，tuna 缺失。它没有可导入的顶层模块名（`import akracer` 失败），
  但不影响 akshare 运行。
- 新锁 70 包，本地与 NAS md5 一致，构建走 `pip install -r requirements.lock.txt` 成功。

## 影响

- 重新构建镜像耗时约 20 分钟（此前 tuna 更快，但不可用）。
- 复现性：锁与运行环境一致，未来构建可复现同版本集合。

## 备选方案（未采纳）

- 保留 tuna 并删掉 `akracer`：会把一个真实传递依赖从锁中抹掉，导致依赖漂移。
- 使用默认官方源但手动裁剪 freeze：不可维护，且非来自真实运行环境。

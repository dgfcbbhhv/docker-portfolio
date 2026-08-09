# NAS 组合系统修复与长桥数据源接入 — 全程记录

> 记录周期:2026-08-08 ~ 2026-08-09
> 项目:`/vol1/1000/docker-portfolio/`(NAS,dividend-portfolio / dividend-alert-worker 双容器)
> 本地工作副本:`C:\Users\weixiao\AppData\Local\Temp\opencode\nas_portal\`
> D 盘镜像:`D:/weixiao/Documents/Codex/2026-07-17/new-chat/outputs/docker-portfolio/`

---

## 一、本轮修复总目标

1. 每日选股文案诚实化,标题与文案与"候选池体检"定位匹配。
2. 回测三场景修复(长线/中线/短线 ROE 代理),港股纳入回测。
3. 清理根目录一次性脚本、冗余缓存、历史 `.bak`。
4. **激活长桥(Longbridge)OpenAPI 作三市场实时行情主源**,含自动 token 保鲜。
5. 修复数据源 fallback 多级补齐逻辑。

---

## 二、已完成修改汇总

### 1. daily_picks.py(候选文案诚实化)
- `daily_picks_title`: `今日价值投资股票观察` → `今日持仓+候选池体检`
- `_preamble` → `持仓+候选观察清单`
- `_footer` 恢复正确文案(李嘉诚)
- `ACTION_LABEL` 恢复原四 `key`(可建仓/可加仓/减仓/可关注)
- 备份 → 上传 → py_compile → 重启 → D 盘 md5 同步。

### 2. backtest.py(回测三场景 + 港股)
- `backtest_scenarios` 三场景改用 ROE 代理,与实测名称匹配。
- `port_hk` 合并进回测;`backtest_portfolio` 增加 `mkt_map` 参数。
- `render/t4.py` 合并拉取 `fetch_fins_hk`,港股 16 只入回测。

### 3. 清理
- 根目录 36 个一次性调试脚本。
- 冗余 `modules/modules/`(3.3M)。
- 7 个 modules 下 `.bak`、3 个 `app.py.bak`、`app.py.bak_split`、`backups/`。
- 统一归档至 `.backup-20260808-fix/`(含 `stale_baks/`、`legacy_scripts/`、`modules_modules_backup/`)。

### 4. 持仓名单
- `watch_list`:21 → 23,新增 `600028 中国石化`、`00939 建设银行H`。
- dry-run 验证:600028 因评分未达阈值/数据不足,机制正常跳过。

### 5. 长桥数据源接入(核心)
见第三节。

---

## 三、长桥接入 + token 轮换事故全过程

### 3.1 接入背景
- 依赖在 `requirements.lock.txt` 增加 `longbridge==4.4.3`(BOM+CRLF 保留)。
- `longbridge.py` 修改:
  - `_to_lb_symbol`:REIT(50 开头)并入 SH;6 位 A 股前缀判断修正。
  - `_http_url()`:`LONGBRIDGE_REGION=cn` → `https://openapi.longbridge.cn`(CN 端点)。
  - `_build_config()` 使用 `http_url=_http_url()`。
  - `_refresh_if_needed()`:每 60 天自动执行 `Config.refresh_access_token()`,成功则写回 `.env`(fail-safe,失败保留旧 token 不抛)。
- `fallback.py`:多源合并补缺(长桥 → 东方财富 → 新浪 → 腾讯)。
- `market.py`:`fetch_spot_a/fetch_spot_hk` 接入 `fetch_spot_with_fallback`。

### 3.2 关键事实(多轮验证)
- 容器用挂载卷 `./modules:/app/modules`,**改宿主 modules 文件即生效**,无需进容器改。
- `docker restart` **不重新注入 `.env`**;必须 `docker compose up -d --force-recreate` 才会加载新 `.env` 变量。
- **容器重建会导致手工 pip 安装的 longbridge SDK 丢失**,需再 `docker exec <container> pip install longbridge==4.4.3`。
- token 手抄极易污染 → 全程用文件(scp autoritative片段)+ 脚本替换,以 md5 校验。

### 3.3 token 轮换事故(关键)
1. 初版 token 曾因手动 refresh / 多容器并发调用,长桥端判定 **401004 token invalid**。
2. GitHub issue #822/#877 佐证:**重置访问 token 的账户后,旧 token 失效;多次手动 refresh 风险高**。
3. 用户从长桥后台**重新生成**新 token(过期 ≈ 2026-11-05)。
4. 更新 NAS `.env`(backup 至 `.before-finaltoken`)+ D 盘副本,`--force-recreate` 注入。
5. recreate 后 worker 单独验证:`fetch_spot(['600900','00002','00006','AAPL'])` → **WORKER_LB 4 只全部出数**(600900=27.75, 00002=77.6, 00006=57.7, AAPL=313.33),token hash `e6d57 30e`。
6. **但 portfolio 容器同 token 同代码同端点却 401004** → 两容器并发同一 token 触发互踢/风控的嫌疑最大。
7. 补充修复:compose anchor `alert-env` 原来不映射 `LONGBRIDGE_TOKEN_REFRESHED`,导致容器首次调用一定触发 `_refresh_if_needed()`(不该触发的时机),增加扰动。
   - 已在 compose 增加 `LONGBRIDGE_TOKEN_REFRESHED: ${LONGBRIDGE_TOKEN_REFRESHED:-}`(备份 `docker-compose.yml.before-REFRESHED-mapping`)。
8. 再次 recreate 两容器 + 重装 SDK 后,**两容器 REFRESHED=2026-08-09 均正确,但又双双 401**。

### 3.4 结论与决策(截至最后一轮动作)
- worker 容器曾用**同一个新 token 成功出数** → token 本身有效。
- 出现"同 token 串行 OK、并行/多次触发 401"的强烈证据,最可能是**长桥端对该应用 access token 的并发/轮换策略**:任一并发连接或 refresh 尝试可能使 token 进入 invalid 状态。
- 处理原则(与用户约定):**拿到新 token 后只更新 `.env` + 验证一次,绝不触发 refresh**;两容器避免同一时刻并发调用;必要时仅保留单一进程作为长桥客户端。

---

## 四、验证命令速查(SSH 到 NAS)

### 容器环境变量
```
docker exec dividend-portfolio env | grep LONGBRIDGE
docker exec dividend-alert-worker env | grep LONGBRIDGE
```

### 单容器长桥报价(串行!不要并发)
```
docker exec dividend-portfolio python -c \
"import logging; logging.basicConfig(level=logging.WARNING); \
import modules.data.longbridge as m; \
print(len(r:=m.fetch_spot(['600900','00002','00006','AAPL'])), [(x['c'],x['price']) for x in r])"
```

### token 一致性
```
grep -o 'LONGBRIDGE_ACCESS_TOKEN=.*' /vol1/1000/docker-portfolio/.env | md5sum
docker exec dividend-portfolio python -c "import os,hashlib;print(hashlib.md5(os.environ['LONGBRIDGE_ACCESS_TOKEN'].encode()).hexdigest())"
```

### 重建 + 装 SDK(重 build 则用 lock)
```
cd /vol1/1000/docker-portfolio && docker compose up -d --no-deps --force-recreate
docker exec dividend-portfolio pip install longbridge==4.4.3
docker exec dividend-alert-worker pip install longbridge==4.4.3
```

---

## 五、遗留与后续 TODO

### 本轮(2026-08-09 10:20)处理结果

- [x] **连接策略落地(单一长桥客户端)**:`docker-compose.yml` 给 `portfolio` 增加 `MODE: portfolio`;`entrypoint.sh` 新增该分支 → **只跑 Streamlit,不再内嵌 alert_worker**。专职 `alert-worker` 容器成为系统唯一长桥客户端。
  - 验证:portfolio 容器 `alert_worker` 进程数 = 0,pid1 = `streamlit run app.py`;worker 容器进程数 = 1(`python -m modules.alert_worker`),SDK ok。
  - 重建手续:`docker compose up -d --no-deps --force-recreate portfolio` 后 **SDK 丢失**,已 `docker exec dividend-portfolio pip install longbridge==4.4.3` 补回。
- [x] **验证 60 天 guard 有效** — 容器内 `LONGBRIDGE_TOKEN_REFRESHED=2026-08-09`(今天),`_refresh_if_needed()` 距上次 <60 天直接 return;启动后日志无任何 refresh 尝试(仅 401004)。
- [x] **token 一致性确认** — host `.env` 行尾含 `\r`(CRLF);`tr -d "\r"` 后 md5=`e6d5730e…` 与容器内 token 完全一致 → 容器运行的就是目标新 token,非陈旧值。
- [x] **备份目录位置确认** — `.backup-20260808-fix/` 保留,含 `entrypoint.sh.after-singleworker`、`docker-compose.yml.before-singleworker` 等本轮新增备份;未做物理清理(保守保留)。

### 仍留待用户操作 / 观察

- [x] **长桥 token 已重新生成并生效(2026-08-09)** — exp=1794024472 新 token 由用户后台重新生成。全程**文件直传**(`lb_token.txt` → scp),不做手抄;脚本自动解码 JWT payload 校验(exp/iat/ak/sub)后写入 `.env`。三容器已被 `--force-recreate` 注入同样 token(md5 一致 615fc98d…)。
  - 串行单容器验证:`fetch_spot(['600900','00002','00006','AAPL'])` → **4 只全部出数**(600900=27.75, 00002=77.6, 00006=57.7, AAPL=313.33);市场权限:HK/US LV1 实时、CN LV1 实时、USOption 需购买。
  - 注意:header 首段含 `hk_m_` 类型前缀,解码前需 `replace('hk_m_', '')`; `.env` 行尾带 `\r`(CRLF),比较 token 时需 `tr -d "\r"`。
- [x] **fallback 多源优先级确认** — `modules/data/fallback.py` 顺序已核:longbridge → eastmoney/sina → tencent;长桥恢复后仍领跑。
- [ ] 观察一段时间,确认 portfolio(前端)与 alert-worker(唯一后台)串行下无新的 401004。

---

## 六、关键要点(易踩坑)
- **改 host modules/ 即生效,但 `.env` 仅 recreate 才注入。**
- **recreate 丢 SDK → 重装 longbridge==4.4.3。**
- **长桥 access token 一句话:别并发、别反复 refresh、别手抄。**
- 所有改动均已有 `.backup-20260808-fix/` 对应备份。
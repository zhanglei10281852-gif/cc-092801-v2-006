# 红白喜事服务运营平台

这是一个面向婚庆公司、殡葬服务机构和现场调度人员的 Python 后端服务，用于管理服务套餐、家庭订单、现场执行队列、服务人员、结果版本和运营干预。服务保留登录、角色权限、会话、审计和配额等基础能力，所有业务状态与审计事件写入本地 SQLite 数据库，适合在单个应用容器中离线运行。

## 运行环境

- Python 3.11
- SQLite 3（由 Python 标准库提供）
- FastAPI 与 Uvicorn

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/ceremony-operations.db`，可复制 `.env.example` 并设置 `TOWNSHIP_DATABASE_PATH` 指向其他本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

服务订单运营接口使用 `/api/compute` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`。仪式流程依赖图（版本化 DAG、阻塞传播、人工放行、领取游标）位于 `/api/process`。

## 测试与编译检查

```bash
python -m pytest
python -m compileall -q app tests
```

本地冒烟命令：

```bash
python -m app.cli smoke
python -m app.cli compute-demo
```

## 目录结构

```text
app/compute/       任务模板、配额、提交、领取、回执和人工干预
app/process/       仪式流程依赖图：版本化 DAG、阻塞传播、人工放行、领取游标
app/api/            登录、角色、审计和系统管理接口
app/core/           时钟、安全、异常和分页能力
app/repositories/   SQLite 查询与事务封装
app/services/       身份、审计和后台任务服务
app/database.py     SQLite 连接、事务、表结构和权限初始化
tests/              领域、接口、调度和身份回归测试
tools/              本地维护脚本
```

## 仪式流程依赖图（/api/process）

每场葬礼对应一个流程定义（`flows`）和一个仪式实例（`instances`）。

- **带版本的步骤关系**：`POST /api/process/flows/{code}/versions` 提交步骤与边；提交时进行 Kahn 拓扑校验，循环、自环、重复边和引用不存在步骤的关系一律以 422 拒绝；内容相同的提交幂等返回当前版本，新版本号单调递增并保留全部历史版本与 `digest`。
- **历史不可改写**：仪式实例创建时绑定当时的图版本（`graph_version_id`），后续图更新只影响新实例；已完成步骤（`succeeded`/`failed`）与已执行的人工放行不会被依赖重算覆盖。
- **领取自动跳过未满足前置**：`POST /api/process/claim` 只派发 `ready` 且 `available_at` 已到的步骤，`waiting`/`blocked`/`running` 的步骤不会占用人员；返回稳定拓扑顺序（实例创建顺序 + 拓扑序号 + run id）、持久化 `cursor`，重启后游标与状态一致，游标越界自动回绕并标记 `wrapped`。
- **失败传播规则**：可重试失败按指数退避回到 `ready`；重试耗尽的失败沿依赖边向下传播为 `blocked`，`blocked_by_run_id` 追溯到根因步骤，`blocked_reason` 给出原因，`blockers` 列出直接阻塞前置；`GET /api/process/blocked` 汇总全部被阻塞环节。
- **人工放行**：`POST /api/process/runs/{run_id}/release` 可放行被阻塞步骤，必须填写理由，放行记录（操作人、理由、时间、阻塞来源）写入 `flow_manual_releases`；放行步骤完成后其下游照常推进。
- **变更记录**：图版本提交、实例创建、领取、成功、失败、阻塞、放行、租约恢复均写入 `flow_change_log`，可通过 `/flows/{code}/changes` 与 `/instances/{id}/changes` 查询。
- 租约过期由 `POST /api/process/recovery/expired-leases` 回收：仍有重试次数则重新就绪，耗尽则判失败并传播阻塞。

## 数据一致性

SQLite 连接启用外键、WAL 和忙等待策略。提交、领取、回执和人工干预在即时事务中完成；租约、配额与结果版本使用可注入时钟，便于复现跨日和恢复边界。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。

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

服务订单运营接口使用 `/api/compute` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`。仪式流程依赖图接口使用 `/api/ceremonies` 前缀。

## 仪式流程依赖图（/api/ceremonies）

针对葬礼中遗像制作、灵堂布置、车辆调度、告别仪式等环节的先后依赖，平台为每场仪式维护可持久化的流程依赖图：

- `POST /api/ceremonies` 创建仪式；`POST /api/ceremonies/{code}/graph` 提交带版本的步骤关系（步骤 + 前置边）。每次提交生成递增版本号，历史版本不可改写，可通过 `GET /api/ceremonies/{code}/graph/versions` 与 `.../versions/{n}` 回看；内容完全相同的重复提交会被去重（返回 200 与 `deduplicated: true`）。
- 提交时校验：引用不存在的步骤、步骤编码或边重复、自环与循环依赖都会被拒绝（422）；拓扑顺序在提交时按字典序确定性计算并持久化，查询结果稳定。
- 每条前置边带有失败规则：`block`（前置失败则阻塞后续，默认）或 `ignore`（忽略前置失败）。步骤状态包括 `pending`（等待前置）、`ready`（可领取）、`running`（执行中）、`succeeded`、`failed`、`blocked`。
- `POST /api/ceremonies/{code}/claim` 按稳定拓扑顺序领取第一个 `ready` 步骤，自动跳过前置未满足的环节，响应中的 `skipped` 列出被跳过步骤及其阻塞原因；可用 `step_keys` 限定可领取的步骤范围。
- 步骤失败（`POST .../steps/{key}/fail`）后，阻塞沿 `block` 边向下游传播并记录原因；`POST .../steps/{key}/release` 可人工放行被阻塞步骤，放行人与理由持久化在步骤状态与变更记录中。放行对该步骤持续有效，新版本提交后仍然保留。
- 依赖更新不会改写已完成的历史：`succeeded`/`failed`/`running` 步骤的状态跨版本保留，其余步骤按新版本重算；全部步骤成功后仪式自动办结。
- `GET /api/ceremonies/{code}` 返回稳定拓扑顺序、每步状态、阻塞原因与逐边前置满足情况；`GET /api/ceremonies?blocked_only=true` 只列出存在阻塞环节的仪式。
- `GET /api/ceremonies/{code}/events?cursor=&limit=` 以键集游标分页返回每次变更记录（提交、领取、就绪、阻塞、放行、完成等），游标为事件 id，重启后翻页与状态保持一致。

## 测试与编译检查

```bash
python -m pytest
python -m compileall -q app tests
```

本地冒烟命令：

```bash
python -m app.cli smoke
python -m app.cli compute-demo
python -m app.cli ceremony-demo
```

## 目录结构

```text
app/compute/        任务模板、配额、提交、领取、回执和人工干预
app/ceremony/       仪式流程依赖图、版本化步骤关系、失败传播与人工放行
app/api/            登录、角色、审计和系统管理接口
app/core/           时钟、安全、异常和分页能力
app/repositories/   SQLite 查询与事务封装
app/services/       身份、审计和后台任务服务
app/database.py     SQLite 连接、事务、表结构和权限初始化
tests/              领域、接口、调度和身份回归测试
tools/              本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL 和忙等待策略。提交、领取、回执和人工干预在即时事务中完成；租约、配额与结果版本使用可注入时钟，便于复现跨日和恢复边界。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。

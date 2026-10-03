from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.process.repository import ProcessRepository, ensure_schema

SYSTEM_ACTOR = "process-engine"


def graph_digest(steps: list[dict[str, Any]], edges: list[dict[str, str]]) -> str:
    payload = {
        "steps": [{k: step[k] for k in ("code", "name", "kind", "required_role", "priority", "max_attempts")} for step in steps],
        "edges": sorted((edge["from"], edge["to"]) for edge in edges),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def topological_order(steps: list[dict[str, Any]], edges: list[dict[str, str]]) -> list[str]:
    """Kahn 拓扑排序；同一层按提交序号、编码稳定排序。若有环或引用了不存在的步骤则抛出 ValidationError。"""
    codes = [step["code"] for step in steps]
    code_set = set(codes)
    if len(code_set) != len(codes):
        duplicates = sorted({code for code in codes if codes.count(code) > 1})
        raise ValidationError("步骤编码重复", context={"steps": duplicates})
    predecessors: dict[str, set[str]] = {code: set() for code in codes}
    successors: dict[str, set[str]] = {code: set() for code in codes}
    seen_edges: set[tuple[str, str]] = set()
    for edge in edges:
        source = edge["from"]
        target = edge["to"]
        if source not in code_set or target not in code_set:
            missing = sorted({code for code in (source, target) if code not in code_set})
            raise ValidationError("依赖关系引用了不存在的步骤", context={"steps": missing})
        if source == target:
            raise ValidationError("步骤不能依赖自身", context={"steps": [source]})
        if (source, target) in seen_edges:
            raise ValidationError("依赖关系重复", context={"edge": {"from": source, "to": target}})
        seen_edges.add((source, target))
        predecessors[target].add(source)
        successors[source].add(target)
    order_index = {code: index for index, code in enumerate(codes)}
    ready = sorted((code for code in codes if not predecessors[code]), key=lambda code: (order_index[code], code))
    ordered: list[str] = []
    while ready:
        code = ready.pop(0)
        ordered.append(code)
        for successor in sorted(successors[code], key=lambda item: (order_index[item], item)):
            predecessors[successor].discard(code)
            if not predecessors[successor]:
                ready.append(successor)
        ready.sort(key=lambda item: (order_index[item], item))
    if len(ordered) != len(codes):
        remaining = sorted(code_set - set(ordered))
        raise ValidationError("步骤依赖中存在循环关系", context={"steps": remaining})
    return ordered


class ProcessFlowService:
    """仪式流程依赖图：版本化 DAG、领取跳阻塞、失败传播与人工放行。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        ensure_schema()
        self.repository = ProcessRepository(self.connection)

    # ---------- 流程与图版本 ----------

    def create_flow(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ProcessRepository(connection)
            if repository.flow_by_code(payload["code"]):
                raise ConflictError("仪式流程编码已存在")
            flow = repository.create_flow(code=payload["code"], name=payload["name"], now=now)
            repository.add_change(flow_id=flow["id"], instance_id=None, entity_type="flow", entity_id=flow["id"],
                                  action="create", actor=actor, reason="", before=None, after=dict(flow), now=now)
            return self._flow_detail(repository, flow["id"])

    def submit_graph(self, flow_code: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        steps = payload["steps"]
        raw_edges = payload["edges"]
        edges = [{"from": edge.get("from", edge.get("from_step")), "to": edge.get("to", edge.get("to_step"))}
                 for edge in raw_edges]
        for step in steps:
            step.setdefault("required_role", "")
        order = topological_order(steps, edges)  # 提交时拒绝环、自环、重复边与不存在的步骤
        digest = graph_digest(steps, edges)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ProcessRepository(connection)
            flow = repository.flow_by_code(flow_code)
            if flow is None:
                raise NotFoundError("仪式流程不存在")
            latest = repository.latest_version(flow["id"])
            if latest is not None and latest["digest"] == digest:
                return self._version_detail(repository, flow, latest)
            version_number = 1 if latest is None else int(latest["version"]) + 1
            version_id = repository.insert_version(flow_id=flow["id"], version=version_number, digest=digest,
                                                   note=payload.get("note", ""), created_by=actor, now=now)
            step_ids: dict[str, int] = {}
            for ordinal, step in enumerate(steps):
                step_ids[step["code"]] = repository.insert_step(graph_version_id=version_id, flow_id=flow["id"], step=step, ordinal=ordinal)
            for edge in edges:
                repository.insert_edge(version_id, step_ids[edge["from"]], step_ids[edge["to"]])
            repository.set_version_counts(version_id, len(steps), len(edges))
            repository.bump_flow_version(flow["id"], version_number, now)
            version = repository.version_by_id(version_id)
            assert version is not None
            repository.add_change(flow_id=flow["id"], instance_id=None, entity_type="graph_version", entity_id=version_id,
                                  action="submit_version", actor=actor, reason=payload.get("note", ""),
                                  before=None, after={"version": version_number, "digest": digest, "steps": len(steps), "edges": len(edges)}, now=now)
            return self._version_detail(repository, flow, version)

    def get_flow(self, flow_code: str) -> dict[str, Any]:
        flow = self.repository.flow_by_code(flow_code)
        if flow is None:
            raise NotFoundError("仪式流程不存在")
        return self._flow_detail(self.repository, flow["id"])

    def list_versions(self, flow_code: str) -> dict[str, Any]:
        flow = self.repository.flow_by_code(flow_code)
        if flow is None:
            raise NotFoundError("仪式流程不存在")
        return {"flow_code": flow_code, "items": [self._version_detail(self.repository, flow, row)
                                                  for row in self.repository.list_versions(flow["id"])]}

    def flow_changes(self, flow_code: str, limit: int = 100) -> dict[str, Any]:
        flow = self.repository.flow_by_code(flow_code)
        if flow is None:
            raise NotFoundError("仪式流程不存在")
        return {"flow_code": flow_code, "items": [self._change(row) for row in self.repository.changes_for_flow(flow["id"], limit)]}

    # ---------- 仪式实例 ----------

    def create_instance(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ProcessRepository(connection)
            if repository.instance_by_business_key(payload["business_key"]):
                raise ConflictError("仪式业务单号已存在")
            flow = repository.flow_by_code(payload["flow_code"])
            if flow is None:
                raise NotFoundError("仪式流程不存在")
            latest = repository.latest_version(flow["id"])
            if latest is None:
                raise ConflictError("流程尚未提交任何步骤关系版本")
            if payload.get("graph_version"):
                pinned = repository.version_by_number(flow["id"], int(payload["graph_version"]))
                if pinned is None:
                    raise NotFoundError("指定的图版本不存在")
            else:
                pinned = latest
            instance_id = repository.create_instance(flow_id=flow["id"], graph_version_id=pinned["id"],
                                                     business_key=payload["business_key"],
                                                     title=payload.get("title", ""), created_by=payload["created_by"], now=now)
            graph = self._load_graph(repository, pinned["id"])
            for step_id, step in graph["steps"].items():
                repository.insert_run(instance_id=instance_id, step_id=step_id,
                                      max_attempts=step["max_attempts"], available_at=now, now=now)
            instance = repository.instance_by_id(instance_id)
            assert instance is not None
            self._sweep(repository, instance, actor=SYSTEM_ACTOR, now=now, log_transitions=False)
            repository.add_change(flow_id=flow["id"], instance_id=instance_id, entity_type="instance", entity_id=instance_id,
                                  action="create", actor=payload["created_by"], reason="",
                                  before=None, after={"business_key": payload["business_key"], "graph_version": pinned["version"]}, now=now)
            return self._instance_detail(repository, repository.instance_by_id(instance_id))

    def get_instance(self, instance_id: int) -> dict[str, Any]:
        instance = self.repository.instance_by_id(instance_id)
        if instance is None:
            raise NotFoundError("仪式实例不存在")
        return self._instance_detail(self.repository, instance)

    def instance_changes(self, instance_id: int, limit: int = 100) -> dict[str, Any]:
        instance = self.repository.instance_by_id(instance_id)
        if instance is None:
            raise NotFoundError("仪式实例不存在")
        return {"instance_id": instance_id, "items": [self._change(row) for row in self.repository.changes_for_instance(instance_id, limit)]}

    def list_blocked(self, flow_code: str | None = None) -> dict[str, Any]:
        flow_id = None
        if flow_code:
            flow = self.repository.flow_by_code(flow_code)
            if flow is None:
                raise NotFoundError("仪式流程不存在")
            flow_id = flow["id"]
        items: list[dict[str, Any]] = []
        for instance_row in self.repository.active_instances(flow_id):
            detail = self._instance_detail(self.repository, self.repository.instance_by_id(instance_row["id"]))
            items.extend({"instance_id": detail["instance_id"], "business_key": detail["business_key"], **step}
                         for step in detail["steps"] if step["status"] == "blocked")
        return {"items": items}

    # ---------- 领取、租约与执行回执 ----------

    def claim(self, worker_id: str, capabilities: list[str], lease_seconds: int,
              ceremony_code: str | None = None, cursor_token: str | None = None) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        lease_until = to_storage(now_value + timedelta(seconds=lease_seconds))
        with transaction(immediate=True) as connection:
            repository = ProcessRepository(connection)
            flow_id = None
            if ceremony_code:
                flow = repository.flow_by_code(ceremony_code)
                if flow is None:
                    raise NotFoundError("仪式流程不存在")
                flow_id = flow["id"]
            cursor_row = repository.cursor(worker_id)
            last_id = 0
            if cursor_token is not None:
                last_id = self._decode_cursor(cursor_token)
            elif cursor_row is not None:
                last_id = int(cursor_row["last_run_id"])
            candidates = repository.ready_runs(now=now, flow_id=flow_id, capabilities=capabilities)
            candidate = next((row for row in candidates if row["id"] > last_id), None)
            wrapped = candidate is None
            if wrapped and candidates:
                candidate = candidates[0]
            if candidate is None:
                return {"run": None, "cursor": str(last_id), "wrapped": False}
            cursor = connection.execute(
                "UPDATE flow_step_runs SET status='running',attempt_count=attempt_count+1,lease_owner=?,lease_expires_at=?,"
                "started_at=COALESCE(started_at,?),version=version+1,updated_at=? WHERE id=? AND status='ready'",
                (worker_id, lease_until, now, now, candidate["id"]),
            )
            if cursor.rowcount != 1:
                return {"run": None, "cursor": str(last_id), "wrapped": False}
            run = repository.run_by_id(candidate["id"])
            assert run is not None
            repository.touch_instance(run["instance_id"], now)
            repository.upsert_cursor(worker_id=worker_id, last_run_id=run["id"], instance_id=run["instance_id"], now=now)
            repository.add_change(flow_id=run["flow_id"], instance_id=run["instance_id"], entity_type="step_run", entity_id=run["id"],
                                  action="claim", actor=worker_id, reason="", before={"status": "ready"}, after={"status": "running"}, now=now)
            return {"run": self._run_payload(repository, run), "cursor": str(run["id"]), "wrapped": wrapped}

    def heartbeat(self, run_id: int, worker_id: str, lease_seconds: int) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        expires = to_storage(now_value + timedelta(seconds=lease_seconds))
        with transaction(immediate=True) as connection:
            repository = ProcessRepository(connection)
            run = repository.run_by_id(run_id)
            self._ensure_owned(run, worker_id)
            connection.execute(
                "UPDATE flow_step_runs SET lease_expires_at=?,version=version+1,updated_at=? WHERE id=?",
                (expires, now, run_id),
            )
            return self._run_payload(repository, repository.run_by_id(run_id))

    def complete(self, run_id: int, worker_id: str, result: dict[str, Any], note: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ProcessRepository(connection)
            run = repository.run_by_id(run_id)
            self._ensure_owned(run, worker_id)
            before = {"status": run["status"]}
            connection.execute(
                "UPDATE flow_step_runs SET status='succeeded',lease_owner='',lease_expires_at='',finished_at=?,"
                "completion_note=?,result_json=?,version=version+1,updated_at=? WHERE id=?",
                (now, note[:1000], json.dumps(result, ensure_ascii=False, sort_keys=True), now, run_id),
            )
            repository.add_change(flow_id=run["flow_id"], instance_id=run["instance_id"], entity_type="step_run", entity_id=run_id,
                                  action="complete", actor=worker_id, reason=note, before=before,
                                  after={"status": "succeeded", "result": result}, now=now)
            return self._after_mutation(repository, run_id, now)

    def fail(self, run_id: int, worker_id: str, error_code: str, message: str, retryable: bool) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = ProcessRepository(connection)
            run = repository.run_by_id(run_id)
            self._ensure_owned(run, worker_id)
            attempts = int(run["attempt_count"])
            can_retry = retryable and attempts < int(run["max_attempts"])
            before = {"status": run["status"], "attempt_count": attempts}
            if can_retry:
                delay = min(300, 2 ** max(0, attempts - 1))
                available = to_storage(now_value + timedelta(seconds=delay))
                connection.execute(
                    "UPDATE flow_step_runs SET status='ready',available_at=?,lease_owner='',lease_expires_at='',"
                    "last_error_code=?,last_error_message=?,version=version+1,updated_at=? WHERE id=?",
                    (available, error_code, message[:2000], now, run_id),
                )
                repository.add_change(flow_id=run["flow_id"], instance_id=run["instance_id"], entity_type="step_run", entity_id=run_id,
                                      action="fail_retry", actor=worker_id, reason=message[:2000],
                                      before=before, after={"status": "ready", "available_at": available}, now=now)
                return self._after_mutation(repository, run_id, now)
            connection.execute(
                "UPDATE flow_step_runs SET status='failed',lease_owner='',lease_expires_at='',finished_at=?,"
                "last_error_code=?,last_error_message=?,version=version+1,updated_at=? WHERE id=?",
                (now, error_code, message[:2000], now, run_id),
            )
            repository.add_change(flow_id=run["flow_id"], instance_id=run["instance_id"], entity_type="step_run", entity_id=run_id,
                                  action="fail", actor=worker_id, reason=message[:2000],
                                  before=before, after={"status": "failed", "error_code": error_code}, now=now)
            return self._after_mutation(repository, run_id, now)

    def manual_release(self, run_id: int, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ProcessRepository(connection)
            run = repository.run_by_id(run_id)
            if run is None:
                raise NotFoundError("步骤运行记录不存在")
            if run["status"] != "blocked":
                raise ConflictError("只有被阻塞的步骤可以人工放行")
            before = {"status": "blocked", "blocked_by_run_id": run["blocked_by_run_id"]}
            connection.execute(
                "UPDATE flow_step_runs SET status='ready',blocked_by_run_id=NULL,blocked_reason='',"
                "released_by=?,released_at=?,available_at=?,version=version+1,updated_at=? WHERE id=?",
                (actor, now, now, now, run_id),
            )
            repository.add_release(run_id=run_id, instance_id=run["instance_id"],
                                   blocked_by_run_id=run["blocked_by_run_id"], actor=actor, reason=reason, now=now)
            repository.add_change(flow_id=run["flow_id"], instance_id=run["instance_id"], entity_type="step_run", entity_id=run_id,
                                  action="manual_release", actor=actor, reason=reason, before=before,
                                  after={"status": "ready", "released_by": actor}, now=now)
            return self._after_mutation(repository, run_id, now)

    def recover_expired(self, actor: str = "recovery-worker") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        recovered: list[int] = []
        failed: list[int] = []
        with transaction(immediate=True) as connection:
            repository = ProcessRepository(connection)
            rows = repository.expired_running_runs(now)
            for row in rows:
                run = repository.run_by_id(row["id"])
                if run is None:
                    continue
                if int(row["attempt_count"]) < int(row["max_attempts"]):
                    connection.execute(
                        "UPDATE flow_step_runs SET status='ready',lease_owner='',lease_expires_at='',version=version+1,updated_at=? WHERE id=?",
                        (now, row["id"]),
                    )
                    recovered.append(row["id"])
                else:
                    connection.execute(
                        "UPDATE flow_step_runs SET status='failed',lease_owner='',lease_expires_at='',finished_at=?,"
                        "last_error_code='lease_expired',last_error_message='工作租约已过期',version=version+1,updated_at=? WHERE id=?",
                        (now, now, row["id"]),
                    )
                    failed.append(row["id"])
                repository.add_change(flow_id=run["flow_id"], instance_id=run["instance_id"], entity_type="step_run", entity_id=run["id"],
                                      action="lease_recovery", actor=actor, reason="租约过期自动恢复",
                                      before={"status": "running"}, after={"status": "ready" if row["id"] in recovered else "failed"}, now=now)
            for run_id in failed:
                run = repository.run_by_id(run_id)
                self._sweep(repository, repository.instance_by_id(run["instance_id"]), actor=SYSTEM_ACTOR, now=now)
        return {"recovered": recovered, "failed": failed}

    # ---------- 核心传播规则 ----------

    def _sweep(self, repository: ProcessRepository, instance_row: sqlite3.Row, *, actor: str, now: str, log_transitions: bool = True) -> None:
        """按稳定拓扑顺序重算所有未终结步骤的状态，传播失败为阻塞、成功为就绪。"""
        graph = self._load_graph(repository, instance_row["graph_version_id"])
        runs = {row["step_id"]: row for row in repository.runs_for_instance(instance_row["id"])}
        # 已终结、运行中或被人工放行的步骤不被自动重算覆盖（历史与人工决策不可改写）。
        for step_id in graph["order"]:
            row = runs[step_id]
            status = row["status"]
            if status in {"running", "succeeded", "failed"} or row["released_at"]:
                continue
            pred_statuses = [runs[pred_id]["status"] for pred_id in graph["predecessors"][step_id]]
            pred_runs = [runs[pred_id] for pred_id in graph["predecessors"][step_id]]
            new_status = status
            blocked_origin = None
            def _is_blocking_source(pred: sqlite3.Row) -> bool:
                if pred["status"] not in {"failed", "blocked"}:
                    return False
                # 放行后再次失败（finished_at 晚于放行时间）仍需向下游传播。
                if pred["released_at"] and pred["status"] == "failed" and pred["finished_at"] and pred["finished_at"] > pred["released_at"]:
                    return True
                return pred["released_at"] is None or pred["released_at"] == ""

            if any(_is_blocking_source(item) for item in pred_runs):
                new_status = "blocked"
                failed_pred = next((item for item in pred_runs if item["status"] == "failed" and _is_blocking_source(item)), None)
                if failed_pred is not None:
                    blocked_origin = failed_pred["id"]
                else:
                    blocked_pred = next(item for item in pred_runs if item["status"] == "blocked" and _is_blocking_source(item))
                    blocked_origin = blocked_pred["blocked_by_run_id"] or blocked_pred["id"]
            elif status == "ready":
                # 已就绪（含退避重试）保持就绪，可领取时间由 available_at 控制
                new_status = "ready"
            elif not pred_statuses or all(item == "succeeded" for item in pred_statuses):
                new_status = "ready"
            else:
                new_status = "waiting"
            if new_status == status:
                continue
            reason = ""
            if new_status == "blocked":
                origin = repository.run_by_id(blocked_origin) if blocked_origin else None
                if origin is not None:
                    detail = origin["last_error_message"] or origin["last_error_code"] or "未满足前置条件"
                    reason = f"前置步骤 {origin['step_code']} 失败：{detail}"
                else:
                    reason = "前置步骤未满足"
            connection = repository.connection
            connection.execute(
                "UPDATE flow_step_runs SET status=?,blocked_by_run_id=?,blocked_reason=?,version=version+1,updated_at=? WHERE id=?",
                (new_status, blocked_origin if new_status == "blocked" else None, reason, now, row["id"]),
            )
            runs[step_id] = repository.run_by_id(row["id"])
            if log_transitions and new_status == "blocked":
                repository.add_change(flow_id=instance_row["flow_id"], instance_id=instance_row["id"],
                                      entity_type="step_run", entity_id=row["id"], action="blocked", actor=actor,
                                      reason=reason, before={"status": status}, after={"status": "blocked", "blocked_by_run_id": blocked_origin}, now=now)
        all_runs = repository.runs_for_instance(instance_row["id"])
        if all_runs and all(item["status"] == "succeeded" for item in all_runs):
            repository.connection.execute(
                "UPDATE ceremony_instances SET status='completed',updated_at=? WHERE id=? AND status='active'",
                (now, instance_row["id"]),
            )

    def _after_mutation(self, repository: ProcessRepository, run_id: int, now: str) -> dict[str, Any]:
        run = repository.run_by_id(run_id)
        assert run is not None
        instance = repository.instance_by_id(run["instance_id"])
        self._sweep(repository, instance, actor=SYSTEM_ACTOR, now=now)
        return self._instance_detail(repository, repository.instance_by_id(run["instance_id"]))

    # ---------- 组装与工具 ----------

    def _load_graph(self, repository: ProcessRepository, graph_version_id: int) -> dict[str, Any]:
        step_rows = repository.steps_for_version(graph_version_id)
        edge_rows = repository.edges_for_version(graph_version_id)
        steps = {row["id"]: row for row in step_rows}
        predecessors: dict[int, set[int]] = {row["id"]: set() for row in step_rows}
        successors: dict[int, set[int]] = {row["id"]: set() for row in step_rows}
        for edge in edge_rows:
            predecessors[edge["to_step_id"]].add(edge["from_step_id"])
            successors[edge["from_step_id"]].add(edge["to_step_id"])
        order = [row["id"] for row in step_rows]  # ordinal 已由稳定拓扑顺序写入
        return {"steps": steps, "predecessors": predecessors, "successors": successors, "order": order}

    @staticmethod
    def _ensure_owned(run: sqlite3.Row | None, worker_id: str) -> None:
        if run is None:
            raise NotFoundError("步骤运行记录不存在")
        if run["status"] != "running" or run["lease_owner"] != worker_id:
            raise ConflictError("步骤未由当前工作者持有")

    @staticmethod
    def _decode_cursor(token: str) -> int:
        try:
            value = int(token)
        except (TypeError, ValueError):
            raise ValidationError("游标格式不正确")
        if value < 0:
            raise ValidationError("游标格式不正确")
        return value

    def _flow_detail(self, repository: ProcessRepository, flow_id: int) -> dict[str, Any]:
        flow = repository.connection.execute("SELECT * FROM ceremony_flows WHERE id=?", (flow_id,)).fetchone()
        latest = repository.latest_version(flow_id)
        result = {"id": flow["id"], "code": flow["code"], "name": flow["name"], "current_version": flow["current_version"],
                  "created_at": flow["created_at"], "updated_at": flow["updated_at"], "current_graph": None}
        if latest is not None:
            result["current_graph"] = self._version_detail(repository, flow, latest)
        return result

    def _version_detail(self, repository: ProcessRepository, flow: sqlite3.Row, version: sqlite3.Row) -> dict[str, Any]:
        step_rows = repository.steps_for_version(version["id"])
        edge_rows = repository.edges_for_version(version["id"])
        return {
            "flow_code": flow["code"],
            "version": version["version"],
            "digest": version["digest"],
            "note": version["note"],
            "created_by": version["created_by"],
            "created_at": version["created_at"],
            "step_count": version["step_count"],
            "edge_count": version["edge_count"],
            "topological_order": [row["code"] for row in step_rows],
            "steps": [{"code": row["code"], "name": row["name"], "kind": row["kind"], "required_role": row["required_role"],
                       "priority": row["priority"], "ordinal": row["ordinal"], "max_attempts": row["max_attempts"]} for row in step_rows],
            "edges": [{"from": row["from_code"], "to": row["to_code"]} for row in edge_rows],
        }

    def _instance_detail(self, repository: ProcessRepository, instance: sqlite3.Row) -> dict[str, Any]:
        graph = self._load_graph(repository, instance["graph_version_id"])
        runs = {row["step_id"]: row for row in repository.runs_for_instance(instance["id"])}
        releases = repository.releases_for_instance(instance["id"])
        release_by_run: dict[int, list[dict[str, Any]]] = {}
        for release in releases:
            release_by_run.setdefault(release["run_id"], []).append(self._release(release))
        steps: list[dict[str, Any]] = []
        for step_id in graph["order"]:
            row = runs[step_id]
            step = graph["steps"][step_id]
            blockers: list[dict[str, Any]] = []
            for pred_id in graph["predecessors"][step_id]:
                pred = runs[pred_id]
                if pred["status"] in {"failed", "blocked"}:
                    blockers.append({"step_code": graph["steps"][pred_id]["code"], "run_id": pred["id"], "status": pred["status"]})
            steps.append({
                "run_id": row["id"],
                "step_code": step["code"],
                "step_name": step["name"],
                "kind": step["kind"],
                "required_role": step["required_role"],
                "priority": step["priority"],
                "ordinal": step["ordinal"],
                "status": row["status"],
                "attempt_count": row["attempt_count"],
                "max_attempts": row["max_attempts"],
                "available_at": row["available_at"] or None,
                "lease_owner": row["lease_owner"] or None,
                "lease_expires_at": row["lease_expires_at"] or None,
                "last_error_code": row["last_error_code"] or None,
                "last_error_message": row["last_error_message"] or None,
                "blocked_by_run_id": row["blocked_by_run_id"],
                "blocked_reason": row["blocked_reason"] or None,
                "blockers": blockers,
                "released_by": row["released_by"] or None,
                "released_at": row["released_at"],
                "releases": release_by_run.get(row["id"], []),
                "updated_at": row["updated_at"],
            })
        return {
            "instance_id": instance["id"],
            "business_key": instance["business_key"],
            "title": instance["title"],
            "status": instance["status"],
            "flow_code": instance["flow_code"],
            "graph_version": instance["graph_version"],
            "created_by": instance["created_by"],
            "created_at": instance["created_at"],
            "updated_at": instance["updated_at"],
            "steps": steps,
        }

    def _run_payload(self, repository: ProcessRepository, run: sqlite3.Row) -> dict[str, Any]:
        instance = repository.instance_by_id(run["instance_id"])
        return {
            "run_id": run["id"],
            "instance_id": run["instance_id"],
            "business_key": run["business_key"],
            "flow_code": instance["flow_code"],
            "graph_version": instance["graph_version"],
            "step_code": run["step_code"],
            "step_name": run["step_name"],
            "kind": run["step_kind"],
            "required_role": run["required_role"],
            "priority": run["priority"],
            "ordinal": run["ordinal"],
            "status": run["status"],
            "attempt_count": run["attempt_count"],
            "lease_owner": run["lease_owner"],
            "lease_expires_at": run["lease_expires_at"] or None,
        }

    @staticmethod
    def _release(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "blocked_by_run_id": row["blocked_by_run_id"], "actor": row["actor"],
                "reason": row["reason"], "created_at": row["created_at"]}

    @staticmethod
    def _change(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "flow_id": row["flow_id"],
            "instance_id": row["instance_id"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "action": row["action"],
            "actor": row["actor"],
            "reason": row["reason"],
            "before": json.loads(row["before_json"] or "{}"),
            "after": json.loads(row["after_json"] or "{}"),
            "created_at": row["created_at"],
        }

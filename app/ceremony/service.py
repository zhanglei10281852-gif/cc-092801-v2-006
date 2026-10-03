from __future__ import annotations

import hashlib
import heapq
import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS ceremony_flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed')),
    current_version INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ceremony_graph_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ceremony_id INTEGER NOT NULL REFERENCES ceremony_flows(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    change_note TEXT NOT NULL DEFAULT '',
    graph_digest TEXT NOT NULL,
    topo_order_json TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(ceremony_id, version)
);
CREATE TABLE IF NOT EXISTS ceremony_graph_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES ceremony_graph_versions(id) ON DELETE CASCADE,
    step_key TEXT NOT NULL,
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    UNIQUE(version_id, step_key)
);
CREATE TABLE IF NOT EXISTS ceremony_graph_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES ceremony_graph_versions(id) ON DELETE CASCADE,
    predecessor TEXT NOT NULL,
    successor TEXT NOT NULL,
    on_failure TEXT NOT NULL DEFAULT 'block' CHECK(on_failure IN ('block','ignore')),
    UNIQUE(version_id, predecessor, successor)
);
CREATE TABLE IF NOT EXISTS ceremony_step_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ceremony_id INTEGER NOT NULL REFERENCES ceremony_flows(id) ON DELETE CASCADE,
    step_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','ready','running','succeeded','failed','blocked')),
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    blocked_reason TEXT NOT NULL DEFAULT '',
    released_by TEXT NOT NULL DEFAULT '',
    released_reason TEXT NOT NULL DEFAULT '',
    released_at TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(ceremony_id, step_key)
);
CREATE INDEX IF NOT EXISTS idx_ceremony_states ON ceremony_step_states(ceremony_id, status);
CREATE TABLE IF NOT EXISTS ceremony_flow_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ceremony_id INTEGER NOT NULL REFERENCES ceremony_flows(id) ON DELETE CASCADE,
    step_key TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ceremony_events ON ceremony_flow_events(ceremony_id, id);
"""

# 终态与执行中的步骤不参与派生状态重算，保证已完成的历史不会被改写。
LOCKED_STATES = {"running", "succeeded", "failed"}


def ensure_schema() -> None:
    connection = get_connection()
    connection.executescript(SCHEMA)


def _digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


class CeremonyFlowService:
    """仪式流程依赖图：版本化步骤关系、领取调度、失败传播与人工放行。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        ensure_schema()

    # ------------------------------------------------------------------
    # 仪式与流程图版本
    # ------------------------------------------------------------------
    def create_ceremony(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            if connection.execute("SELECT id FROM ceremony_flows WHERE code=?", (payload["code"],)).fetchone():
                raise ConflictError("仪式编码已存在")
            cursor = connection.execute(
                "INSERT INTO ceremony_flows(code,name,created_by,created_at,updated_at) VALUES(?,?,?,?,?)",
                (payload["code"], payload["name"], payload["actor"], now, now),
            )
            ceremony_id = cursor.lastrowid
            self._emit(connection, ceremony_id, "ceremony_created", payload["actor"], now, detail={"code": payload["code"], "name": payload["name"]})
            return self.get_ceremony(payload["code"])

    def list_ceremonies(self, *, blocked_only: bool = False) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM ceremony_flows ORDER BY id").fetchall()
        items = []
        for row in rows:
            summary = self._summary(self.connection, row)
            if blocked_only and summary["counts"].get("blocked", 0) == 0:
                continue
            items.append(summary)
        return items

    def get_ceremony(self, code: str) -> dict[str, Any]:
        ceremony = self._ceremony_row(self.connection, code)
        result = self._summary(self.connection, ceremony)
        version_row, steps, edges = self._current_graph(self.connection, ceremony)
        states = self._states(self.connection, ceremony["id"])
        names = {step["step_key"]: step["name"] for step in steps}
        incoming = self._incoming(edges)
        result["topo_order"] = json.loads(version_row["topo_order_json"]) if version_row else []
        result["steps"] = [self._step_view(step, states.get(step["step_key"]), names, incoming.get(step["step_key"], []), states) for step in steps]
        return result

    def submit_graph(self, code: str, payload: dict[str, Any]) -> dict[str, Any]:
        steps, edges = self._validate_graph(payload["steps"], payload["edges"])
        topo_order = self._topo_sort([step["key"] for step in steps], edges)
        graph_digest = _digest({"steps": sorted(steps, key=lambda item: item["key"]), "edges": sorted(edges, key=lambda item: (item["predecessor"], item["successor"]))})
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            ceremony = self._ceremony_row(connection, code)
            current_version = int(ceremony["current_version"])
            if current_version > 0:
                current = self._version_row(connection, ceremony["id"], current_version)
                if current["graph_digest"] == graph_digest:
                    return self._version_view(connection, ceremony, current, deduplicated=True)
            version = current_version + 1
            cursor = connection.execute(
                "INSERT INTO ceremony_graph_versions(ceremony_id,version,change_note,graph_digest,topo_order_json,submitted_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (ceremony["id"], version, payload.get("change_note", ""), graph_digest, json.dumps(topo_order, ensure_ascii=False), payload["actor"], now),
            )
            version_id = cursor.lastrowid
            position = {key: index for index, key in enumerate(topo_order)}
            for step in steps:
                connection.execute(
                    "INSERT INTO ceremony_graph_steps(version_id,step_key,name,position) VALUES(?,?,?,?)",
                    (version_id, step["key"], step["name"], position[step["key"]]),
                )
            for edge in edges:
                connection.execute(
                    "INSERT INTO ceremony_graph_edges(version_id,predecessor,successor,on_failure) VALUES(?,?,?,?)",
                    (version_id, edge["predecessor"], edge["successor"], edge["on_failure"]),
                )
            connection.execute(
                "UPDATE ceremony_flows SET current_version=?,updated_at=? WHERE id=?",
                (version, now, ceremony["id"]),
            )
            self._emit(
                connection, ceremony["id"], "graph_submitted", payload["actor"], now,
                reason=payload.get("change_note", ""),
                detail={"version": version, "topo_order": topo_order, "steps": steps, "edges": edges},
            )
            updated = self._ceremony_row(connection, code)
            self._recompute(connection, updated, payload["actor"], now)
            return self._version_view(connection, updated, self._version_row(connection, ceremony["id"], version), deduplicated=False)

    def list_versions(self, code: str) -> list[dict[str, Any]]:
        ceremony = self._ceremony_row(self.connection, code)
        rows = self.connection.execute(
            "SELECT * FROM ceremony_graph_versions WHERE ceremony_id=? ORDER BY version", (ceremony["id"],)
        ).fetchall()
        return [self._version_summary(row) for row in rows]

    def get_version(self, code: str, version: int) -> dict[str, Any]:
        ceremony = self._ceremony_row(self.connection, code)
        row = self._version_row(self.connection, ceremony["id"], version)
        if row is None:
            raise NotFoundError("流程图版本不存在")
        return self._version_view(self.connection, ceremony, row, deduplicated=False)

    def list_events(self, code: str, *, cursor: int = 0, limit: int = 50, step_key: str | None = None) -> dict[str, Any]:
        ceremony = self._ceremony_row(self.connection, code)
        limit = max(1, min(limit, 200))
        clause = "ceremony_id=? AND id>?"
        params: list[Any] = [ceremony["id"], cursor]
        if step_key:
            clause += " AND step_key=?"
            params.append(step_key)
        params.append(limit + 1)
        rows = self.connection.execute(
            f"SELECT * FROM ceremony_flow_events WHERE {clause} ORDER BY id LIMIT ?", params
        ).fetchall()
        items = [self._event_view(row) for row in rows[:limit]]
        return {
            "items": items,
            "next_cursor": items[-1]["id"] if items else cursor,
            "has_more": len(rows) > limit,
        }

    # ------------------------------------------------------------------
    # 领取、回执与人工放行
    # ------------------------------------------------------------------
    def claim(self, code: str, worker_id: str, step_keys: list[str], lease_seconds: int) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        lease_until = to_storage(now_value + timedelta(seconds=lease_seconds))
        with transaction(immediate=True) as connection:
            ceremony = self._ceremony_row(connection, code)
            version_row, steps, edges = self._current_graph(connection, ceremony)
            if version_row is None:
                raise ConflictError("仪式尚未提交步骤关系")
            allowed = set(step_keys)
            states = self._states(connection, ceremony["id"])
            claimed_key: str | None = None
            for step in steps:
                if allowed and step["step_key"] not in allowed:
                    continue
                state = states.get(step["step_key"])
                if state is None or state["status"] != "ready":
                    continue
                cursor = connection.execute(
                    "UPDATE ceremony_step_states SET status='running',lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,started_at=COALESCE(started_at,?),updated_at=? WHERE ceremony_id=? AND step_key=? AND status='ready'",
                    (worker_id, lease_until, now, now, ceremony["id"], step["step_key"]),
                )
                if cursor.rowcount == 1:
                    claimed_key = step["step_key"]
                    break
            states = self._states(connection, ceremony["id"])
            names = {step["step_key"]: step["name"] for step in steps}
            incoming = self._incoming(edges)
            skipped = [
                {"step_key": step["step_key"], "name": step["name"], "status": states[step["step_key"]]["status"], "blocked_reason": states[step["step_key"]]["blocked_reason"]}
                for step in steps
                if states[step["step_key"]]["status"] in ("pending", "blocked")
            ]
            claimed_view = None
            if claimed_key is not None:
                self._emit(connection, ceremony["id"], "step_claimed", worker_id, now, step_key=claimed_key, detail={"lease_expires_at": lease_until})
                step_row = next(step for step in steps if step["step_key"] == claimed_key)
                claimed_view = self._step_view(step_row, states[claimed_key], names, incoming.get(claimed_key, []), states)
            return {"step": claimed_view, "skipped": skipped}

    def complete(self, code: str, step_key: str, worker_id: str, result: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            ceremony = self._ceremony_row(connection, code)
            state = self._owned_state(connection, ceremony, step_key, worker_id)
            connection.execute(
                "UPDATE ceremony_step_states SET status='succeeded',result_json=?,lease_owner='',lease_expires_at='',finished_at=?,updated_at=? WHERE ceremony_id=? AND step_key=?",
                (json.dumps(result, ensure_ascii=False, sort_keys=True), now, now, ceremony["id"], step_key),
            )
            self._emit(connection, ceremony["id"], "step_completed", worker_id, now, step_key=step_key, detail={"attempt": state["attempt_count"]})
            self._recompute(connection, ceremony, worker_id, now)
            return self._step_response(connection, ceremony, step_key)

    def fail(self, code: str, step_key: str, worker_id: str, error_code: str, message: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            ceremony = self._ceremony_row(connection, code)
            self._owned_state(connection, ceremony, step_key, worker_id)
            connection.execute(
                "UPDATE ceremony_step_states SET status='failed',lease_owner='',lease_expires_at='',last_error_code=?,last_error_message=?,finished_at=?,updated_at=? WHERE ceremony_id=? AND step_key=?",
                (error_code, message[:2000], now, now, ceremony["id"], step_key),
            )
            self._emit(connection, ceremony["id"], "step_failed", worker_id, now, step_key=step_key, reason=message[:2000], detail={"error_code": error_code})
            self._recompute(connection, ceremony, worker_id, now)
            return self._step_response(connection, ceremony, step_key)

    def release(self, code: str, step_key: str, actor: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            ceremony = self._ceremony_row(connection, code)
            version_row, steps, _edges = self._current_graph(connection, ceremony)
            if version_row is None or step_key not in {step["step_key"] for step in steps}:
                raise NotFoundError("步骤不存在")
            states = self._states(connection, ceremony["id"])
            state = states.get(step_key)
            if state is None or state["status"] != "blocked":
                raise ConflictError("只有处于阻塞状态的步骤可以人工放行")
            previous_reason = state["blocked_reason"]
            connection.execute(
                "UPDATE ceremony_step_states SET released_by=?,released_reason=?,released_at=?,updated_at=? WHERE ceremony_id=? AND step_key=?",
                (actor, reason, now, now, ceremony["id"], step_key),
            )
            self._recompute(connection, ceremony, actor, now, suppress={step_key})
            states = self._states(connection, ceremony["id"])
            self._emit(
                connection, ceremony["id"], "step_released", actor, now,
                step_key=step_key, reason=reason,
                detail={"previous_blocked_reason": previous_reason, "status": states[step_key]["status"]},
            )
            return self._step_response(connection, ceremony, step_key)

    # ------------------------------------------------------------------
    # 派生状态重算：失败沿 block 边传播为阻塞，放行标记可覆盖
    # ------------------------------------------------------------------
    def _recompute(self, connection: sqlite3.Connection, ceremony: sqlite3.Row, actor: str, now: str, suppress: set[str] | None = None) -> None:
        version_row, steps, edges = self._current_graph(connection, ceremony)
        if version_row is None:
            return
        suppress = suppress or set()
        states = self._states(connection, ceremony["id"])
        names = {step["step_key"]: step["name"] for step in steps}
        incoming = self._incoming(edges)
        for step in steps:
            key = step["step_key"]
            current = states.get(key)
            if current is not None and current["status"] in LOCKED_STATES:
                continue
            desired, reason = self._evaluate(key, incoming.get(key, []), states, names)
            if current is None:
                connection.execute(
                    "INSERT INTO ceremony_step_states(ceremony_id,step_key,status,blocked_reason,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (ceremony["id"], key, desired, reason, now, now),
                )
                states[key] = {"status": desired, "blocked_reason": reason, "released_by": ""}
                continue
            if current["status"] == desired and current["blocked_reason"] == reason:
                continue
            connection.execute(
                "UPDATE ceremony_step_states SET status=?,blocked_reason=?,updated_at=? WHERE ceremony_id=? AND step_key=?",
                (desired, reason, now, ceremony["id"], key),
            )
            states[key] = {**current, "status": desired, "blocked_reason": reason}
            if key not in suppress:
                action = {"blocked": "step_blocked", "ready": "step_ready", "pending": "step_pending"}[desired]
                self._emit(connection, ceremony["id"], action, actor, now, step_key=key, reason=reason)
        if all(states[step["step_key"]]["status"] == "succeeded" for step in steps):
            cursor = connection.execute(
                "UPDATE ceremony_flows SET status='completed',updated_at=? WHERE id=? AND status='active'",
                (now, ceremony["id"]),
            )
            if cursor.rowcount == 1:
                self._emit(connection, ceremony["id"], "ceremony_completed", actor, now, detail={"version": version_row["version"]})

    @staticmethod
    def _evaluate(step_key: str, incoming: list[sqlite3.Row], states: dict[str, Any], names: dict[str, str]) -> tuple[str, str]:
        current = states.get(step_key) or {}
        released = bool(current.get("released_by"))
        blocking: list[str] = []
        waiting = False
        for edge in incoming:
            predecessor = edge["predecessor"]
            predecessor_status = (states.get(predecessor) or {}).get("status", "pending")
            if predecessor_status == "succeeded":
                continue
            if edge["on_failure"] == "ignore":
                if predecessor_status == "failed":
                    continue  # 规则：忽略前置失败
                waiting = True
                continue
            if predecessor_status in ("failed", "blocked"):
                if released:
                    continue  # 人工放行覆盖失败或阻塞的前置
                label = "已失败" if predecessor_status == "failed" else "被阻塞"
                blocking.append(f"前置步骤 {predecessor}（{names.get(predecessor, predecessor)}）{label}")
            else:
                waiting = True
        if blocking:
            return "blocked", "；".join(blocking)
        if waiting:
            return "pending", ""
        return "ready", ""

    # ------------------------------------------------------------------
    # 图校验与稳定拓扑排序
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_graph(steps: list[dict[str, Any]], edges: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        keys = [step["key"] for step in steps]
        duplicated = sorted({key for key in keys if keys.count(key) > 1})
        if duplicated:
            raise ValidationError("步骤编码重复", context={"steps": duplicated})
        known = set(keys)
        unknown = sorted(({edge["predecessor"] for edge in edges} | {edge["successor"] for edge in edges}) - known)
        if unknown:
            raise ValidationError("步骤关系引用了不存在的步骤", context={"steps": unknown})
        seen: set[tuple[str, str]] = set()
        normalized: list[dict[str, Any]] = []
        for edge in edges:
            pair = (edge["predecessor"], edge["successor"])
            if pair in seen:
                raise ValidationError("步骤关系重复", context={"predecessor": pair[0], "successor": pair[1]})
            seen.add(pair)
            normalized.append({"predecessor": pair[0], "successor": pair[1], "on_failure": edge["on_failure"]})
        return [{"key": step["key"], "name": step["name"]} for step in steps], normalized

    @staticmethod
    def _topo_sort(keys: list[str], edges: list[dict[str, Any]]) -> list[str]:
        indegree = {key: 0 for key in keys}
        outgoing: dict[str, list[str]] = {key: [] for key in keys}
        for edge in edges:
            indegree[edge["successor"]] += 1
            outgoing[edge["predecessor"]].append(edge["successor"])
        heap = [key for key, degree in indegree.items() if degree == 0]
        heapq.heapify(heap)
        order: list[str] = []
        while heap:
            node = heapq.heappop(heap)
            order.append(node)
            for successor in outgoing[node]:
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    heapq.heappush(heap, successor)
        if len(order) != len(keys):
            remaining = sorted(key for key, degree in indegree.items() if degree > 0)
            raise ValidationError("步骤关系存在循环", context={"steps": remaining})
        return order

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _ceremony_row(connection: sqlite3.Connection, code: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM ceremony_flows WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("仪式不存在")
        return row

    @staticmethod
    def _version_row(connection: sqlite3.Connection, ceremony_id: int, version: int) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM ceremony_graph_versions WHERE ceremony_id=? AND version=?", (ceremony_id, version)
        ).fetchone()

    def _current_graph(self, connection: sqlite3.Connection, ceremony: sqlite3.Row) -> tuple[sqlite3.Row | None, list[sqlite3.Row], list[sqlite3.Row]]:
        version = int(ceremony["current_version"])
        if version == 0:
            return None, [], []
        version_row = self._version_row(connection, ceremony["id"], version)
        if version_row is None:
            return None, [], []
        steps = connection.execute("SELECT * FROM ceremony_graph_steps WHERE version_id=? ORDER BY position", (version_row["id"],)).fetchall()
        edges = connection.execute("SELECT * FROM ceremony_graph_edges WHERE version_id=? ORDER BY predecessor,successor", (version_row["id"],)).fetchall()
        return version_row, steps, edges

    @staticmethod
    def _states(connection: sqlite3.Connection, ceremony_id: int) -> dict[str, dict[str, Any]]:
        rows = connection.execute("SELECT * FROM ceremony_step_states WHERE ceremony_id=?", (ceremony_id,)).fetchall()
        return {row["step_key"]: dict(row) for row in rows}

    @staticmethod
    def _incoming(edges: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
        incoming: dict[str, list[sqlite3.Row]] = {}
        for edge in edges:
            incoming.setdefault(edge["successor"], []).append(edge)
        return incoming

    def _owned_state(self, connection: sqlite3.Connection, ceremony: sqlite3.Row, step_key: str, worker_id: str) -> dict[str, Any]:
        version_row, steps, _edges = self._current_graph(connection, ceremony)
        if version_row is None or step_key not in {step["step_key"] for step in steps}:
            raise NotFoundError("步骤不存在")
        state = self._states(connection, ceremony["id"]).get(step_key)
        if state is None:
            raise NotFoundError("步骤不存在")
        if state["status"] != "running" or state["lease_owner"] != worker_id:
            raise ConflictError("步骤未由当前执行人持有")
        return state

    def _step_response(self, connection: sqlite3.Connection, ceremony: sqlite3.Row, step_key: str) -> dict[str, Any]:
        _version_row, steps, edges = self._current_graph(connection, ceremony)
        states = self._states(connection, ceremony["id"])
        names = {step["step_key"]: step["name"] for step in steps}
        incoming = self._incoming(edges)
        step_row = next(step for step in steps if step["step_key"] == step_key)
        return self._step_view(step_row, states.get(step_key), names, incoming.get(step_key, []), states)

    def _step_view(self, step: sqlite3.Row, state: dict[str, Any] | None, names: dict[str, str], incoming: list[sqlite3.Row], states: dict[str, Any]) -> dict[str, Any]:
        state = state or {}
        released = bool(state.get("released_by"))
        prerequisites = []
        for edge in incoming:
            predecessor = edge["predecessor"]
            predecessor_status = (states.get(predecessor) or {}).get("status", "pending")
            satisfied = predecessor_status == "succeeded" or (edge["on_failure"] == "ignore" and predecessor_status == "failed") or (released and predecessor_status in ("failed", "blocked"))
            prerequisites.append({
                "step_key": predecessor,
                "name": names.get(predecessor, predecessor),
                "on_failure": edge["on_failure"],
                "status": predecessor_status,
                "satisfied": satisfied,
            })
        return {
            "step_key": step["step_key"],
            "name": step["name"],
            "position": step["position"],
            "status": state.get("status", "pending"),
            "blocked_reason": state.get("blocked_reason", ""),
            "lease_owner": state.get("lease_owner", ""),
            "lease_expires_at": state.get("lease_expires_at", ""),
            "attempt_count": state.get("attempt_count", 0),
            "released": released,
            "released_by": state.get("released_by", ""),
            "released_reason": state.get("released_reason", ""),
            "released_at": state.get("released_at", ""),
            "last_error_code": state.get("last_error_code", ""),
            "last_error_message": state.get("last_error_message", ""),
            "result": json.loads(state["result_json"]) if state.get("result_json") else {},
            "started_at": state.get("started_at"),
            "finished_at": state.get("finished_at"),
            "prerequisites": prerequisites,
        }

    def _summary(self, connection: sqlite3.Connection, ceremony: sqlite3.Row) -> dict[str, Any]:
        version_row, steps, _edges = self._current_graph(connection, ceremony)
        states = self._states(connection, ceremony["id"])
        counts: dict[str, int] = {}
        for step in steps:
            status = (states.get(step["step_key"]) or {}).get("status", "pending")
            counts[status] = counts.get(status, 0) + 1
        return {
            "code": ceremony["code"],
            "name": ceremony["name"],
            "status": ceremony["status"],
            "current_version": ceremony["current_version"],
            "counts": counts,
            "created_by": ceremony["created_by"],
            "created_at": ceremony["created_at"],
            "updated_at": ceremony["updated_at"],
        }

    def _version_summary(self, row: sqlite3.Row) -> dict[str, Any]:
        steps = self.connection.execute("SELECT COUNT(*) FROM ceremony_graph_steps WHERE version_id=?", (row["id"],)).fetchone()[0]
        edges = self.connection.execute("SELECT COUNT(*) FROM ceremony_graph_edges WHERE version_id=?", (row["id"],)).fetchone()[0]
        return {
            "version": row["version"],
            "change_note": row["change_note"],
            "graph_digest": row["graph_digest"],
            "topo_order": json.loads(row["topo_order_json"]),
            "step_count": steps,
            "edge_count": edges,
            "submitted_by": row["submitted_by"],
            "created_at": row["created_at"],
        }

    def _version_view(self, connection: sqlite3.Connection, ceremony: sqlite3.Row, version_row: sqlite3.Row, *, deduplicated: bool) -> dict[str, Any]:
        steps = connection.execute("SELECT * FROM ceremony_graph_steps WHERE version_id=? ORDER BY position", (version_row["id"],)).fetchall()
        edges = connection.execute("SELECT * FROM ceremony_graph_edges WHERE version_id=? ORDER BY predecessor,successor", (version_row["id"],)).fetchall()
        states = self._states(connection, ceremony["id"])
        return {
            "ceremony_code": ceremony["code"],
            "version": version_row["version"],
            "deduplicated": deduplicated,
            "change_note": version_row["change_note"],
            "graph_digest": version_row["graph_digest"],
            "topo_order": json.loads(version_row["topo_order_json"]),
            "steps": [
                {"step_key": step["step_key"], "name": step["name"], "position": step["position"], "status": (states.get(step["step_key"]) or {}).get("status", "pending")}
                for step in steps
            ],
            "edges": [{"predecessor": edge["predecessor"], "successor": edge["successor"], "on_failure": edge["on_failure"]} for edge in edges],
            "submitted_by": version_row["submitted_by"],
            "created_at": version_row["created_at"],
        }

    @staticmethod
    def _event_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "step_key": row["step_key"],
            "action": row["action"],
            "actor": row["actor"],
            "reason": row["reason"],
            "detail": json.loads(row["detail_json"]),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _emit(connection: sqlite3.Connection, ceremony_id: int, action: str, actor: str, now: str, *, step_key: str = "", reason: str = "", detail: dict[str, Any] | None = None) -> None:
        connection.execute(
            "INSERT INTO ceremony_flow_events(ceremony_id,step_key,action,actor,reason,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (ceremony_id, step_key, action, actor, reason, json.dumps(detail or {}, ensure_ascii=False, sort_keys=True), now),
        )

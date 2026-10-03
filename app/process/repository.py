from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS ceremony_flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS flow_graph_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id INTEGER NOT NULL REFERENCES ceremony_flows(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    digest TEXT NOT NULL,
    step_count INTEGER NOT NULL,
    edge_count INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(flow_id, version)
);
CREATE TABLE IF NOT EXISTS flow_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    graph_version_id INTEGER NOT NULL REFERENCES flow_graph_versions(id) ON DELETE RESTRICT,
    flow_id INTEGER NOT NULL REFERENCES ceremony_flows(id) ON DELETE RESTRICT,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    required_role TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    UNIQUE(graph_version_id, code)
);
CREATE TABLE IF NOT EXISTS flow_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    graph_version_id INTEGER NOT NULL REFERENCES flow_graph_versions(id) ON DELETE RESTRICT,
    from_step_id INTEGER NOT NULL REFERENCES flow_steps(id) ON DELETE RESTRICT,
    to_step_id INTEGER NOT NULL REFERENCES flow_steps(id) ON DELETE RESTRICT,
    UNIQUE(graph_version_id, from_step_id, to_step_id)
);
CREATE INDEX IF NOT EXISTS idx_flow_edges_version ON flow_edges(graph_version_id);
CREATE TABLE IF NOT EXISTS ceremony_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id INTEGER NOT NULL REFERENCES ceremony_flows(id) ON DELETE RESTRICT,
    graph_version_id INTEGER NOT NULL REFERENCES flow_graph_versions(id) ON DELETE RESTRICT,
    business_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','cancelled')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_instances_flow ON ceremony_instances(flow_id,id);
CREATE TABLE IF NOT EXISTS flow_step_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES ceremony_instances(id) ON DELETE CASCADE,
    step_id INTEGER NOT NULL REFERENCES flow_steps(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'waiting'
        CHECK(status IN ('waiting','ready','running','succeeded','failed','blocked')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    available_at TEXT NOT NULL DEFAULT '',
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    finished_at TEXT,
    completion_note TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    blocked_by_run_id INTEGER,
    blocked_reason TEXT NOT NULL DEFAULT '',
    released_by TEXT NOT NULL DEFAULT '',
    released_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    UNIQUE(instance_id, step_id)
);
CREATE INDEX IF NOT EXISTS idx_step_runs_status ON flow_step_runs(status, available_at, id);
CREATE TABLE IF NOT EXISTS flow_manual_releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES flow_step_runs(id) ON DELETE CASCADE,
    instance_id INTEGER NOT NULL REFERENCES ceremony_instances(id) ON DELETE CASCADE,
    blocked_by_run_id INTEGER,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_manual_releases_run ON flow_manual_releases(run_id,id);
CREATE TABLE IF NOT EXISTS flow_change_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_id INTEGER,
    instance_id INTEGER,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL DEFAULT '',
    after_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_change_log_flow ON flow_change_log(flow_id,id);
CREATE INDEX IF NOT EXISTS idx_change_log_instance ON flow_change_log(instance_id,id);
CREATE TABLE IF NOT EXISTS flow_claim_cursors (
    worker_id TEXT NOT NULL PRIMARY KEY,
    last_run_id INTEGER NOT NULL DEFAULT 0,
    instance_id INTEGER,
    updated_at TEXT NOT NULL
);
"""


def ensure_schema() -> None:
    from app.database import get_connection

    get_connection().executescript(SCHEMA)


class ProcessRepository:
    """流程依赖图领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---- 流程与版本 ----

    def flow_by_code(self, code: str) -> sqlite3.Row | None:
        # 所有写操作都在 BEGIN IMMEDIATE 事务中进行，写锁已在事务开始时获取。
        return self.connection.execute("SELECT * FROM ceremony_flows WHERE code=?", (code,)).fetchone()

    def create_flow(self, *, code: str, name: str, now: str) -> sqlite3.Row:
        cursor = self.connection.execute(
            "INSERT INTO ceremony_flows(code,name,current_version,created_at,updated_at) VALUES(?,?,'0',?,?)",
            (code, name, now, now),
        )
        row = self.connection.execute("SELECT * FROM ceremony_flows WHERE id=?", (cursor.lastrowid,)).fetchone()
        assert row is not None
        return row

    def latest_version(self, flow_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM flow_graph_versions WHERE flow_id=? ORDER BY version DESC LIMIT 1",
            (flow_id,),
        ).fetchone()

    def version_by_number(self, flow_id: int, number: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM flow_graph_versions WHERE flow_id=? AND version=?",
            (flow_id, number),
        ).fetchone()

    def version_by_id(self, version_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM flow_graph_versions WHERE id=?", (version_id,)).fetchone()

    def list_versions(self, flow_id: int) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM flow_graph_versions WHERE flow_id=? ORDER BY version",
            (flow_id,),
        ).fetchall())

    def insert_version(self, *, flow_id: int, version: int, digest: str, note: str, created_by: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO flow_graph_versions(flow_id,version,digest,step_count,edge_count,note,created_by,created_at) VALUES(?,?,?,0,0,?,?,?)",
            (flow_id, version, digest, note, created_by, now),
        )
        return int(cursor.lastrowid)

    def set_version_counts(self, version_id: int, step_count: int, edge_count: int) -> None:
        self.connection.execute(
            "UPDATE flow_graph_versions SET step_count=?,edge_count=? WHERE id=?",
            (step_count, edge_count, version_id),
        )

    def bump_flow_version(self, flow_id: int, version: int, now: str) -> None:
        self.connection.execute(
            "UPDATE ceremony_flows SET current_version=?,updated_at=? WHERE id=?",
            (version, now, flow_id),
        )

    def insert_step(self, *, graph_version_id: int, flow_id: int, step: dict[str, Any], ordinal: int) -> int:
        cursor = self.connection.execute(
            "INSERT INTO flow_steps(graph_version_id,flow_id,code,name,kind,required_role,priority,ordinal,max_attempts) VALUES(?,?,?,?,?,?,?,?,?)",
            (graph_version_id, flow_id, step["code"], step["name"], step["kind"], step.get("required_role", ""), step["priority"], ordinal, step["max_attempts"]),
        )
        return int(cursor.lastrowid)

    def insert_edge(self, graph_version_id: int, from_step_id: int, to_step_id: int) -> None:
        self.connection.execute(
            "INSERT INTO flow_edges(graph_version_id,from_step_id,to_step_id) VALUES(?,?,?)",
            (graph_version_id, from_step_id, to_step_id),
        )

    def steps_for_version(self, graph_version_id: int) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM flow_steps WHERE graph_version_id=? ORDER BY ordinal,code,id",
            (graph_version_id,),
        ).fetchall())

    def edges_for_version(self, graph_version_id: int) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT e.*,sf.code AS from_code,st.code AS to_code FROM flow_edges e "
            "JOIN flow_steps sf ON sf.id=e.from_step_id JOIN flow_steps st ON st.id=e.to_step_id "
            "WHERE e.graph_version_id=? ORDER BY sf.ordinal,st.ordinal,e.id",
            (graph_version_id,),
        ).fetchall())

    # ---- 仪式实例 ----

    def create_instance(self, *, flow_id: int, graph_version_id: int, business_key: str, title: str, created_by: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO ceremony_instances(flow_id,graph_version_id,business_key,title,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (flow_id, graph_version_id, business_key, title, created_by, now, now),
        )
        return int(cursor.lastrowid)

    def instance_by_id(self, instance_id: int) -> sqlite3.Row | None:
        sql = "SELECT i.*,f.code AS flow_code,f.name AS flow_name,g.version AS graph_version FROM ceremony_instances i JOIN ceremony_flows f ON f.id=i.flow_id JOIN flow_graph_versions g ON g.id=i.graph_version_id WHERE i.id=?"
        return self.connection.execute(sql, (instance_id,)).fetchone()

    def instance_by_business_key(self, business_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT i.*,f.code AS flow_code,f.name AS flow_name,g.version AS graph_version FROM ceremony_instances i JOIN ceremony_flows f ON f.id=i.flow_id JOIN flow_graph_versions g ON g.id=i.graph_version_id WHERE i.business_key=?",
            (business_key,),
        ).fetchone()

    def insert_run(self, *, instance_id: int, step_id: int, max_attempts: int, available_at: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO flow_step_runs(instance_id,step_id,max_attempts,available_at,updated_at) VALUES(?,?,?,?,?)",
            (instance_id, step_id, max_attempts, available_at, now),
        )
        return int(cursor.lastrowid)

    def run_by_id(self, run_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT r.*,s.code AS step_code,s.name AS step_name,s.kind AS step_kind,s.required_role,s.priority,s.ordinal,i.flow_id,i.business_key "
            "FROM flow_step_runs r JOIN flow_steps s ON s.id=r.step_id JOIN ceremony_instances i ON i.id=r.instance_id WHERE r.id=?",
            (run_id,),
        ).fetchone()

    def runs_for_instance(self, instance_id: int) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT r.*,s.code AS step_code,s.name AS step_name,s.kind AS step_kind,s.required_role,s.priority,s.ordinal "
            "FROM flow_step_runs r JOIN flow_steps s ON s.id=r.step_id WHERE r.instance_id=? ORDER BY s.ordinal,s.code,r.id",
            (instance_id,),
        ).fetchall())

    def expired_running_runs(self, now: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM flow_step_runs WHERE status='running' AND lease_expires_at<>'' AND lease_expires_at<? ORDER BY id",
            (now,),
        ).fetchall())

    def active_instances(self, flow_id: int | None = None) -> list[sqlite3.Row]:
        if flow_id is None:
            return list(self.connection.execute("SELECT * FROM ceremony_instances WHERE status='active' ORDER BY id").fetchall())
        return list(self.connection.execute(
            "SELECT * FROM ceremony_instances WHERE status='active' AND flow_id=? ORDER BY id",
            (flow_id,),
        ).fetchall())

    def ready_runs(self, *, now: str, flow_id: int | None, capabilities: Iterable[str] | None) -> list[sqlite3.Row]:
        clauses = ["r.status='ready'", "(r.available_at='' OR r.available_at<=?)", "i.status='active'"]
        values: list[Any] = [now]
        if flow_id is not None:
            clauses.append("i.flow_id=?")
            values.append(flow_id)
        caps = sorted(set(capabilities or []))
        if caps:
            placeholders = ",".join("?" for _ in caps)
            clauses.append(f"(s.kind IN ({placeholders}) OR s.required_role IN ({placeholders}))")
            values.extend(caps)
            values.extend(caps)
        sql = (
            "SELECT r.*,s.code AS step_code,s.name AS step_name,s.kind AS step_kind,s.required_role,s.priority,s.ordinal "
            "FROM flow_step_runs r JOIN flow_steps s ON s.id=r.step_id "
            "JOIN ceremony_instances i ON i.id=r.instance_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY r.id"
        )
        return list(self.connection.execute(sql, values).fetchall())

    def touch_instance(self, instance_id: int, now: str) -> None:
        self.connection.execute("UPDATE ceremony_instances SET updated_at=? WHERE id=?", (now, instance_id))

    # ---- 放行与变更记录 ----

    def add_release(self, *, run_id: int, instance_id: int, blocked_by_run_id: int | None, actor: str, reason: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO flow_manual_releases(run_id,instance_id,blocked_by_run_id,actor,reason,created_at) VALUES(?,?,?,?,?,?)",
            (run_id, instance_id, blocked_by_run_id, actor, reason, now),
        )

    def releases_for_instance(self, instance_id: int) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM flow_manual_releases WHERE instance_id=? ORDER BY id",
            (instance_id,),
        ).fetchall())

    def add_change(self, *, flow_id: int | None, instance_id: int | None, entity_type: str, entity_id: int | None, action: str, actor: str, reason: str, before: dict[str, Any] | None, after: dict[str, Any] | None, now: str) -> None:
        self.connection.execute(
            "INSERT INTO flow_change_log(flow_id,instance_id,entity_type,entity_id,action,actor,reason,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (flow_id, instance_id, entity_type, entity_id, action, actor, reason,
             json.dumps(before or {}, ensure_ascii=False, sort_keys=True),
             json.dumps(after or {}, ensure_ascii=False, sort_keys=True), now),
        )

    def changes_for_flow(self, flow_id: int, limit: int) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM flow_change_log WHERE flow_id=? ORDER BY id DESC LIMIT ?",
            (flow_id, limit),
        ).fetchall())

    def changes_for_instance(self, instance_id: int, limit: int) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM flow_change_log WHERE instance_id=? ORDER BY id DESC LIMIT ?",
            (instance_id, limit),
        ).fetchall())

    # ---- 领取游标 ----

    def cursor(self, worker_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM flow_claim_cursors WHERE worker_id=?",
            (worker_id,),
        ).fetchone()

    def upsert_cursor(self, *, worker_id: str, last_run_id: int, instance_id: int, now: str) -> None:
        self.connection.execute(
            "INSERT INTO flow_claim_cursors(worker_id,last_run_id,instance_id,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(worker_id) DO UPDATE SET last_run_id=excluded.last_run_id,instance_id=excluded.instance_id,updated_at=excluded.updated_at",
            (worker_id, last_run_id, instance_id, now),
        )

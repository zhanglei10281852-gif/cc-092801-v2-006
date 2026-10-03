from __future__ import annotations

from datetime import UTC, datetime

from app.core.clock import FrozenClock
from app.database import close_connection, get_connection
from app.process.service import ProcessFlowService

FUNERAL_STEPS = [
    {"code": "portrait", "name": "遗像制作", "kind": "photo", "priority": 50, "max_attempts": 2},
    {"code": "hall", "name": "灵堂布置", "kind": "setup", "priority": 50, "max_attempts": 2},
    {"code": "vehicle", "name": "车辆调度", "kind": "dispatch", "priority": 50, "max_attempts": 2},
    {"code": "farewell", "name": "告别仪式", "kind": "ceremony", "priority": 50, "max_attempts": 2},
]
FUNERAL_EDGES = [
    {"from": "portrait", "to": "hall"},
    {"from": "hall", "to": "vehicle"},
    {"from": "hall", "to": "farewell"},
    {"from": "vehicle", "to": "farewell"},
]


def setup_flow(client, code: str = "funeral.std", steps=FUNERAL_STEPS, edges=FUNERAL_EDGES, note: str = "初版") -> None:
    created = client.post("/api/process/flows?actor=admin", json={"code": code, "name": "标准葬礼"})
    assert created.status_code == 201, created.text
    submitted = client.post(
        f"/api/process/flows/{code}/versions?actor=admin",
        json={"note": note, "steps": steps, "edges": edges},
    )
    assert submitted.status_code == 201, submitted.text


def create_instance(client, business_key: str = "FN-001", code: str = "funeral.std", version: int | None = None):
    payload = {"flow_code": code, "business_key": business_key, "title": "张氏葬礼", "created_by": "clerk-1"}
    if version is not None:
        payload["graph_version"] = version
    response = client.post("/api/process/instances", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def status_map(detail: dict) -> dict[str, str]:
    return {step["step_code"]: step["status"] for step in detail["steps"]}


# ---------- 图提交校验 ----------

def test_graph_rejects_cycles_and_unknown_steps_at_submit(client):
    setup_flow(client)
    cycle = client.post("/api/process/flows/funeral.std/versions?actor=admin", json={
        "steps": [
            {"code": "a1", "name": "甲", "kind": "k", "priority": 1, "max_attempts": 1},
            {"code": "b1", "name": "乙", "kind": "k", "priority": 1, "max_attempts": 1},
        ],
        "edges": [{"from": "a1", "to": "b1"}, {"from": "b1", "to": "a1"}],
    })
    assert cycle.status_code == 422
    assert "循环" in cycle.json()["error"]["message"]

    missing = client.post("/api/process/flows/funeral.std/versions?actor=admin", json={
        "steps": [{"code": "a1", "name": "甲", "kind": "k", "priority": 1, "max_attempts": 1}],
        "edges": [{"from": "a1", "to": "ghost"}],
    })
    assert missing.status_code == 422
    assert missing.json()["error"]["context"] == {"steps": ["ghost"]}

    self_loop = client.post("/api/process/flows/funeral.std/versions?actor=admin", json={
        "steps": [{"code": "solo", "name": "独", "kind": "k", "priority": 1, "max_attempts": 1}],
        "edges": [{"from": "solo", "to": "solo"}],
    })
    assert self_loop.status_code == 422

    detail = client.get("/api/process/flows/funeral.std").json()
    assert detail["current_version"] == 1  # 非法提交没有产生新版本


def test_topological_order_is_stable(client):
    setup_flow(client)
    detail = client.get("/api/process/flows/funeral.std").json()
    graph = detail["current_graph"]
    assert graph["topological_order"] == ["portrait", "hall", "vehicle", "farewell"]
    ordinals = [step["ordinal"] for step in graph["steps"]]
    assert ordinals == sorted(ordinals)
    # 边的前置一定出现在后继之前
    position = {code: index for index, code in enumerate(graph["topological_order"])}
    for edge in graph["edges"]:
        assert position[edge["from"]] < position[edge["to"]]


def test_identical_graph_is_idempotent_new_version_changes(client):
    setup_flow(client)
    again = client.post("/api/process/flows/funeral.std/versions?actor=admin",
                        json={"note": "重复提交", "steps": FUNERAL_STEPS, "edges": FUNERAL_EDGES})
    assert again.status_code == 201
    assert again.json()["version"] == 1

    updated = client.post("/api/process/flows/funeral.std/versions?actor=admin", json={
        "note": "增加火化环节",
        "steps": FUNERAL_STEPS + [{"code": "cremation", "name": "火化", "kind": "ceremony", "priority": 50, "max_attempts": 2}],
        "edges": FUNERAL_EDGES + [{"from": "farewell", "to": "cremation"}],
    })
    assert updated.status_code == 201
    assert updated.json()["version"] == 2
    versions = client.get("/api/process/flows/funeral.std/versions").json()["items"]
    assert [item["version"] for item in versions] == [1, 2]


# ---------- 领取与依赖门控 ----------

def test_claim_skips_steps_with_unmet_prerequisites(client):
    setup_flow(client)
    instance = create_instance(client)
    assert status_map(instance) == {"portrait": "ready", "hall": "waiting", "vehicle": "waiting", "farewell": "waiting"}

    claimed = client.post("/api/process/claim", json={"worker_id": "w1", "capabilities": [], "lease_seconds": 60})
    assert claimed.status_code == 200
    run = claimed.json()["run"]
    assert run["step_code"] == "portrait"
    first_cursor = claimed.json()["cursor"]

    # 后续领取不会重复派给正在执行的步骤，也不会跳过依赖派发
    empty = client.post("/api/process/claim", json={"worker_id": "w2", "capabilities": [], "lease_seconds": 60})
    assert empty.json()["run"] is None

    completed = client.post(f"/api/process/runs/{run['run_id']}/complete",
                            json={"worker_id": "w1", "result": {"photo": "done"}, "note": ""})
    assert completed.status_code == 200
    detail = completed.json()
    assert status_map(detail)["hall"] == "ready"
    assert status_map(detail)["vehicle"] == "waiting"

    next_claim = client.post("/api/process/claim", json={"worker_id": "w2", "capabilities": [], "lease_seconds": 60})
    assert next_claim.json()["run"]["step_code"] == "hall"
    assert next_claim.json()["cursor"] > first_cursor


def test_capability_filter(client):
    setup_flow(client)
    create_instance(client)
    claimed = client.post("/api/process/claim", json={"worker_id": "w1", "capabilities": ["dispatch"], "lease_seconds": 60})
    # 唯一就绪的 portrait 需要 photo，能力不匹配时领取为空（被阻塞的步骤自动跳过）
    assert claimed.json()["run"] is None
    matched = client.post("/api/process/claim", json={"worker_id": "w1", "capabilities": ["photo"], "lease_seconds": 60})
    assert matched.json()["run"]["step_code"] == "portrait"


# ---------- 失败传播与人工放行 ----------

def test_failure_propagates_as_blocked_with_reasons(client):
    setup_flow(client)
    create_instance(client)
    run = client.post("/api/process/claim", json={"worker_id": "w1", "capabilities": [], "lease_seconds": 60}).json()["run"]
    failed = client.post(f"/api/process/runs/{run['run_id']}/fail",
                         json={"worker_id": "w1", "error_code": "photo_lost", "message": "底片损坏", "retryable": False})
    assert failed.status_code == 200
    detail = failed.json()
    assert status_map(detail) == {"portrait": "failed", "hall": "blocked", "vehicle": "blocked", "farewell": "blocked"}
    for step in detail["steps"][1:]:
        assert step["blocked_by_run_id"] == run["run_id"]  # 阻塞根因都追溯到 portrait
        assert "portrait" in step["blocked_reason"]
        assert step["blockers"]  # 每个下游都至少有一个失败/阻塞的直接前置
    hall_step = next(step for step in detail["steps"] if step["step_code"] == "hall")
    assert {"step_code": "portrait", "run_id": run["run_id"], "status": "failed"} in hall_step["blockers"]
    vehicle_step = next(step for step in detail["steps"] if step["step_code"] == "vehicle")
    assert {"step_code": "hall", "status": "blocked", "run_id": hall_step["run_id"]} in vehicle_step["blockers"]

    blocked = client.get("/api/process/blocked").json()["items"]
    assert {item["step_code"] for item in blocked} == {"hall", "vehicle", "farewell"}

    # 被阻塞或失败的步骤不会被领取
    claim = client.post("/api/process/claim", json={"worker_id": "w2", "capabilities": [], "lease_seconds": 60})
    assert claim.json()["run"] is None


def test_manual_release_records_reason_and_unblocks_chain(client):
    setup_flow(client)
    create_instance(client)
    run = client.post("/api/process/claim", json={"worker_id": "w1", "capabilities": [], "lease_seconds": 60}).json()["run"]
    client.post(f"/api/process/runs/{run['run_id']}/fail",
                json={"worker_id": "w1", "error_code": "photo_lost", "message": "底片损坏", "retryable": False})
    detail = client.get("/api/process/instances/1").json()
    hall = next(step for step in detail["steps"] if step["step_code"] == "hall")

    # 非阻塞状态不允许放行
    bad = client.post(f"/api/process/runs/{run['run_id']}/release",
                      json={"actor": "manager-1", "reason": "测试"})
    assert bad.status_code == 409

    released = client.post(f"/api/process/runs/{hall['run_id']}/release",
                           json={"actor": "manager-1", "reason": "家属提供备用照片，先行布置灵堂"})
    assert released.status_code == 200
    after = released.json()
    hall_after = next(step for step in after["steps"] if step["step_code"] == "hall")
    assert hall_after["status"] == "ready"
    assert hall_after["released_by"] == "manager-1"
    assert hall_after["releases"][0]["reason"] == "家属提供备用照片，先行布置灵堂"
    # vehicle/farewell 仍等待 hall 完成，不会因为 portrait 失败再次阻塞 hall
    assert status_map(after)["vehicle"] == "waiting"

    # 放行的步骤被领取并完成后，其下游正常就绪
    claimed = client.post("/api/process/claim", json={"worker_id": "w2", "capabilities": [], "lease_seconds": 60}).json()["run"]
    assert claimed["step_code"] == "hall"
    completed = client.post(f"/api/process/runs/{claimed['run_id']}/complete",
                            json={"worker_id": "w2", "result": {}, "note": ""})
    assert status_map(completed.json())["vehicle"] == "ready"


def test_retryable_failure_uses_backoff_with_frozen_clock():
    clock = FrozenClock(datetime(2026, 10, 3, 2, 0, tzinfo=UTC))
    service = ProcessFlowService(get_connection(), clock)
    service.create_flow({"code": "f.clock", "name": "时钟流程"}, "admin")
    service.submit_graph("f.clock", {"note": "", "steps": [
        {"code": "s1", "name": "一", "kind": "k", "priority": 50, "max_attempts": 2}], "edges": []}, "admin")
    service.create_instance({"flow_code": "f.clock", "business_key": "K-1", "created_by": "c"})
    run = service.claim("w1", [], 60)["run"]
    failed = service.fail(run["run_id"], "w1", "transient", "瞬时错误", True)
    step = failed["steps"][0]
    assert step["status"] == "ready"
    assert step["attempt_count"] == 1
    # 退避窗口内不可领取
    assert service.claim("w1", [], 60)["run"] is None
    clock.advance(seconds=2)
    again = service.claim("w1", [], 60)["run"]
    assert again["run_id"] == run["run_id"] and again["attempt_count"] == 2
    service.fail(run["run_id"], "w1", "transient", "再次错误", False)
    detail = service.get_instance(run["instance_id"])
    assert detail["steps"][0]["status"] == "failed"


# ---------- 变更记录 ----------

def test_every_transition_is_recorded(client):
    setup_flow(client)
    create_instance(client)
    run = client.post("/api/process/claim", json={"worker_id": "w1", "capabilities": [], "lease_seconds": 60}).json()["run"]
    client.post(f"/api/process/runs/{run['run_id']}/fail",
                json={"worker_id": "w1", "error_code": "e", "message": "失败信息", "retryable": False})
    detail = client.get("/api/process/instances/1").json()
    hall = next(step for step in detail["steps"] if step["step_code"] == "hall")
    client.post(f"/api/process/runs/{hall['run_id']}/release", json={"actor": "mgr", "reason": "人工理由"})

    changes = client.get("/api/process/instances/1/changes").json()["items"]
    actions = [item["action"] for item in changes]
    assert "create" in actions
    assert "claim" in actions
    assert "fail" in actions
    assert "blocked" in actions
    release_event = next(item for item in changes if item["action"] == "manual_release")
    assert release_event["reason"] == "人工理由"
    assert release_event["before"]["status"] == "blocked"
    assert release_event["after"]["status"] == "ready"

    flow_changes = client.get("/api/process/flows/funeral.std/changes").json()["items"]
    assert {item["action"] for item in flow_changes} >= {"create", "submit_version"}


# ---------- 历史不可变 ----------

def test_graph_update_does_not_rewrite_completed_history(client):
    setup_flow(client)
    instance = create_instance(client, "FN-OLD")
    # 完成旧实例的全部四个步骤
    for _ in range(4):
        run = client.post("/api/process/claim", json={"worker_id": "w1", "capabilities": [], "lease_seconds": 60}).json()["run"]
        client.post(f"/api/process/runs/{run['run_id']}/complete", json={"worker_id": "w1", "result": {}, "note": ""})
    old_detail = client.get(f"/api/process/instances/{instance['instance_id']}").json()
    assert old_detail["status"] == "completed"
    assert old_detail["graph_version"] == 1

    # 提交新版本：新增步骤
    client.post("/api/process/flows/funeral.std/versions?actor=admin", json={
        "note": "加环节",
        "steps": FUNERAL_STEPS + [{"code": "cremation", "name": "火化", "kind": "ceremony", "priority": 50, "max_attempts": 2}],
        "edges": FUNERAL_EDGES + [{"from": "farewell", "to": "cremation"}],
    })
    old_detail = client.get(f"/api/process/instances/{instance['instance_id']}").json()
    assert old_detail["graph_version"] == 1
    assert [step["step_code"] for step in old_detail["steps"]] == ["portrait", "hall", "vehicle", "farewell"]
    assert all(step["status"] == "succeeded" for step in old_detail["steps"])

    new_instance = create_instance(client, "FN-NEW")
    assert new_instance["graph_version"] == 2
    assert "cremation" in status_map(new_instance)


# ---------- 游标与状态在重启后保持 ----------

def test_cursor_and_state_survive_restart(client):
    setup_flow(client)
    create_instance(client, "FN-A")
    create_instance(client, "FN-B")
    first = client.post("/api/process/claim", json={"worker_id": "w1", "capabilities": [], "lease_seconds": 60}).json()
    assert first["run"]["business_key"] == "FN-A"
    second = client.post("/api/process/claim", json={"worker_id": "w1", "capabilities": [], "lease_seconds": 60})
    # FN-A 的 hall 还在 waiting，下一个可领取的是 FN-B 的 portrait
    assert second.json()["run"]["business_key"] == "FN-B"

    # 模拟重启：关闭线程级连接后重新构建服务，游标持久化在 SQLite 中
    close_connection()
    service = ProcessFlowService(get_connection())
    assert service.claim("w1", [], 60)["run"] is None  # 游标之后暂无可领取步骤
    # 显式传入游标 0 从头扫描，仍只能看到进行中的 FN-A portrait（已被 w1 租走，状态 running，不会被重复领取）
    from_start = service.claim("w2", [], 60, cursor_token="0")
    assert from_start["run"] is None

    # 完成 FN-B 的 portrait（租约属于 w1）后，w1 从头扫描即可拿到 FN-B 的 hall
    second_run_id = second.json()["run"]["run_id"]
    detail = service.complete(second_run_id, "w1", {}, "")
    hall_b = next(s for s in detail["steps"] if s["step_code"] == "hall")
    assert hall_b["status"] == "ready"
    wrapped = service.claim("w1", [], 60, cursor_token="0")
    assert wrapped["run"]["business_key"] == "FN-B" and wrapped["run"]["step_code"] == "hall"
    close_connection()


def test_expired_lease_recovery_requeues_then_fails_and_blocks(client):
    setup_flow(client)
    instance = create_instance(client)
    run = client.post("/api/process/claim", json={"worker_id": "w1", "capabilities": [], "lease_seconds": 5}).json()["run"]

    clock = FrozenClock(datetime(2027, 1, 1, 0, 0, 10, tzinfo=UTC))
    service = ProcessFlowService(get_connection(), clock)
    # 第一次租约过期：仍有重试次数，回到 ready
    result = service.recover_expired()
    assert result["recovered"] == [run["run_id"]]
    assert result["failed"] == []
    again = service.claim("w1", [], 60)
    assert again["run"]["run_id"] == run["run_id"] and again["run"]["attempt_count"] == 2
    clock.advance(seconds=61)
    # 重试次数耗尽：置为 failed，下游全部 blocked
    result = service.recover_expired()
    assert result["failed"] == [run["run_id"]]
    detail = service.get_instance(instance["instance_id"])
    assert status_map(detail) == {"portrait": "failed", "hall": "blocked", "vehicle": "blocked", "farewell": "blocked"}
    recovery_events = client.get(f"/api/process/instances/{instance['instance_id']}/changes").json()["items"]
    assert [item["action"] for item in recovery_events if item["action"] == "lease_recovery"].count("lease_recovery") == 2
    close_connection()

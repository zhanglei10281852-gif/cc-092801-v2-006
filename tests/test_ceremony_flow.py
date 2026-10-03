from __future__ import annotations

from datetime import UTC, datetime


def graph_payload(actor: str = "dispatcher", note: str = "初始流程") -> dict:
    # 步骤故意乱序提交，验证拓扑顺序与提交顺序无关
    return {
        "actor": actor,
        "change_note": note,
        "steps": [
            {"key": "farewell", "name": "告别仪式"},
            {"key": "portrait", "name": "遗像制作"},
            {"key": "vehicles", "name": "车辆调度"},
            {"key": "hall", "name": "灵堂布置"},
        ],
        "edges": [
            {"predecessor": "portrait", "successor": "hall", "on_failure": "block"},
            {"predecessor": "portrait", "successor": "vehicles", "on_failure": "ignore"},
            {"predecessor": "hall", "successor": "farewell", "on_failure": "block"},
            {"predecessor": "vehicles", "successor": "farewell", "on_failure": "block"},
        ],
    }


def create_ceremony(client, code: str = "funeral-001") -> dict:
    response = client.post("/api/ceremonies", json={"code": code, "name": "张氏告别仪式", "actor": "dispatcher"})
    assert response.status_code == 201, response.text
    return response.json()


def submit_graph(client, code: str = "funeral-001", payload: dict | None = None) -> dict:
    response = client.post(f"/api/ceremonies/{code}/graph", json=payload or graph_payload())
    assert response.status_code == 201, response.text
    return response.json()


def claim(client, worker: str, code: str = "funeral-001", **extra) -> dict:
    response = client.post(f"/api/ceremonies/{code}/claim", json={"worker_id": worker, "lease_seconds": 60, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def complete(client, step: str, worker: str, code: str = "funeral-001", result: dict | None = None) -> dict:
    response = client.post(f"/api/ceremonies/{code}/steps/{step}/complete", json={"worker_id": worker, "result": result or {}})
    assert response.status_code == 200, response.text
    return response.json()


def fail_portrait(client, code: str = "funeral-001") -> None:
    claimed = claim(client, "w-portrait", code)
    assert claimed["step"]["step_key"] == "portrait"
    response = client.post(
        f"/api/ceremonies/{code}/steps/portrait/fail",
        json={"worker_id": "w-portrait", "error_code": "photo_damaged", "message": "原始照片损毁无法修复"},
    )
    assert response.status_code == 200, response.text


def detail_steps(client, code: str = "funeral-001") -> dict:
    detail = client.get(f"/api/ceremonies/{code}").json()
    return {step["step_key"]: step for step in detail["steps"]}


def test_graph_submission_stable_topo_and_idempotent_resubmit(client):
    create_ceremony(client)
    submitted = submit_graph(client)
    assert submitted["version"] == 1
    assert submitted["deduplicated"] is False
    assert submitted["topo_order"] == ["portrait", "hall", "vehicles", "farewell"]

    detail = client.get("/api/ceremonies/funeral-001").json()
    assert detail["topo_order"] == ["portrait", "hall", "vehicles", "farewell"]
    assert [step["step_key"] for step in detail["steps"]] == detail["topo_order"]
    statuses = {step["step_key"]: step["status"] for step in detail["steps"]}
    assert statuses == {"portrait": "ready", "hall": "pending", "vehicles": "pending", "farewell": "pending"}
    hall = detail_steps(client)["hall"]
    assert hall["prerequisites"] == [
        {"step_key": "portrait", "name": "遗像制作", "on_failure": "block", "status": "ready", "satisfied": False}
    ]

    again = client.post("/api/ceremonies/funeral-001/graph", json=graph_payload())
    assert again.status_code == 200
    assert again.json()["deduplicated"] is True
    versions = client.get("/api/ceremonies/funeral-001/graph/versions").json()["items"]
    assert [item["version"] for item in versions] == [1]


def test_graph_validation_rejects_unknown_steps_and_cycles(client):
    create_ceremony(client)

    unknown = graph_payload()
    unknown["edges"].append({"predecessor": "portrait", "successor": "makeup", "on_failure": "block"})
    response = client.post("/api/ceremonies/funeral-001/graph", json=unknown)
    assert response.status_code == 422
    assert "makeup" in str(response.json()["error"]["context"])

    cyclic = graph_payload()
    cyclic["edges"].append({"predecessor": "farewell", "successor": "portrait", "on_failure": "block"})
    response = client.post("/api/ceremonies/funeral-001/graph", json=cyclic)
    assert response.status_code == 422
    assert "循环" in response.json()["error"]["message"]

    self_loop = graph_payload()
    self_loop["edges"].append({"predecessor": "hall", "successor": "hall", "on_failure": "block"})
    assert client.post("/api/ceremonies/funeral-001/graph", json=self_loop).status_code == 422

    duplicated_step = graph_payload()
    duplicated_step["steps"].append({"key": "hall", "name": "重复灵堂"})
    assert client.post("/api/ceremonies/funeral-001/graph", json=duplicated_step).status_code == 422

    repeated_edge = graph_payload()
    repeated_edge["edges"].append({"predecessor": "portrait", "successor": "hall", "on_failure": "ignore"})
    assert client.post("/api/ceremonies/funeral-001/graph", json=repeated_edge).status_code == 422

    assert client.get("/api/ceremonies/funeral-001/graph/versions").json()["items"] == []


def test_claim_skips_steps_with_unmet_prerequisites(client):
    create_ceremony(client)
    submit_graph(client)

    first = claim(client, "w1")
    assert first["step"]["step_key"] == "portrait"
    assert first["step"]["status"] == "running"

    second = claim(client, "w2")
    assert second["step"] is None
    skipped = {item["step_key"]: item["status"] for item in second["skipped"]}
    assert skipped == {"hall": "pending", "vehicles": "pending", "farewell": "pending"}

    filtered = claim(client, "driver-1", step_keys=["vehicles"])
    assert filtered["step"] is None  # vehicles 前置未满足，即使指定步骤也不会领到

    wrong_worker = client.post("/api/ceremonies/funeral-001/steps/portrait/complete", json={"worker_id": "w2", "result": {}})
    assert wrong_worker.status_code == 409

    done = complete(client, "portrait", "w1", result={"photo": "retouched"})
    assert done["status"] == "succeeded"

    third = claim(client, "w3")
    assert third["step"]["step_key"] == "hall"  # 拓扑顺序先于 vehicles
    vehicles_only = claim(client, "driver-1", step_keys=["vehicles"])
    assert vehicles_only["step"]["step_key"] == "vehicles"


def test_failure_propagates_blocking_by_edge_rules(client):
    create_ceremony(client)
    submit_graph(client)
    fail_portrait(client)

    steps = detail_steps(client)
    assert steps["portrait"]["status"] == "failed"
    assert steps["portrait"]["last_error_code"] == "photo_damaged"
    assert steps["hall"]["status"] == "blocked"
    assert "portrait" in steps["hall"]["blocked_reason"]
    assert steps["farewell"]["status"] == "blocked"  # 沿 block 边传递阻塞
    assert steps["vehicles"]["status"] == "ready"  # ignore 规则忽略前置失败

    claimed = claim(client, "driver-1")
    assert claimed["step"]["step_key"] == "vehicles"  # 自动跳过被阻塞环节
    skipped = {item["step_key"]: item["status"] for item in claimed["skipped"]}
    assert skipped == {"hall": "blocked", "farewell": "blocked"}

    events = client.get("/api/ceremonies/funeral-001/events").json()["items"]
    blocked = {event["step_key"]: event["reason"] for event in events if event["action"] == "step_blocked"}
    assert set(blocked) == {"hall", "farewell"}
    assert all(blocked.values())

    listing = client.get("/api/ceremonies", params={"blocked_only": True}).json()["items"]
    assert [item["code"] for item in listing] == ["funeral-001"]
    assert listing[0]["counts"]["blocked"] == 2


def test_manual_release_records_reason_and_completes_ceremony(client):
    create_ceremony(client)
    submit_graph(client)
    fail_portrait(client)

    not_blocked = client.post("/api/ceremonies/funeral-001/steps/vehicles/release", json={"actor": "director", "reason": "车辆未阻塞"})
    assert not_blocked.status_code == 409

    reason = "家属同意使用临时遗像，灵堂先行布置"
    released = client.post("/api/ceremonies/funeral-001/steps/hall/release", json={"actor": "director", "reason": reason})
    assert released.status_code == 200, released.text
    assert released.json()["status"] == "ready"
    assert released.json()["released"] is True
    assert released.json()["released_by"] == "director"
    assert released.json()["released_reason"] == reason

    steps = detail_steps(client)
    assert steps["farewell"]["status"] == "pending"  # 放行后 farewell 转为等待 hall 完成

    events = client.get("/api/ceremonies/funeral-001/events", params={"step_key": "hall"}).json()["items"]
    release_events = [event for event in events if event["action"] == "step_released"]
    assert release_events[-1]["reason"] == reason
    assert release_events[-1]["actor"] == "director"

    assert claim(client, "w-hall")["step"]["step_key"] == "hall"
    complete(client, "hall", "w-hall")
    assert claim(client, "driver-1")["step"]["step_key"] == "vehicles"
    complete(client, "vehicles", "driver-1")
    assert claim(client, "w-farewell")["step"]["step_key"] == "farewell"
    complete(client, "farewell", "w-farewell")

    detail = client.get("/api/ceremonies/funeral-001").json()
    assert detail["status"] == "active"  # portrait 仍处于失败，仪式不能算完成

    # 提交移除失败步骤的新版本后，其余步骤均已完成，仪式自动办结
    final = graph_payload(note="遗像步骤终止，仪式收尾")
    final["steps"] = [step for step in final["steps"] if step["key"] != "portrait"]
    final["edges"] = [edge for edge in final["edges"] if edge["predecessor"] != "portrait"]
    submitted = client.post("/api/ceremonies/funeral-001/graph", json=final)
    assert submitted.status_code == 201
    detail = client.get("/api/ceremonies/funeral-001").json()
    assert detail["status"] == "completed"
    assert all(step["status"] == "succeeded" for step in detail["steps"])
    actions = [event["action"] for event in client.get("/api/ceremonies/funeral-001/events").json()["items"]]
    assert "ceremony_completed" in actions


def test_version_update_preserves_completed_history(client):
    create_ceremony(client)
    submit_graph(client)
    assert claim(client, "w1")["step"]["step_key"] == "portrait"
    complete(client, "portrait", "w1", result={"photo": "retouched"})

    updated = graph_payload(note="告别仪式改为等待遗体整容")
    updated["steps"].append({"key": "makeup", "name": "遗体整容"})
    updated["edges"] = [
        {"predecessor": "portrait", "successor": "hall", "on_failure": "block"},
        {"predecessor": "portrait", "successor": "vehicles", "on_failure": "ignore"},
        {"predecessor": "hall", "successor": "farewell", "on_failure": "block"},
        {"predecessor": "makeup", "successor": "farewell", "on_failure": "block"},
    ]
    submitted = client.post("/api/ceremonies/funeral-001/graph", json=updated)
    assert submitted.status_code == 201
    assert submitted.json()["version"] == 2
    assert submitted.json()["topo_order"] == ["makeup", "portrait", "hall", "farewell", "vehicles"]

    steps = detail_steps(client)
    assert steps["portrait"]["status"] == "succeeded"  # 已完成历史不被改写
    assert steps["portrait"]["result"] == {"photo": "retouched"}
    assert steps["makeup"]["status"] == "ready"
    assert steps["farewell"]["status"] == "pending"  # 新增前置立即生效

    first = client.get("/api/ceremonies/funeral-001/graph/versions/1").json()
    assert first["topo_order"] == ["portrait", "hall", "vehicles", "farewell"]
    assert {edge["successor"] for edge in first["edges"] if edge["predecessor"] == "vehicles"} == {"farewell"}
    versions = client.get("/api/ceremonies/funeral-001/graph/versions").json()["items"]
    assert [item["version"] for item in versions] == [1, 2]


def test_version_update_recomputes_blocked_steps(client):
    create_ceremony(client)
    submit_graph(client)
    fail_portrait(client)
    assert detail_steps(client)["hall"]["status"] == "blocked"

    updated = graph_payload(note="灵堂布置不再依赖遗像")
    updated["edges"] = [edge for edge in updated["edges"] if not (edge["predecessor"] == "portrait" and edge["successor"] == "hall")]
    submitted = client.post("/api/ceremonies/funeral-001/graph", json=updated)
    assert submitted.status_code == 201

    steps = detail_steps(client)
    assert steps["portrait"]["status"] == "failed"  # 失败事实同样保留
    assert steps["hall"]["status"] == "ready"  # 新版本中阻塞解除
    assert steps["farewell"]["status"] == "pending"


def test_events_cursor_pagination(client):
    create_ceremony(client)
    submit_graph(client)
    fail_portrait(client)
    client.post("/api/ceremonies/funeral-001/steps/hall/release", json={"actor": "director", "reason": "临时遗像先行布置"})

    collected: list[dict] = []
    cursor = 0
    while True:
        page = client.get("/api/ceremonies/funeral-001/events", params={"cursor": cursor, "limit": 3}).json()
        collected.extend(page["items"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    full = client.get("/api/ceremonies/funeral-001/events", params={"limit": 200}).json()["items"]
    assert [event["id"] for event in collected] == [event["id"] for event in full]
    assert [event["id"] for event in collected] == sorted(event["id"] for event in collected)
    assert any(event["action"] == "graph_submitted" for event in collected)


def test_state_and_cursor_survive_restart(client):
    from app.ceremony.service import CeremonyFlowService
    from app.core.clock import FrozenClock
    from app.database import close_connection, get_connection

    clock = FrozenClock(datetime(2026, 10, 1, 8, 0, tzinfo=UTC))
    service = CeremonyFlowService(get_connection(), clock)
    service.create_ceremony({"code": "funeral-009", "name": "重启演练仪式", "actor": "dispatcher"})
    service.submit_graph("funeral-009", graph_payload())
    assert service.claim("funeral-009", "w1", [], 60)["step"]["step_key"] == "portrait"
    service.fail("funeral-009", "portrait", "w1", "photo_damaged", "原始照片损毁")
    before = service.get_ceremony("funeral-009")
    first_page = service.list_events("funeral-009", cursor=0, limit=2)
    assert len(first_page["items"]) == 2 and first_page["has_more"] is True

    close_connection()  # 模拟进程重启：关闭连接后从同一数据库文件恢复
    clock.advance(seconds=30)
    restarted = CeremonyFlowService(get_connection(), clock)

    after = restarted.get_ceremony("funeral-009")
    assert after["topo_order"] == before["topo_order"]
    snapshot = lambda detail: [(step["step_key"], step["status"], step["blocked_reason"]) for step in detail["steps"]]
    assert snapshot(after) == snapshot(before)

    remaining = restarted.list_events("funeral-009", cursor=first_page["next_cursor"], limit=200)["items"]
    all_events = restarted.list_events("funeral-009", cursor=0, limit=200)["items"]
    assert [event["id"] for event in first_page["items"]] + [event["id"] for event in remaining] == [event["id"] for event in all_events]

    restarted.release("funeral-009", "hall", "director", "重启后人工放行")
    claimed = restarted.claim("funeral-009", "w2", [], 60)
    assert claimed["step"]["step_key"] == "hall"

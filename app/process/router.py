from __future__ import annotations

from fastapi import APIRouter, Query

from app.process.schemas import (
    CeremonyCreate,
    GraphSubmit,
    InstanceCreate,
    ManualRequest,
    StepClaim,
    StepFailure,
    StepResult,
)
from app.process.service import ProcessFlowService

router = APIRouter(prefix="/api/process", tags=["仪式流程依赖图"])


def service() -> ProcessFlowService:
    return ProcessFlowService()


@router.post("/flows", status_code=201)
def create_flow(payload: CeremonyCreate, actor: str = Query(..., min_length=1)):
    return service().create_flow(payload.model_dump(), actor)


@router.get("/flows/{flow_code}")
def get_flow(flow_code: str):
    return service().get_flow(flow_code)


@router.post("/flows/{flow_code}/versions", status_code=201)
def submit_graph(flow_code: str, payload: GraphSubmit, actor: str = Query(..., min_length=1)):
    return service().submit_graph(flow_code, payload.model_dump(by_alias=True), actor)


@router.get("/flows/{flow_code}/versions")
def list_versions(flow_code: str):
    return service().list_versions(flow_code)


@router.get("/flows/{flow_code}/changes")
def flow_changes(flow_code: str, limit: int = Query(default=100, ge=1, le=500)):
    return service().flow_changes(flow_code, limit)


@router.post("/instances", status_code=201)
def create_instance(payload: InstanceCreate):
    return service().create_instance(payload.model_dump(exclude_none=True))


@router.get("/instances/{instance_id}")
def get_instance(instance_id: int):
    return service().get_instance(instance_id)


@router.get("/instances/{instance_id}/changes")
def instance_changes(instance_id: int, limit: int = Query(default=100, ge=1, le=500)):
    return service().instance_changes(instance_id, limit)


@router.get("/blocked")
def list_blocked(flow_code: str | None = None):
    return service().list_blocked(flow_code)


@router.post("/claim")
def claim(payload: StepClaim):
    data = payload.model_dump()
    return service().claim(
        data["worker_id"], data["capabilities"], data["lease_seconds"],
        data.get("ceremony_code"), data.get("cursor"),
    )


@router.post("/runs/{run_id}/heartbeat")
def heartbeat(run_id: int, payload: StepClaim):
    return service().heartbeat(run_id, payload.worker_id, payload.lease_seconds)


@router.post("/runs/{run_id}/complete")
def complete_run(run_id: int, payload: StepResult):
    return service().complete(run_id, payload.worker_id, payload.result, payload.note)


@router.post("/runs/{run_id}/fail")
def fail_run(run_id: int, payload: StepFailure):
    return service().fail(run_id, payload.worker_id, payload.error_code, payload.message, payload.retryable)


@router.post("/runs/{run_id}/release")
def manual_release(run_id: int, payload: ManualRequest):
    return service().manual_release(run_id, payload.actor, payload.reason)


@router.post("/recovery/expired-leases")
def recover_expired(actor: str = Query(default="recovery-worker", min_length=1)):
    return service().recover_expired(actor)

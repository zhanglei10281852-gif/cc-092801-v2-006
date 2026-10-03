from __future__ import annotations

from fastapi import APIRouter, Query, Response

from app.ceremony.schemas import CeremonyCreate, GraphSubmit, StepClaim, StepComplete, StepFail, StepRelease
from app.ceremony.service import CeremonyFlowService

router = APIRouter(prefix="/api/ceremonies", tags=["仪式流程依赖图"])


def service() -> CeremonyFlowService:
    return CeremonyFlowService()


@router.post("", status_code=201)
def create_ceremony(payload: CeremonyCreate):
    return service().create_ceremony(payload.model_dump())


@router.get("")
def list_ceremonies(blocked_only: bool = Query(default=False)):
    return {"items": service().list_ceremonies(blocked_only=blocked_only)}


@router.get("/{code}")
def get_ceremony(code: str):
    return service().get_ceremony(code)


@router.post("/{code}/graph")
def submit_graph(code: str, payload: GraphSubmit, response: Response):
    result = service().submit_graph(code, payload.model_dump())
    response.status_code = 200 if result["deduplicated"] else 201
    return result


@router.get("/{code}/graph/versions")
def list_versions(code: str):
    return {"items": service().list_versions(code)}


@router.get("/{code}/graph/versions/{version}")
def get_version(code: str, version: int):
    return service().get_version(code, version)


@router.get("/{code}/events")
def list_events(code: str, cursor: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=200), step_key: str | None = None):
    return service().list_events(code, cursor=cursor, limit=limit, step_key=step_key)


@router.post("/{code}/claim")
def claim_step(code: str, payload: StepClaim):
    return service().claim(code, payload.worker_id, payload.step_keys, payload.lease_seconds)


@router.post("/{code}/steps/{step_key}/complete")
def complete_step(code: str, step_key: str, payload: StepComplete):
    return service().complete(code, step_key, payload.worker_id, payload.result)


@router.post("/{code}/steps/{step_key}/fail")
def fail_step(code: str, step_key: str, payload: StepFail):
    return service().fail(code, step_key, payload.worker_id, payload.error_code, payload.message)


@router.post("/{code}/steps/{step_key}/release")
def release_step(code: str, step_key: str, payload: StepRelease):
    return service().release(code, step_key, payload.actor, payload.reason)

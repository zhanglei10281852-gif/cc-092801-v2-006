from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CeremonyCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)
    actor: str = Field(min_length=1, max_length=120)


class StepSpec(BaseModel):
    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=120)


class EdgeSpec(BaseModel):
    predecessor: str = Field(min_length=1, max_length=64)
    successor: str = Field(min_length=1, max_length=64)
    on_failure: Literal["block", "ignore"] = "block"


class GraphSubmit(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    change_note: str = Field(default="", max_length=500)
    steps: list[StepSpec] = Field(min_length=1, max_length=200)
    edges: list[EdgeSpec] = Field(default_factory=list, max_length=2000)


class StepClaim(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    step_keys: list[str] = Field(default_factory=list, max_length=200)
    lease_seconds: int = Field(default=60, ge=5, le=3600)


class StepComplete(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    result: dict[str, Any] = Field(default_factory=dict)


class StepFail(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    error_code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=2000)


class StepRelease(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CeremonyCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)


class InstanceCreate(BaseModel):
    flow_code: str = Field(min_length=2, max_length=64)
    business_key: str = Field(min_length=2, max_length=120)
    title: str = Field(default="", max_length=200)
    created_by: str = Field(min_length=1, max_length=120)
    graph_version: int | None = Field(default=None, ge=1)


class StepDefinition(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(default="custom", min_length=1, max_length=64)
    required_role: str = Field(default="", max_length=64)
    priority: int = Field(default=50, ge=0, le=100)
    max_attempts: int = Field(default=3, ge=1, le=20)


class EdgeDefinition(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_step: str = Field(alias="from", min_length=2, max_length=64)
    to_step: str = Field(alias="to", min_length=2, max_length=64)


class GraphSubmit(BaseModel):
    note: str = Field(default="", max_length=1000)
    steps: list[StepDefinition] = Field(min_length=1, max_length=300)
    edges: list[EdgeDefinition] = Field(default_factory=list, max_length=20000)


class StepClaim(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    lease_seconds: int = Field(default=60, ge=5, le=3600)
    ceremony_code: str | None = Field(default=None, min_length=2, max_length=64)
    cursor: str | None = Field(default=None, max_length=2000)


class StepResult(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    result: dict[str, Any] = Field(default_factory=dict)
    note: str = Field(default="", max_length=1000)


class StepFailure(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    error_code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=2000)
    retryable: bool = True


class ManualRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)

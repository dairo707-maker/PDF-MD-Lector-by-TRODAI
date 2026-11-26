"""Pydantic request and response schemas for the lazy local REST / OpenAPI 3.1 API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"
    app: str = "PDF/MD Lector"
    version: str = "0.1.0"


class ToolParamSchema(BaseModel):
    name: str
    kind: str
    default: Any = None
    choices: list[Any] | None = None
    min_val: float | int | None = None
    max_val: float | int | None = None


class ToolInfo(BaseModel):
    id: str
    category: str
    params: list[ToolParamSchema] = Field(default_factory=list)


class ToolRunRequest(BaseModel):
    inputs: list[str] = Field(default_factory=list)
    output: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class ToolRunResponse(BaseModel):
    success: bool
    tool_id: str
    output_paths: list[str] = Field(default_factory=list)
    summary: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class AiAskRequest(BaseModel):
    path: str
    question: str
    provider: str = "local_bm25"
    model: str = ""
    endpoint_url: str | None = None
    consent_granted: bool = False
    top_k: int = Field(default=4, ge=1, le=20)


class AiCitationSchema(BaseModel):
    source_path: str
    doc_type: str
    page_number: int | None = None
    section: str | None = None
    score: float = 0.0
    text: str = ""


class AiAskResponse(BaseModel):
    answer_text: str
    provider: str
    consent_blocked: bool = False
    used_local_only: bool = True
    error: str | None = None
    citations: list[AiCitationSchema] = Field(default_factory=list)

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: int
    username: str
    display_name: str
    role: str
    department: str


@dataclass(slots=True)
class Citation:
    source: str
    section: str
    chunk_id: str
    quote: str
    score: float


@dataclass(slots=True)
class ToolResult:
    ok: bool
    content: str
    data: dict[str, Any] = field(default_factory=dict)
    citations: list[Citation] = field(default_factory=list)


@dataclass(slots=True)
class AgentStep:
    phase: Literal["plan", "act", "observe"]
    detail: str
    tool_name: str | None = None


class LoginRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=128)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class ResolveCaseRequest(BaseModel):
    resolution: str = Field(min_length=2, max_length=1000)


class ReviewLeaveRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=500)


class ReindexResponse(BaseModel):
    chunk_count: int
    document_hash: str

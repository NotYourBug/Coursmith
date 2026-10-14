"""Strict job requests and minimal public views; execution inputs remain private."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..domain import Actor
from ..production.models import ModelPublic, ProductionInput


TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
MODIFYING_KINDS = frozenset({"course_draft_generate", "outline_generate",
                           "chapters_generate", "chapter_regenerate"})


class JobRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)

    kind: Literal["course_draft_generate", "outline_generate", "chapters_generate",
                  "chapter_regenerate", "draft_validate", "connection_test"]
    project_id: int | None = Field(ge=1, le=9223372036854775807)
    project_revision: int | None = Field(ge=1, le=9223372036854775807)
    chapter_ids: tuple[str, ...] = Field(default=(), max_length=60)
    parent_job_id: int | None = Field(default=None, ge=1, le=9223372036854775807)

    @field_validator("chapter_ids")
    @classmethod
    def validate_chapters(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Chapter identities must be unique.")
        for cid in value:
            if str(UUID(cid)) != cid:
                raise ValueError("Invalid chapter identity.")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> JobRequest:
        if self.kind == "connection_test":
            if self.project_id is not None or self.project_revision is not None or self.chapter_ids:
                raise ValueError("Connection tests have no project or chapters.")
        elif self.project_id is None or self.project_revision is None:
            raise ValueError("A project and revision are required.")
        if self.kind not in ("chapters_generate", "chapter_regenerate") and self.chapter_ids:
            raise ValueError("This job does not select chapters.")
        if self.kind == "chapter_regenerate" and len(self.chapter_ids) != 1:
            raise ValueError("Choose exactly one chapter.")
        return self


@dataclass(frozen=True)
class JobView:
    id: int
    revision: int
    state: str
    reason_code: str | None


@dataclass(frozen=True)
class RecoveryPreview:
    unchanged_input: bool
    completed_chapter_ids: tuple[str, ...]
    pending_chapter_ids: tuple[str, ...]
    unknown_call_ids: tuple[int, ...]


@dataclass(frozen=True)
class Claim:
    job_id: int
    attempt_id: int
    attempt_token: str = field(repr=False)
    lease_until: datetime
    actor: Actor
    input: ProductionInput | None = field(repr=False)
    config: ModelPublic


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def job_view(row) -> JobView:
    return JobView(row["id"], row["revision"], row["state"], row["reason_code"])


def frozen_payload(frozen: ProductionInput) -> dict:
    """Complete Task 3 value, including decision metadata excluded from its hash."""
    outline = None if frozen.outline is None else {
        "id": frozen.outline.id, "revision": frozen.outline.revision,
        "project_id": frozen.outline.project_id,
        "items": [item.model_dump(warnings=False) for item in frozen.outline.items],
        "confirmed": frozen.outline.confirmed,
    }
    return {
        "project_id": frozen.project_id, "project_revision": frozen.project_revision,
        "mode": frozen.mode, "brief_revision": frozen.brief_revision,
        "brief": frozen.brief.model_dump(warnings=False),
        "references": [{"id": ref.id, "revision": ref.revision,
                        "data": ref.data.model_dump(warnings=False)} for ref in frozen.references],
        "outline": outline,
        "chapters": [{"id": c.id, "body_revision": c.body_revision, "markdown": c.markdown}
                     for c in frozen.chapters],
        "input_hash": frozen.input_hash,
    }

"""Strict production inputs and immutable service views."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .briefs import brief_text_total, source_text, title_text


class BriefInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)

    title: str = Field(min_length=1, max_length=200)
    audience: str = Field(default="", max_length=2000)
    prerequisites: str = Field(default="", max_length=2000)
    goal: str = Field(default="", max_length=2000)
    difficulty: str = Field(default="", max_length=2000)
    chapter_count: int = Field(default=10, ge=1, le=60)
    category_id: int | None = Field(default=None, ge=1, le=9223372036854775807)
    content_kind: Literal["course", "guide", "handbook"] = "course"

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return title_text(value)

    @field_validator("audience", "prerequisites", "goal", "difficulty")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return source_text(value)

    @model_validator(mode="after")
    def validate_total(self) -> BriefInput:
        brief_text_total(self.title, self.audience, self.prerequisites, self.goal, self.difficulty)
        return self


class ReferenceInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)

    text: str = Field(max_length=20000)
    source_note: str = Field(default="", max_length=1000)
    source_status: Literal["unprovided", "pending", "owner_noted"] = "unprovided"

    @field_validator("text", "source_note")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return source_text(value)

    @model_validator(mode="after")
    def validate_source_status(self) -> ReferenceInput:
        if not self.source_note.strip() and self.source_status != "unprovided":
            raise ValueError("A source explanation is required for this source status.")
        return self


@dataclass(frozen=True)
class ProjectView:
    id: int
    revision: int
    mode: str
    status: str
    brief_revision: int
    brief: BriefInput


@dataclass(frozen=True)
class ReferenceView:
    id: int
    revision: int
    data: ReferenceInput

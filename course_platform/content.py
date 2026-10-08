"""Course package models, validation, and collision-safe publishing."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def _safe_relative_path(value: str) -> str:
    value = unquote(value, errors="strict")
    if not value or "\x00" in value:
        raise ValueError("path must be a non-empty relative path")
    if "\\" in value or ":" in value or "%" in value or "?" in value or "#" in value:
        raise ValueError("path must use forward slashes")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("path must not contain control characters")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"unsafe relative path: {value}")
    return path.as_posix()


class ChapterManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int = Field(gt=0)
    title: str = Field(min_length=1)
    path: str
    free_preview: bool = False
    duration_minutes: int | None = Field(default=None, gt=0)

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("chapter title must not be blank")
        return value.strip()

    @field_validator("path")
    @classmethod
    def path_must_be_safe(cls, value: str) -> str:
        return _safe_relative_path(value)


class CourseManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_id: str = Field(min_length=1)
    slug: str = Field(min_length=1)
    title: str = Field(min_length=1)
    category: str = Field(min_length=1)
    version: str
    status: Literal["draft", "published"]
    chapter_count: int = Field(gt=0)
    chapters: list[ChapterManifest] = Field(min_length=1)
    free_chapters: list[int] = Field(default_factory=list)
    contains_ai_generated_content: bool = False
    ai_disclosure: str = ""
    source_manifest: str
    license_file: str

    @field_validator("course_id", "title", "category")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("manifest text fields must not be blank")
        return value.strip()

    @field_validator("slug")
    @classmethod
    def slug_must_be_safe(cls, value: str) -> str:
        if not _SLUG_RE.fullmatch(value):
            raise ValueError("slug must contain lowercase letters, numbers, and hyphens")
        return value

    @field_validator("version")
    @classmethod
    def version_must_be_semver(cls, value: str) -> str:
        if not _VERSION_RE.fullmatch(value):
            raise ValueError("version must use semantic version format, such as 0.1.0")
        return value

    @field_validator("source_manifest", "license_file")
    @classmethod
    def metadata_paths_must_be_safe(cls, value: str) -> str:
        return _safe_relative_path(value)

    @field_validator("free_chapters")
    @classmethod
    def free_chapters_must_be_positive(cls, value: list[int]) -> list[int]:
        if any(number <= 0 for number in value):
            raise ValueError("free_chapters must contain positive chapter numbers")
        return sorted(set(value))

    @model_validator(mode="after")
    def validate_chapter_relationships(self) -> "CourseManifest":
        numbers = [chapter.number for chapter in self.chapters]
        if self.chapter_count != len(self.chapters):
            raise ValueError("chapter_count must equal the number of chapters")
        if len(set(numbers)) != len(numbers):
            raise ValueError("chapter numbers must be unique")
        if not set(self.free_chapters).issubset(numbers):
            raise ValueError("free_chapters must reference existing chapters")
        preview_numbers = {
            chapter.number for chapter in self.chapters if chapter.free_preview
        }
        if set(self.free_chapters) != preview_numbers:
            raise ValueError(
                "free_chapters must exactly match chapters marked free_preview"
            )
        if self.contains_ai_generated_content and not self.ai_disclosure.strip():
            raise ValueError("ai_disclosure is required when AI content is present")
        return self


@dataclass(frozen=True)
class CoursePackage:
    manifest: CourseManifest
    root: Path
    chapter_files: list[Path]


@dataclass(frozen=True)
class ValidationReport:
    ok: bool
    errors: list[str]
    warnings: list[str]


def _read_manifest(path: Path) -> CourseManifest:
    try:
        data = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("manifest.json is missing") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest.json is invalid JSON: {exc.msg}") from exc
    return CourseManifest.model_validate(data)


def _resolved_child(root: Path, relative_path: str) -> Path:
    root = root.resolve()
    candidate = (root / relative_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"path escapes package root: {relative_path}")
    return candidate


def load_course_package(path: Path) -> CoursePackage:
    root = Path(path).resolve()
    if not root.is_dir():
        raise ValueError(f"course package directory does not exist: {path}")
    manifest = _read_manifest(root)
    chapter_files = [_resolved_child(root, chapter.path) for chapter in manifest.chapters]
    return CoursePackage(manifest=manifest, root=root, chapter_files=chapter_files)


def _optional_files(root: Path, directory: str) -> list[Path]:
    optional_root = root / directory
    if not optional_root.exists():
        return []
    if optional_root.is_symlink() or not optional_root.is_dir():
        raise ValueError(f"{directory} must be a real directory inside the package")
    files: list[Path] = []
    resolved_root = root.resolve()
    for candidate in optional_root.rglob("*"):
        if candidate.is_symlink():
            raise ValueError(f"symbolic links are not allowed: {candidate.relative_to(root)}")
        resolved = candidate.resolve()
        if resolved_root not in resolved.parents:
            raise ValueError(f"path escapes package root: {candidate.relative_to(root)}")
        if candidate.is_file():
            files.append(candidate)
    return files


def validate_course_package(path: Path) -> ValidationReport:
    # Import lazily: snapshot inspection uses the manifest models above.
    from .content_inspection import (
        _confirm_snapshot,
        _package_root,
        _read_snapshot,
        _validate_snapshot,
    )

    try:
        root = _package_root(path)
        files, inventory = _read_snapshot(root)
        manifest, _ = _validate_snapshot(files)
        _confirm_snapshot(root, files, inventory)
    except (ValueError, OSError, RecursionError) as exc:
        return ValidationReport(ok=False, errors=[str(exc)], warnings=[])
    warnings: list[str] = []
    if manifest.status == "published" and not manifest.free_chapters:
        warnings.append("published course has no free preview chapter")
    return ValidationReport(ok=True, errors=[], warnings=warnings)


def _copy_if_present(source: Path, target: Path) -> None:
    if source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def publish_course(
    source_dir: Path,
    content_root: Path,
    manifest: CourseManifest,
    overwrite: bool = False,
) -> Path:
    """Validate and atomically publish a package without replacing a slug."""
    if overwrite:
        raise ValueError("overwrite is intentionally unsupported for published courses")
    source_dir = Path(source_dir).resolve()
    content_root = Path(content_root).resolve()
    report = validate_course_package(source_dir)
    if not report.ok:
        raise ValueError("course package is invalid: " + "; ".join(report.errors))
    loaded = load_course_package(source_dir)
    if loaded.manifest.model_dump() != manifest.model_dump():
        raise ValueError("provided manifest does not match source manifest")

    content_root.mkdir(parents=True, exist_ok=True)
    target = content_root / manifest.slug
    if target.exists():
        raise FileExistsError(f"course slug already exists: {manifest.slug}")
    temporary = Path(tempfile.mkdtemp(prefix=f".{manifest.slug}.", dir=content_root))
    try:
        for relative in (
            "manifest.json",
            "index.html",
            manifest.source_manifest,
            manifest.license_file,
            "CHANGELOG.md",
        ):
            _copy_if_present(source_dir / relative, temporary / relative)
        for chapter in manifest.chapters:
            _copy_if_present(source_dir / chapter.path, temporary / chapter.path)
        for optional_dir in ("assets", "downloads"):
            for source in _optional_files(source_dir, optional_dir):
                relative = source.relative_to(source_dir)
                _copy_if_present(source, temporary / relative)
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target

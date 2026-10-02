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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_REMOTE_RE = re.compile(r"(?:https?:)?//", re.IGNORECASE)
_SCRIPT_SRC_RE = re.compile(r"<script\b[^>]*\bsrc\s*=\s*['\"]([^'\"]+)", re.I)
_STYLESHEET_RE = re.compile(
    r"<link\b[^>]*\b(?:href\s*=\s*['\"]([^'\"]+)['\"][^>]*\brel\s*=\s*['\"]stylesheet|"
    r"rel\s*=\s*['\"]stylesheet['\"][^>]*\bhref\s*=\s*['\"]([^'\"]+))",
    re.I,
)
_IMAGE_SRC_RE = re.compile(r"<img\b[^>]*\bsrc\s*=\s*['\"]([^'\"]+)", re.I)
_SCRIPT_TAG_RE = re.compile(r"<script\b", re.I)
_ACTIVE_TAG_RE = re.compile(r"<(?:iframe|object|embed|base)\b", re.I)
_EVENT_HANDLER_RE = re.compile(r"\son[a-z0-9_-]+\s*=", re.I)
_JAVASCRIPT_URL_RE = re.compile(r"(?:href|src|action)\s*=\s*['\"]\s*javascript:", re.I)
_REMOTE_CSS_RE = re.compile(r"(?:@import\s+|url\s*\(\s*['\"]?)(?:https?:)?//", re.I)


def _safe_relative_path(value: str) -> str:
    if not value or "\x00" in value:
        raise ValueError("path must be a non-empty relative path")
    if "\\" in value:
        raise ValueError("path must use forward slashes")
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


def _check_external_resources(file_path: Path, text: str) -> list[str]:
    errors: list[str] = []
    for value in _SCRIPT_SRC_RE.findall(text):
        if _REMOTE_RE.search(value):
            errors.append(f"{file_path.name}: external script is not allowed: {value}")
    for match in _STYLESHEET_RE.findall(text):
        value = next((item for item in match if item), "")
        if _REMOTE_RE.search(value):
            errors.append(f"{file_path.name}: external stylesheet is not allowed: {value}")
    for value in _IMAGE_SRC_RE.findall(text):
        if _REMOTE_RE.search(value):
            errors.append(f"{file_path.name}: external image is not allowed: {value}")
    if _SCRIPT_TAG_RE.search(text):
        errors.append(f"{file_path.name}: script elements are not allowed")
    if _ACTIVE_TAG_RE.search(text):
        errors.append(f"{file_path.name}: embedded active content is not allowed")
    if _EVENT_HANDLER_RE.search(text):
        errors.append(f"{file_path.name}: inline event handlers are not allowed")
    if _JAVASCRIPT_URL_RE.search(text):
        errors.append(f"{file_path.name}: javascript URLs are not allowed")
    if _REMOTE_CSS_RE.search(text):
        errors.append(f"{file_path.name}: remote CSS resources are not allowed")
    return errors


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


def _check_html(file_path: Path) -> list[str]:
    try:
        text = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [f"{file_path.name}: HTML must be UTF-8"]
    errors = []
    lowered = text.lower()
    for marker in ("<!doctype html", "<html", "<head", "<body"):
        if marker not in lowered:
            errors.append(f"{file_path.name}: missing HTML marker {marker}")
    errors.extend(_check_external_resources(file_path, text))
    return errors


def validate_course_package(path: Path) -> ValidationReport:
    root = Path(path).resolve()
    errors: list[str] = []
    warnings: list[str] = []
    try:
        package = load_course_package(root)
    except Exception as exc:
        return ValidationReport(ok=False, errors=[str(exc)], warnings=[])

    manifest = package.manifest
    required = ["index.html", manifest.source_manifest, manifest.license_file]
    for relative in required:
        try:
            unresolved = root / relative
            candidate = _resolved_child(root, relative)
            if unresolved.is_symlink():
                errors.append(f"symbolic links are not allowed: {relative}")
            elif not candidate.is_file():
                errors.append(f"required file is missing: {relative}")
        except ValueError as exc:
            errors.append(str(exc))

    for chapter, chapter_file in zip(manifest.chapters, package.chapter_files):
        unresolved = root / chapter.path
        if unresolved.is_symlink():
            errors.append(f"symbolic links are not allowed: {chapter.path}")
        elif not chapter_file.is_file():
            errors.append(f"chapters/{chapter.number:02d}.html is missing: {chapter.path}")
        else:
            errors.extend(_check_html(chapter_file))

    index_file = root / "index.html"
    if index_file.is_file():
        errors.extend(_check_html(index_file))
    for optional_dir in ("assets", "downloads"):
        try:
            _optional_files(root, optional_dir)
        except ValueError as exc:
            errors.append(str(exc))
    if manifest.status == "published" and not manifest.free_chapters:
        warnings.append("published course has no free preview chapter")
    return ValidationReport(ok=not errors, errors=errors, warnings=warnings)


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

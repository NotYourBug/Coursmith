"""Inspect immutable byte snapshots; never serve a path after verifying other bytes."""

from __future__ import annotations

import hashlib
import io
import os
import posixpath
import re
import stat
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import tinycss2

from .content import CourseManifest, _safe_relative_path
from .domain import BusinessError


@dataclass(frozen=True)
class PackageInspection:
    course_id: str
    slug: str
    version: str
    fingerprint: str
    preview_assets: frozenset[str]
    all_assets: frozenset[str]
    pdf_ready: bool
    zip_ready: bool


def _unavailable() -> BusinessError:
    return BusinessError("package_unavailable", "Course package is unavailable.", 409)


def _is_link(info: os.stat_result) -> bool:
    # Windows junctions/reparse points must not bypass the symlink policy.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _check_segments(path: Path) -> None:
    for segment in (*reversed(path.parents), path):
        if _is_link(segment.lstat()):
            raise ValueError("symbolic links are not allowed")


def _package_root(root: Path) -> Path:
    root = Path(root)
    if not root.is_absolute():
        root = Path.cwd() / root
    # Check the caller's segments before abspath collapses link/../ sequences.
    _check_segments(root)
    return Path(os.path.abspath(root))


def _signature(info: os.stat_result) -> tuple:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _inventory(root: Path) -> dict[str, tuple]:
    _check_segments(root)
    if not root.is_dir():
        raise ValueError("course package directory does not exist")
    result = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        _check_segments(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                # DirEntry.stat on Windows omits file identity (st_ino/st_dev).
                info = path.lstat()
                if _is_link(info):
                    raise ValueError(f"symbolic links are not allowed: {relative}")
                if _safe_relative_path(relative) != relative:
                    raise ValueError("ambiguous package filename")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                    # Track directory identity, not its timestamp/size.
                    result[relative + "/"] = (info.st_dev, info.st_ino, info.st_mode)
                elif stat.S_ISREG(info.st_mode):
                    result[relative] = _signature(info)
                else:
                    raise ValueError("package contains a non-regular file")
    info = root.lstat()
    result["/"] = (info.st_dev, info.st_ino, info.st_mode)
    return result


def _read_snapshot(root: Path) -> tuple[dict[str, bytes], dict[str, tuple]]:
    inventory = _inventory(root)
    files = {}
    for relative, signature in sorted(inventory.items()):
        if relative.endswith("/"):
            continue
        path = root / relative
        _check_segments(path)
        if _signature(path.lstat()) != signature:
            raise ValueError("package changed during read")
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                _is_link(before)
                or not stat.S_ISREG(before.st_mode)
                or _signature(before) != signature
            ):
                raise ValueError("package changed during open")
            files[relative] = stream.read()
            if _signature(os.fstat(stream.fileno())) != signature:
                raise ValueError("package changed during read")
        _check_segments(path)
        if _signature(path.lstat()) != signature:
            raise ValueError("package changed during read")
    if _inventory(root) != inventory:
        raise ValueError("package changed during read")
    return files, inventory


def _confirm_snapshot(
    root: Path, files: dict[str, bytes], inventory: dict[str, tuple]
) -> None:
    # Windows ctime is creation time: an equal-length edit with restored mtime
    # can evade stat comparisons. Compare bytes again before returning.
    current_files, current_inventory = _read_snapshot(root)
    if current_inventory != inventory or current_files != files:
        raise ValueError("package changed during verification")


def _local_reference(source: str, value: str) -> str | None:
    value = unquote(value.strip(), errors="strict")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("dangerous URL control characters")
    if "\\" in value or "%" in value or ":" in value:
        raise ValueError("external or dangerous URL is not allowed")
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.path.startswith("/"):
        raise ValueError("external or absolute URL is not allowed")
    if not parts.path:
        if parts.fragment and not parts.query:
            return None
        raise ValueError("empty resource URL")
    relative = posixpath.normpath(posixpath.join(posixpath.dirname(source), parts.path))
    return _safe_relative_path(relative)


def _css_references(text: str) -> list[tuple[str, bool]]:
    """Return URLs plus whether the dependency is an imported stylesheet."""
    references = []
    pending = [tinycss2.parse_component_value_list(text, skip_comments=True)]
    while pending:
        tokens = pending.pop()
        import_pending = False
        for token in tokens:
            if token.type == "error":
                raise ValueError("invalid CSS resource syntax")
            if token.type == "whitespace":
                continue
            if token.type == "at-keyword" and token.lower_value == "import":
                import_pending = True
                continue
            if import_pending and token.type == "string":
                references.append((token.value, True))
            if token.type == "url":
                references.append((token.value, import_pending))
            elif token.type == "function":
                if token.lower_name == "url":
                    arguments = [
                        item for item in token.arguments if item.type != "whitespace"
                    ]
                    if len(arguments) != 1 or arguments[0].type != "string":
                        raise ValueError("invalid CSS URL")
                    references.append((arguments[0].value, import_pending))
                else:
                    if token.lower_name in {"image-set", "-webkit-image-set"}:
                        references.extend(
                            (item.value, False)
                            for item in token.arguments
                            if item.type == "string"
                        )
                    pending.append(token.arguments)
            elif hasattr(token, "content"):
                pending.append(token.content)
            import_pending = False
    return references


class _PackageHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.references: list[tuple[str, bool, bool]] = []
        self.styles: list[str] = []
        self.errors: list[str] = []
        self.in_style = False
        self.tags: set[str] = set()
        self.doctype = False

    def handle_decl(self, decl):
        self.doctype |= decl.lower().startswith("doctype html")

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        if tag == "script":
            self.errors.append("script elements are not allowed")
        if tag in {
            "iframe",
            "object",
            "embed",
            "base",
            "frame",
            "frameset",
            "form",
            "portal",
        }:
            self.errors.append("embedded active content is not allowed")
        attributes = dict(attrs)
        link_rel = (attributes.get("rel") or "").lower().split()
        stylesheet = tag == "link" and "stylesheet" in link_rel
        resource_link = stylesheet or (
            tag == "link" and bool({"icon", "preload"} & set(link_rel))
        )
        if tag == "meta" and "http-equiv" in attributes:
            self.errors.append("active meta directives are not allowed")
        for name, value in attrs:
            if name.startswith("on"):
                self.errors.append("inline event handlers are not allowed")
            if name == "srcdoc":
                self.errors.append("embedded active content is not allowed")
            if name in {"ping", "xml:base"}:
                self.errors.append("active URL attributes are not allowed")
            if value is None:
                continue
            if name == "style":
                self.styles.append(value)
            elif name in {"srcset", "imagesrcset"}:
                for candidate in value.split(","):
                    tokens = candidate.split()
                    if not tokens:
                        self.errors.append("empty srcset resource")
                    else:
                        self.references.append((tokens[0], True, False))
            elif name in {
                "src",
                "poster",
                "background",
                "href",
                "xlink:href",
                "action",
                "formaction",
                "data",
                "lowsrc",
                "dynsrc",
                "cite",
                "longdesc",
                "profile",
                "manifest",
                "codebase",
                "archive",
            }:
                # Navigation/attachment anchors are validated, never preview assets.
                embedded = (
                    name in {"src", "poster", "background", "lowsrc", "dynsrc"}
                    or (resource_link and name == "href")
                    or (tag in {"image", "use"} and name in {"href", "xlink:href"})
                )
                self.references.append((value, embedded, stylesheet))
        if tag == "style":
            self.in_style = True

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.in_style:
            self.styles.append(data)


def _asset_dependency(
    files: dict[str, bytes], source: str, value: str, protected: set[str]
) -> str | None:
    relative = _local_reference(source, value)
    if relative is None:
        return None
    if not relative.startswith("assets/") or relative in protected:
        raise ValueError("embedded resources must be inside assets")
    if relative not in files:
        raise ValueError(f"resource is missing: {relative}")
    return relative


def _validate_snapshot(
    files: dict[str, bytes],
) -> tuple[CourseManifest, dict[str, set[str]]]:
    if "manifest.json" not in files:
        raise ValueError("manifest.json is missing")
    manifest = CourseManifest.model_validate_json(files["manifest.json"])
    required = {"index.html", manifest.source_manifest, manifest.license_file}
    required.update(chapter.path for chapter in manifest.chapters)
    protected = required | {"manifest.json"}
    missing = required - files.keys()
    if missing:
        raise ValueError("required file is missing: " + ", ".join(sorted(missing)))
    dependencies: dict[str, set[str]] = {}
    css_files = {name for name in files if name.lower().endswith(".css")}
    html_files = {"index.html", *(chapter.path for chapter in manifest.chapters)}
    html_files.update(name for name in files if name.endswith((".html", ".svg")))
    for relative in sorted(html_files):
        parser = _PackageHTML()
        parser.feed(files[relative].decode("utf-8"))
        parser.close()
        if not relative.endswith(".svg") and (
            not parser.doctype or not {"html", "head", "body"} <= parser.tags
        ):
            parser.errors.append("missing HTML markers")
        if parser.errors:
            raise ValueError(f"{relative}: " + "; ".join(parser.errors))
        refs = list(parser.references)
        for css in parser.styles:
            refs.extend(
                (value, True, imported) for value, imported in _css_references(css)
            )
        dependencies[relative] = set()
        for value, embedded, stylesheet in refs:
            if embedded:
                asset = _asset_dependency(files, relative, value, protected)
                if asset:
                    dependencies[relative].add(asset)
                    if stylesheet:
                        css_files.add(asset)
            else:
                _local_reference(relative, value)
    visited_css = set()
    while css_files:
        relative = css_files.pop()
        if relative in visited_css:
            continue
        visited_css.add(relative)
        dependencies.setdefault(relative, set())
        for value, imported in _css_references(files[relative].decode("utf-8")):
            asset = _asset_dependency(files, relative, value, protected)
            if asset:
                dependencies[relative].add(asset)
                if imported:
                    css_files.add(asset)
    return manifest, dependencies


def _fingerprint(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative, content in sorted(files.items()):
        path = relative.encode("utf-8")
        # Length framing keeps different path/byte partitions unambiguous.
        digest.update(len(path).to_bytes(8, "big"))
        digest.update(path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _zip_ready(files: dict[str, bytes], manifest: CourseManifest) -> bool:
    payload = files.get("downloads/course.zip")
    if not payload:
        return False
    required = {
        "manifest.json",
        "index.html",
        manifest.source_manifest,
        manifest.license_file,
    }
    required.update(chapter.path for chapter in manifest.chapters)
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = set()
            seen = set()
            for member in archive.infolist():
                # ZipInfo normalizes Windows separators; inspect the original name.
                name = member.orig_filename.rstrip("/")
                if _safe_relative_path(name) != name or name in seen:
                    return False
                seen.add(name)
                mode = stat.S_IFMT(member.external_attr >> 16)
                if mode not in {0, stat.S_IFREG, stat.S_IFDIR} or member.flag_bits & 1:
                    return False
                if member.is_dir():
                    continue
                if mode == stat.S_IFDIR:
                    return False
                names.add(name)
            return required <= names and archive.testzip() is None
    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        return False


def _inspect_snapshot(files: dict[str, bytes]) -> PackageInspection:
    manifest, dependencies = _validate_snapshot(files)
    preview: set[str] = set()
    pending = [chapter.path for chapter in manifest.chapters if chapter.free_preview]
    visited = set()
    while pending:
        source = pending.pop()
        if source in visited:
            continue
        visited.add(source)
        for asset in dependencies.get(source, ()):
            preview.add(asset)
            pending.append(asset)
    pdf = files.get("downloads/course.pdf", b"")
    protected = {
        "manifest.json",
        "index.html",
        manifest.source_manifest,
        manifest.license_file,
    }
    protected.update(chapter.path for chapter in manifest.chapters)
    return PackageInspection(
        course_id=manifest.course_id,
        slug=manifest.slug,
        version=manifest.version,
        fingerprint=_fingerprint(files),
        preview_assets=frozenset(preview),
        all_assets=frozenset(
            name
            for name in files
            if name.startswith("assets/") and name not in protected
        ),
        pdf_ready=bool(re.match(rb"%PDF-[0-9]+\.[0-9]+", pdf)),
        zip_ready=_zip_ready(files, manifest),
    )


def inspect_package(root: Path) -> PackageInspection:
    try:
        root = _package_root(root)
        files, inventory = _read_snapshot(root)
        result = _inspect_snapshot(files)
        _confirm_snapshot(root, files, inventory)
        return result
    except (ValueError, OSError, RecursionError) as exc:
        raise _unavailable() from exc


def read_verified_file(
    root: Path, relative_path: str, expected_fingerprint: str | None
) -> bytes:
    """Return the exact snapshot bytes checked for safety and optional identity."""
    try:
        root = _package_root(root)
        relative = _safe_relative_path(relative_path)
        files, inventory = _read_snapshot(root)
        result = _inspect_snapshot(files)
        if expected_fingerprint and result.fingerprint != expected_fingerprint:
            raise ValueError("package fingerprint changed")
        if relative not in files:
            raise ValueError("file is missing")
        _confirm_snapshot(root, files, inventory)
        return files[relative]
    except (ValueError, OSError, RecursionError) as exc:
        raise _unavailable() from exc

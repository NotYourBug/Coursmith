"""Pure structure/safety checks of a frozen draft; never sales approval."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .models import ProductionInput
from .rendering import Finding, MAX_CHAPTER_BYTES, _markdown_parser, render_markdown


@dataclass(frozen=True)
class DraftCheck:
    input_hash: str
    findings: tuple[Finding, ...]
    blocking: bool
    human_review_required: bool = True

    def __post_init__(self) -> None:
        # A structural result can never disable the separate human review.
        object.__setattr__(self, "human_review_required", True)


_SECTIONS = (
    ("objectives", {"学习目标", "目标"}),
    ("explanation", {"讲解"}),
    ("example", {"案例", "示例"}),
    ("exercise", {"练习"}),
    ("summary", {"总结", "小结"}),
)


def validate_chapter(markdown: str, *, chapter_id: str) -> tuple[Finding, ...]:
    try:
        size = len(markdown.encode("utf-8"))
    except UnicodeEncodeError:
        return (Finding("invalid_body_utf8", chapter_id, True),)
    if size > MAX_CHAPTER_BYTES:
        return (Finding("chapter_too_large", chapter_id, True),)
    if not markdown.strip():
        return (Finding("missing_body", chapter_id, True),)
    tokens = _markdown_parser().parse(markdown)
    present: set[str] = set()
    for index, token in enumerate(tokens):
        # Quoted/list-embedded heading examples are not course sections.
        if token.type != "heading_open" or token.level != 0:
            continue
        heading = tokens[index + 1]
        children = heading.children or []
        if any(child.type not in {"text", "strong_open", "strong_close", "em_open", "em_close"}
               for child in children):
            continue
        title = ''.join(child.content for child in children if child.type == "text").strip()
        section = next((key for key, aliases in _SECTIONS if title in aliases), None)
        if section is None:
            continue
        # Require some body under that section. This is presence checking,
        # not an assertion about quality, correctness, or sufficient depth.
        for body_index in range(index + 3, len(tokens)):
            body = tokens[body_index]
            if body.type == "heading_open" and body.level == 0 and int(body.tag[1:]) <= int(token.tag[1:]):
                break
            if body.type == "inline" and tokens[body_index - 1].type == "heading_open":
                continue
            if body.type in {"inline", "fence", "code_block"} and body.content.strip():
                present.add(section)
                break
    findings = [Finding("missing_" + section, chapter_id, True)
                for section, _ in _SECTIONS if section not in present]
    findings.extend(replace(finding, chapter_id=chapter_id)
                    for finding in render_markdown(markdown).findings)
    return tuple(findings)


def validate_draft(input: ProductionInput) -> DraftCheck:
    """Check frozen content only; pending decisions/jobs are Task 11's gate."""
    findings: list[Finding] = []
    outline = input.outline
    if outline is None or not outline.items:
        findings.append(Finding("missing_outline", None, True))
    if outline is not None and len(outline.items) > 60:
        findings.append(Finding("outline_too_large", None, True))
    if outline is not None and outline.project_id != input.project_id:
        findings.append(Finding("outline_project_mismatch", None, True))
    if input.mode == "precise" and (outline is None or not outline.confirmed):
        findings.append(Finding("outline_unconfirmed", None, True))
    expected: set[str] = set()
    for item in outline.items if outline is not None else ():
        chapter_id = item.chapter_id
        if not chapter_id or not chapter_id.strip():
            findings.append(Finding("invalid_outline_chapter", None, True))
        elif chapter_id in expected:
            findings.append(Finding("duplicate_outline_chapter", chapter_id, True))
        else:
            expected.add(chapter_id)
    actual: set[str] = set()
    for chapter in input.chapters:
        if chapter.id in actual:
            findings.append(Finding("duplicate_chapter", chapter.id, True))
        actual.add(chapter.id)
        if chapter.id not in expected:
            findings.append(Finding("unexpected_chapter", chapter.id, True))
        if chapter.body_revision is None:
            findings.append(Finding("missing_body", chapter.id, True))
        else:
            findings.extend(validate_chapter(chapter.markdown, chapter_id=chapter.id))
    for chapter_id in sorted(expected - actual):
        findings.append(Finding("missing_body", chapter_id, True))
    return DraftCheck(input.input_hash, tuple(findings), any(f.blocking for f in findings))

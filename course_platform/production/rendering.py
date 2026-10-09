"""Render untrusted draft Markdown as inert, allowlisted HTML fragments."""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import unicodedata
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token
import nh3


MAX_CHAPTER_BYTES = 200 * 1024


@dataclass(frozen=True)
class Finding:
    """Privacy-safe diagnostic: never include source text or a rejected URL."""

    code: str
    chapter_id: str | None
    blocking: bool


@dataclass(frozen=True)
class RenderedChapter:
    html: str
    findings: tuple[Finding, ...]


def _markdown_parser() -> MarkdownIt:
    parser = MarkdownIt("commonmark", {"html": False}).enable("table")
    # Parse all syntactically valid destinations so our stricter policy can
    # reject them with a safe code. No token is rendered before that check.
    parser.validateLink = lambda value: True
    normalize = parser.normalizeLink

    def normalize_destination(value: str) -> str:
        # IDNA/URL normalization may discard hostname controls. Preserve an
        # unsafe original for the token-level rejection and diagnostic.
        return normalize(value) if _safe_http_url(value) else value

    parser.normalizeLink = normalize_destination
    return parser


def _safe_http_url(value: str) -> bool:
    # Check the authority that the original href actually uses BEFORE decoding
    # can move an encoded /, ? or # (and following credentials) into the path.
    try:
        original = urlsplit(value)
        if original.scheme.lower() not in {"http", "https"} or not original.hostname:
            return False
        if original.username is not None or original.password is not None:
            return False
        if any(char in original.netloc for char in "%/?#"):
            return False
        original.port
    except ValueError:
        return False
    decoded = unquote(unescape(value))
    if any(unicodedata.category(char).startswith("C") or char.isspace()
           for char in decoded) or "\\" in decoded:
        return False
    try:
        parsed = urlsplit(decoded)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        # Browsers normalize malformed authorities differently. Require an
        # unambiguous host, rather than passing encoded delimiters through.
        if any(char in parsed.netloc for char in "%/?#"):
            return False
        parsed.port  # Reject malformed/out-of-range ports.
    except ValueError:
        return False
    return True


def _protect_tokens(tokens: list[Token], findings: list[Finding]) -> list[Token]:
    protected = []
    links: list[bool] = []
    for token in tokens:
        if token.type == "image":
            findings.append(Finding("image_placeholder", None, False))
            token.type, token.tag, token.nesting = "text", "", 0
            token.content = "[图片：待补素材]"
            token.attrs, token.children = {}, None
        elif token.type == "link_open":
            safe = _safe_http_url(token.attrGet("href") or "")
            links.append(safe)
            if not safe:
                findings.append(Finding("unsafe_link", None, True))
                token.type, token.tag, token.nesting = "text", "", 0
                token.content, token.attrs = "", {}
        elif token.type == "link_close":
            safe = links.pop()
            if safe:
                protected.append(token)
                indication = Token("text", "", 0)
                indication.content = "（外部资料）"
                protected.append(indication)
                continue
            token.type, token.tag, token.nesting = "text", "", 0
            token.content, token.attrs = "", {}
        if token.children:
            token.children = _protect_tokens(token.children, findings)
        protected.append(token)
    return protected


def _filter_attribute(tag: str, attribute: str, value: str) -> str | None:
    if tag == "a" and attribute == "href" and not _safe_http_url(value):
        return None
    return value


def render_markdown(markdown: str) -> RenderedChapter:
    """No HTML trust, resource fetching, execution, or database operations."""
    try:
        size = len(markdown.encode("utf-8"))
    except UnicodeEncodeError:
        return RenderedChapter("", (Finding("invalid_body_utf8", None, True),))
    if size > MAX_CHAPTER_BYTES:
        return RenderedChapter("", (Finding("chapter_too_large", None, True),))
    parser = _markdown_parser()
    findings: list[Finding] = []
    tokens = _protect_tokens(parser.parse(markdown), findings)
    rendered = parser.renderer.render(tokens, parser.options, {})
    sanitized = nh3.clean(
        rendered,
        tags={"p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "em", "strong",
              "blockquote", "ul", "ol", "li", "pre", "code", "table", "thead", "tbody",
              "tr", "th", "td", "a"},
        attributes={"a": {"href", "title"}, "ol": {"start"}},
        attribute_filter=_filter_attribute,
        url_schemes={"http", "https"},
        url_relative="deny",
        link_rel="noopener noreferrer",
        strip_comments=True,
    )
    return RenderedChapter(sanitized, tuple(findings))

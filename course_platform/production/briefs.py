"""Unicode source validation, without normalizing or truncating private text."""

from __future__ import annotations


def source_text(value: str) -> str:
    # SQLite length() stops at NUL; lone surrogates cannot be stored as UTF-8.
    if "\x00" in value:
        raise ValueError("Source text contains an unsupported character.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Source text must be valid Unicode.") from None
    return value


def title_text(value: str) -> str:
    source_text(value)
    if not value.strip():
        raise ValueError("A nonblank title is required.")
    return value


def brief_text_total(title: str, audience: str, prerequisites: str, goal: str, difficulty: str) -> None:
    if sum(map(len, (title, audience, prerequisites, goal, difficulty))) > 10000:
        raise ValueError("Combined requirement text exceeds the limit.")

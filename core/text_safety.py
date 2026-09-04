"""Helpers for safely rendering external metadata in Markdown-like output."""

from __future__ import annotations

import re
from typing import Any, Type


_MARKDOWN_META_CHARS = re.compile(r"([\\`*_{}\[\]()<>#!|~$])")
_CONTROL_WHITESPACE = re.compile(r"[\r\n\t]+")


def escape_markdown_metadata(value: Any) -> str:
    """Escape untrusted metadata before embedding it in Markdown-like text."""
    text = "" if value is None else str(value)
    text = _CONTROL_WHITESPACE.sub(" ", text)
    return _MARKDOWN_META_CHARS.sub(r"\\\1", text)


class MarkdownSafeMetadata(str):
    """A raw string that escapes itself only when formatted for display.

    ``str(value)`` intentionally stays raw so filename/path sanitizers and
    structured data keep the original metadata.  ``f"{value}"`` goes through
    ``__format__`` and therefore receives Markdown escaping at presentation
    boundaries such as user-facing messages and transcript headers.
    """

    def __format__(self, format_spec: str) -> str:
        raw = super().__format__(format_spec)
        return escape_markdown_metadata(raw)


def install_video_info_title_safety(video_info_type: Type[Any]) -> None:
    """Ensure future ``VideoInfo.title`` assignments use display-safe strings."""
    if getattr(video_info_type, "_metadata_title_safety_installed", False):
        return

    original_setattr = video_info_type.__setattr__

    def _safe_setattr(instance: Any, name: str, value: Any) -> None:
        if name == "title" and isinstance(value, str) and not isinstance(value, MarkdownSafeMetadata):
            value = MarkdownSafeMetadata(value)
        original_setattr(instance, name, value)

    video_info_type.__setattr__ = _safe_setattr
    video_info_type._metadata_title_safety_installed = True

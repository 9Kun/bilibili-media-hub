"""Helpers for safely rendering external metadata in Markdown-like output."""

from __future__ import annotations

import re
from typing import Any


_MARKDOWN_META_CHARS = re.compile(r"([\\`*_{}\[\]()<>#!|~$])")
_CONTROL_WHITESPACE = re.compile(r"[\r\n\t]+")


def escape_markdown_metadata(value: Any) -> str:
    """Escape untrusted metadata before embedding it in Markdown-like text.

    Video metadata may contain Markdown links, inline HTML/tag-looking text, or
    control whitespace. Keep the original value untouched for filenames and
    structured data, and only escape it at the presentation boundary.
    """
    text = "" if value is None else str(value)
    text = _CONTROL_WHITESPACE.sub(" ", text)
    return _MARKDOWN_META_CHARS.sub(r"\\\1", text)

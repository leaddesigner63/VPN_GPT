"""Utilities for removing forbidden phrases from outgoing messages."""
from __future__ import annotations

import re
from typing import Final

# Match different spellings of "geoblocking" in Russian and English.
# The pattern tolerates optional spaces or hyphens between the parts and
# keeps additional suffixes (e.g. "геоблокировок", "geo-blocking").
_GEOBLOCK_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)гео[\s\-]*блок\w*"),
    re.compile(r"(?i)geo[\s\-]*block\w*"),
)

_REPLACEMENT: Final[str] = "[скрыто]"


def contains_geoblocking(text: str) -> bool:
    """Return ``True`` if the text references geoblocking."""

    return any(pattern.search(text) for pattern in _GEOBLOCK_PATTERNS)


def sanitize_text(text: str) -> str:
    """Replace every geoblocking mention with a neutral placeholder."""

    sanitized = text
    for pattern in _GEOBLOCK_PATTERNS:
        sanitized = pattern.sub(_REPLACEMENT, sanitized)
    return sanitized


def assert_no_geoblocking(text: str) -> None:
    """Raise ``ValueError`` if the text still references geoblocking."""

    if contains_geoblocking(text):
        raise ValueError("Outgoing message contains a forbidden geoblocking term")

from __future__ import annotations

from typing import Iterable, Mapping, Any


def _normalise_username(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.startswith("@"):
        text = text[1:]
    return text.lower()


def should_block_issue(users: Iterable[Mapping[str, Any]], username: str, limit: int) -> bool:
    """Return ``True`` if issuing a new key would exceed the configured limit.

    The check counts only active users and ignores the current user so they can
    получить новый ключ, не увеличивая общее количество.  ``limit`` less than or
    equal to zero is treated as "no limit".
    """

    if limit <= 0:
        return False

    active_users = [user for user in users if bool(user.get("active"))]
    if not active_users:
        return False

    normalized_username = _normalise_username(username)
    has_current = any(
        _normalise_username(user.get("username")) == normalized_username for user in active_users
    )

    return len(active_users) >= limit and not has_current


__all__ = ["should_block_issue"]

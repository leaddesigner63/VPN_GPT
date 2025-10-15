from __future__ import annotations

from fastapi import Header, HTTPException, Query, status

from api import config
from api.utils.logging import get_logger

logger = get_logger("endpoints.security")


def require_service_token(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    x_admin_query: str | None = Query(default=None, alias="x-admin-token"),
    x_internal_query: str | None = Query(default=None, alias="x-internal-token"),
) -> None:
    valid_tokens = {token for token in (config.ADMIN_TOKEN, config.INTERNAL_TOKEN) if token}
    if not valid_tokens:
        logger.error("Service tokens are not configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="service_token_not_configured",
        )

    presented: list[str] = []
    if authorization:
        try:
            scheme, token = authorization.split(" ", 1)
        except ValueError:
            token = ""
            scheme = ""
        if scheme.lower() == "bearer" and token.strip():
            presented.append(token.strip())
    for candidate in (x_admin_token, x_internal_token, x_admin_query, x_internal_query):
        if candidate:
            presented.append(candidate.strip())

    for token in presented:
        if token in valid_tokens:
            return

    logger.warning("Unauthorized service request", extra={"presented": bool(presented)})
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


__all__ = ["require_service_token"]

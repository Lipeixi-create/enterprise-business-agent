from __future__ import annotations

from fastapi import HTTPException, Request

from ..security import AuthenticationError, Principal, authenticate


def principal_from_request(request: Request) -> Principal:
    settings = request.app.state.settings
    try:
        principal = authenticate(settings, request.headers.get("X-API-Key"))
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "unauthorized",
                "message": str(exc),
                "stage": "authentication",
                "detail": {},
            },
            headers={"WWW-Authenticate": "ApiKey"},
        ) from exc
    # Middleware and security audit use this after the request completes.
    request.state.principal = principal
    return principal

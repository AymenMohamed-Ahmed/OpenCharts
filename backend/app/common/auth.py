"""Bearer-token check shared by the gateway and the execution service."""

import hmac

from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import SecretStr


def require_bearer(credentials: HTTPAuthorizationCredentials | None, expected: SecretStr) -> None:
    supplied = credentials.credentials if credentials else ""
    if not hmac.compare_digest(supplied.encode(), expected.get_secret_value().encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

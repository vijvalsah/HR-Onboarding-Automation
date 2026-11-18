from typing import Annotated, Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.core.config import settings

security = HTTPBearer(auto_error=True)


def get_token_payload(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
) -> dict[str, Any]:
    try:
        return jwt.decode(
            credentials.credentials,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        ) from exc


def require_roles(*allowed: str):
    def _inner(
        payload: Annotated[dict[str, Any], Depends(get_token_payload)],
    ) -> dict[str, Any]:
        role = payload.get("role")
        if role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions for this resource",
            )
        return payload

    return _inner


candidate_user = require_roles("candidate")
hr_user = require_roles("hr")
admin_user = require_roles("admin")
interviewer_user = require_roles("interviewer")
# HR and admin can both manage recruiting.
recruiting_user = require_roles("hr", "admin")

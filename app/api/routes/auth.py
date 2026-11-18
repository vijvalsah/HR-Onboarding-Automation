from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from jose import jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import verify_password
from app.db.models import User
from app.db.session import get_db

router = APIRouter()


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


def _create_access_token(*, subject: str, email: str, role: str) -> str:
    expire = datetime.now(UTC) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": subject,
        "email": email,
        "role": role,
        "exp": int(expire.timestamp()),
    }
    return jwt.encode(
        payload,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


@router.post("/auth/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    email = body.email.lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    # Return the same 401 whether the user is unknown or the password is wrong,
    # so we do not reveal which emails are registered.
    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    token = _create_access_token(
        subject=user.id,
        email=user.email,
        role=user.role,
    )
    return TokenResponse(access_token=token)

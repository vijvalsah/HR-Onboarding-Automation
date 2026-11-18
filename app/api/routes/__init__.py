from fastapi import APIRouter

from app.api.routes import (
    admin,
    auth,
    candidate,
    chat,
    health,
    hr,
    interviewer,
    recruiting,
)

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, tags=["auth"])
api_router.include_router(candidate.router, tags=["candidate"])
api_router.include_router(hr.router, tags=["hr"])
api_router.include_router(admin.router, tags=["admin"])
api_router.include_router(chat.router, tags=["chat"])
api_router.include_router(recruiting.router, tags=["recruiting"])
api_router.include_router(interviewer.router, tags=["interviewer"])

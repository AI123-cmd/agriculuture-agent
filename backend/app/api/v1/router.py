"""聚合注册所有 v1 子路由。"""
from fastapi import APIRouter

from .ai import router as ai_router
from .auth import router as auth_router
from .broadcast import router as broadcast_router
from .environment import router as environment_router
from .farmer import router as farmer_router
from .location import router as location_router
from .user import router as user_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(farmer_router)
api_router.include_router(location_router)
api_router.include_router(broadcast_router)
api_router.include_router(environment_router)
api_router.include_router(ai_router)
api_router.include_router(user_router)
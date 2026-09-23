"""业务服务层入口，暴露各服务单例。"""
from functools import lru_cache

from .auth_service import AuthService
from .farmer_service import FarmerService
from .location_service import LocationService
from .broadcast_service import BroadcastService
from .environment_service import EnvironmentService
from .ai_service import AIService
from .memory_service import MemoryService
from .user_service import UserService
from .profile_service import ProfileService


@lru_cache
def get_auth_service() -> AuthService:
    return AuthService()


@lru_cache
def get_farmer_service() -> FarmerService:
    return FarmerService()


@lru_cache
def get_location_service() -> LocationService:
    return LocationService()


@lru_cache
def get_broadcast_service() -> BroadcastService:
    return BroadcastService()


@lru_cache
def get_environment_service() -> EnvironmentService:
    return EnvironmentService()


@lru_cache
def get_ai_service() -> AIService:
    return AIService()


@lru_cache
def get_memory_service() -> MemoryService:
    return MemoryService()


@lru_cache
def get_user_service() -> UserService:
    return UserService()


@lru_cache
def get_profile_service() -> ProfileService:
    return ProfileService()


__all__ = [
    "get_auth_service",
    "get_farmer_service",
    "get_location_service",
    "get_broadcast_service",
    "get_environment_service",
    "get_ai_service",
    "get_memory_service",
    "get_user_service",
    "get_profile_service",
]
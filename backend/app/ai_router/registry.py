"""作物模型注册表。"""

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.ai_router.base import CropModel

_registry: dict[str, "CropModel"] = {}


def register(name: str) -> Callable[[type["CropModel"]], type["CropModel"]]:
    def deco(cls: type["CropModel"]) -> type["CropModel"]:
        _registry[name] = cls()
        return cls

    return deco


def resolve(crop_name: str | None) -> "CropModel":
    """按作物名称路由到垂类模型，未命中回落到通用农业模型。"""
    if crop_name:
        # 精确命中
        if crop_name in _registry:
            return _registry[crop_name]
        # 模糊命中（名称互相包含）
        for key, model in _registry.items():
            if key and (key in crop_name or crop_name in key):
                return model

    # 通用农业兜底
    if "通用农业" in _registry:
        return _registry["通用农业"]
    return next(iter(_registry.values()))


def all_models() -> dict[str, "CropModel"]:
    return dict(_registry)

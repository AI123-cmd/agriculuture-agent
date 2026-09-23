"""对象存储抽象 + 本地实现（可替换为 OSS/COS）。"""

import os
import uuid
from abc import ABC, abstractmethod

from app.core.config import get_settings


class Storage(ABC):
    @abstractmethod
    def save(self, filename: str, data: bytes) -> str:
        """保存文件并返回可访问 URL。"""


class LocalStorage(Storage):
    def __init__(self, root: str):
        self.root = root
        os.makedirs(self.root, exist_ok=True)

    def save(self, filename: str, data: bytes) -> str:
        ext = os.path.splitext(filename)[1] or ".jpg"
        key = f"{uuid.uuid4().hex}{ext}"
        path = os.path.join(self.root, key)
        with open(path, "wb") as f:
            f.write(data)
        return f"/static/{key}"


def get_storage() -> Storage:
    settings = get_settings()
    if settings.STORAGE_TYPE == "local":
        return LocalStorage(settings.STORAGE_LOCAL_DIR)
    # 其它类型（oss/cos）预留：新增实现类并在此返回
    raise NotImplementedError(f"暂不支持的存储类型：{settings.STORAGE_TYPE}")

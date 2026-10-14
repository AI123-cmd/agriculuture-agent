"""对象存储抽象 + 本地实现（可替换为 OSS/COS）。"""

import hashlib
import os
from abc import ABC, abstractmethod

from app.core.config import get_settings

# 扩展名由真实 MIME 决定，绝不取自用户文件名
_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

# 两级目录的前缀长度：单目录堆几十万文件时部分文件系统会退化，分目录是廉价保险
_SHARD_LEN = 2


class Storage(ABC):
    @abstractmethod
    def save_image(self, data: bytes, mime: str) -> str:
        """保存图片并返回可访问 URL。`mime` 必须是校验通过的真实类型。"""

    @abstractmethod
    def read_image(self, key: str) -> bytes | None:
        """按存储键读回图片字节；键不存在或非法时返回 `None`（不抛异常）。

        `key` 是 `save_image` 返回值去掉 `/static/` 前缀后的相对路径（如 `ab/ab12….png`）。
        调用方（图片识别）要拿字节交给模型，故需要读取能力。oss/cos 实现留待接入时补齐。
        """


class LocalStorage(Storage):
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    def save_image(self, data: bytes, mime: str) -> str:
        """按内容哈希命名，天然去重且不复用用户文件名。

        `os.replace` 而非直接 `open(path, "wb")`：后者在写入过程中进程被杀会留下
        一个被截断却仍可访问的文件，而 `os.replace` 是同目录内的原子替换。
        """
        digest = hashlib.sha256(data).hexdigest()
        key = f"{digest[:_SHARD_LEN]}/{digest}{_EXT.get(mime, '.jpg')}"
        path = os.path.join(self.root, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            tmp = f"{path}.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        return f"/static/{key}"

    def read_image(self, key: str) -> bytes | None:
        """读回图片字节。路径越界一律返回 `None`，不抛异常。

        `os.path.realpath` 之后必须仍在 `root` 之内：`key` 可能来自 `ChatRequest.imageUrl`，
        是外部输入；不设这道闸，`../../etc/passwd` 这类键就能读到存储目录之外的文件。
        """
        path = os.path.realpath(os.path.join(self.root, key))
        if path != self.root and not path.startswith(self.root + os.sep):
            return None
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return f.read()


def get_storage() -> Storage:
    settings = get_settings()
    if settings.STORAGE_TYPE == "local":
        return LocalStorage(settings.STORAGE_LOCAL_DIR)
    # 其它类型（oss/cos）预留：新增实现类并在此返回
    raise NotImplementedError(f"暂不支持的存储类型：{settings.STORAGE_TYPE}")


__all__ = ["LocalStorage", "Storage", "get_storage"]

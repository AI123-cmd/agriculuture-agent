"""上传校验纯函数：不依赖框架与存储，便于单测。

拆成纯函数而非服务方法，是因为校验规则要被多处复用（当前是头像上传，第二步的
`/api/v1/ai/chat/upload` 问诊图片上传会复用同一套），而纯函数没有会话与 IO 依赖，
可以直接用字节串断言，不必起应用。
"""

from dataclasses import dataclass

# 魔数表：文件头 → MIME。扩展名可伪造，魔数不可（除非刻意构造文件）。
MAGIC: dict[bytes, str] = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"RIFF": "image/webp",  # WebP 需进一步校验偏移 8 处的 WEBP
}

# WebP 的完整判定：`RIFF` 之后 4 字节是长度，偏移 8 起才是 `WEBP` 标识
_WEBP_TAG = b"WEBP"
_WEBP_TAG_OFFSET = 8
# 读取前 16 字节即可判定上表全部格式（WebP 用到偏移 8-11）
_HEAD_LEN = 16


@dataclass(frozen=True)
class ImageCheck:
    """校验结果。`reason` 直接面向农户，故用中文且不泄露文件系统细节。"""

    ok: bool
    mime: str | None
    reason: str | None


def sniff_mime(head: bytes) -> str | None:
    """按文件头判定 MIME；识别不出返回 None。"""
    for magic, mime in MAGIC.items():
        if head.startswith(magic):
            if mime == "image/webp" and head[_WEBP_TAG_OFFSET : _WEBP_TAG_OFFSET + 4] != _WEBP_TAG:
                continue
            return mime
    return None


def validate_image(
    filename: str,
    content_type: str | None,
    data: bytes,
    *,
    max_bytes: int,
    allowed: set[str],
) -> ImageCheck:
    """三重校验：大小 → 真实魔数 → 与声明类型一致。

    顺序不可调换：大小校验只读 `len()`，是最便宜的一步，放在最前面才能更早拒掉
    超大文件（若先读头部再判大小，攻击者用 50MB 文件就能逼服务端持续分配内存）。
    真实魔数排在声明类型之前：声明类型完全由客户端伪造，先信它等于没校验。

    `filename` 保留在签名里但只用于日志——现有实现用 `os.path.splitext(filename)`
    决定扩展名，那是路径与类型污染的入口（`x.php` 会得到 `.php` 后缀）。扩展名现在
    由 `sniff_mime` 的真实结果决定，调用方传它只为在日志里留下线索。
    """
    if len(data) == 0:
        return ImageCheck(False, None, "文件为空")
    if len(data) > max_bytes:
        # 用一位小数而非整数除法：上限小于 1MB 时 `// 1024 // 1024` 会显示"0MB"
        return ImageCheck(False, None, f"文件超过上限 {max_bytes / 1024 / 1024:.1f}MB")

    real = sniff_mime(data[:_HEAD_LEN])
    if real is None:
        return ImageCheck(False, None, "文件头不是支持的图片格式")
    if real not in allowed:
        return ImageCheck(False, None, f"不支持的图片类型：{real}")
    # 声明与真实必须一致：只查魔数挡不住"PNG 头 + 可执行体"，只查声明更挡不住
    if content_type and content_type != real:
        return ImageCheck(False, None, f"声明类型 {content_type} 与真实类型 {real} 不一致")
    return ImageCheck(True, real, None)


__all__ = ["MAGIC", "ImageCheck", "sniff_mime", "validate_image"]

"""设备密钥工具：生成、加盐摘要、常量时间校验（T5-03）。

设备密钥在绑定时下发，之后平台无法再取回明文——落库的只有引用名 `secret_ref`（供审计与轮换
定位）与加盐摘要 `secret_digest`（供比对）。这是与 T1-07 令牌处理一致的取舍：可重复取回的秘密
等于没有秘密。

密钥生成用 32 字节随机而非可读口令：设备侧固件通常不会显示密钥，长度与熵比可读性更重要
（调研报告 11.1 指出"设备端是简单固件、无法安全保存密钥材料"是核心矛盾，平台侧不能因此放松）。

校验一律用 `secrets.compare_digest`，不用 `==`：上报入口是对外暴露的匿名入口（只凭 SN 与密钥），
`==` 的短路比较会泄露密钥前缀信息，足够多的请求可以逐字节猜出密钥。
"""

import base64
import hashlib
import secrets
from uuid import uuid4

from app.core.config import get_settings
from app.models import DeviceConfig

# 生成 32 字节随机密钥；base64url 定长 43 字符（去掉"="填充）。
_SECRET_BYTES = 32


def generate_device_secret() -> str:
    """生成 32 字节随机密钥，base64url 编码，无填充。

    用 base64url 而不是 hex：同样 32 字节熵，hex 是 64 字符、base64url 是 43 字符，农户抄写与
    固件存储都更省。去掉 "=" 填充因为它对解码无信息、却容易在复制粘贴时丢失或被误当成噪声。
    """
    return base64.urlsafe_b64encode(secrets.token_bytes(_SECRET_BYTES)).rstrip(b"=").decode()


def hash_device_secret(secret: str) -> tuple[str, str]:
    """返回 `(secret_ref, digest)`。

    `digest` 是 `sha256(salt + secret)` 的十六进制（64 字符），固定的库列宽。加盐而非裸哈希：
    密钥空间即便很大，盐值也能让"同一密钥在不同部署下得到不同摘要"，避免跨环境比对。

    `secret_ref` 是引用的可读名（`device_secret:<uuid4>`），与 `digest` 分工不同：ref 用于审计
    与轮换记录（"某设备在何时换了密钥"），digest 是比对材料。两者都不含明文。
    """
    digest = hashlib.sha256((get_settings().DEVICE_SECRET_SALT + secret).encode()).hexdigest()
    ref = f"device_secret:{uuid4()}"
    return ref, digest


def verify_device_secret(device: DeviceConfig, presented: str) -> bool:
    """常量时间比较，避免时序侧信道。

    `presented` 为空或设备无摘要一律 `False`：这两种情况下密钥不可能成立，且能省掉一次哈希。
    设备密钥校验与"设备存在"在调用方合并返回同一错误，使攻击者无法靠响应差异枚举合法 SN。
    """
    if not presented or not device.secret_digest:
        return False
    expected = hashlib.sha256((get_settings().DEVICE_SECRET_SALT + presented).encode()).hexdigest()
    return secrets.compare_digest(expected, device.secret_digest)


__all__ = ["generate_device_secret", "hash_device_secret", "verify_device_secret"]

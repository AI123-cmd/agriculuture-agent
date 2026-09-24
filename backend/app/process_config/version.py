"""流程配置的版本引用：把「这次会话用的是哪一版配置」变成一个可存储、可比对的值对象。

`version` 与 `content_hash` 二者都要存：`version` 是人工递增的语义版本号，用于对人
解释「这是第几版」；`content_hash` 是内容的客观指纹，用于判定「内容是否真的变了」。
若只比对 `version`，一次忘记改版本号的编辑会被热加载忽略，问题很难发现。
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.agent.state import ProcessId
from app.process_config.models import ProcessConfig

# 规范化 JSON 的哈希取前 12 位。12 位十六进制约 48 bit，对「同一流程内区分内容」
# 这个用途足够，且短到能塞进日志与指标标签里。
HASH_LENGTH = 12


def canonical_json(config: ProcessConfig) -> str:
    """把配置序列化为规范化 JSON：键排序、无多余空格、非 ASCII 不转义。

    规范化的目的是让哈希只依赖内容而不依赖字段顺序或对象身份。`sort_keys` 是必须的：
    dict 的插入顺序随构造方式变化，不排序会让同一份配置算出不同哈希。
    `ensure_ascii=False` 让中文术语在原始文本里可读（哈希值一样，但排查时能看懂）。
    """
    return json.dumps(_canonical_payload(config), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_payload(config: ProcessConfig) -> dict[str, Any]:
    """去掉 `version` 后的规范化载荷。

    **`version` 必须排除在内容指纹之外**，这不是风格取舍而是正确性要求：
    - `version` 是人工在 YAML 里手写的，`content_hash` 是内容本身的指纹。二者都比对
      才能覆盖两类编辑：改了内容但忘了改版本号（靠哈希发现），以及只改了版本号。
    - T2-07 的同步任务会按数据库序列自增 `config_version` 后再入库。若哈希含
      `version`，则「seeds 里 version=1、库里自增到 version=2」会被判为内容不同，
      每次同步都再插一版，幂等性直接失效。
    """
    payload = config.model_dump(mode="json")
    payload.pop("version", None)
    return payload


def compute_config_hash(config: ProcessConfig) -> str:
    """内容指纹：规范化 JSON 的 sha256 前 12 位。"""
    digest = hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()
    return digest[:HASH_LENGTH]


def compute_payload_hash(payload: dict[str, Any]) -> str:
    """对已落库的 `config_json` 直接算指纹，避免先还原成对象再算。

    落库形态与 seeds 形态可能因为 YAML 浮点/整型表示差异而不同，因此比对时两边
    都必须走同一条规范化路径。这里同样剔除 `version`，否则数据库侧（`config_version`
    已被同步任务自增）与 seeds 侧永远算不出同一个哈希，比对失去意义。
    """
    normalized = {k: v for k, v in payload.items() if k != "version"}
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:HASH_LENGTH]


class ConfigVersionRef(BaseModel):
    """一次会话在开始时刻固定的配置版本引用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    process_id: ProcessId
    version: int
    content_hash: str
    loaded_at: datetime

    def matches(self, other: "ConfigVersionRef") -> bool:
        """内容是否相同。

        只比 `process_id` 与 `content_hash`，刻意不比 `version`：版本号可能忘记递增，
        而内容哈希不会说谎。`loaded_at` 参与比较会让「同一份配置的不同加载时刻」
        被判为变化，从而触发无意义的热加载。
        """
        return self.process_id == other.process_id and self.content_hash == other.content_hash


def build_ref(config: ProcessConfig) -> ConfigVersionRef:
    """由配置对象构造版本引用。"""
    return ConfigVersionRef(
        process_id=config.process_id,
        version=config.version,
        content_hash=compute_config_hash(config),
        loaded_at=datetime.now(UTC),
    )


__all__ = [
    "HASH_LENGTH",
    "ConfigVersionRef",
    "build_ref",
    "canonical_json",
    "compute_config_hash",
    "compute_payload_hash",
]

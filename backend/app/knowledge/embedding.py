"""向量化接口与真实/mock 两个实现（T2-48）。

`Embedder` 做成协议而非具体类：构建器只依赖"能批量把文本转成定长向量"，真实环境走
HTTP 向量化服务，测试与 `USE_MOCK_LLM=true` 走 `MockEmbedder`，两者互不牵连。

`MockEmbedder` 必须**确定性**：同一文本每次返回同一向量、不同文本返回不同向量。若用
随机向量，"相似查询能召回对应条目"这类断言会随机失败。做法是以文本哈希为种子生成
单位向量，既保证确定性又保证不同文本近乎正交。
"""

import asyncio
import hashlib
import math
import random
from typing import Protocol

import httpx

# 向量维度与 `Settings.EMBEDDING_DIM` 及表列 `Vector(1024)` 必须一致，换模型时三处同改。
EMBEDDING_DIM = 1024

# 批量请求的重试参数。向量化服务偶发抖动不应让整轮构建失败，指数退避重试几次即可。
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5


class Embedder(Protocol):
    """向量化接口。真实实现走模型服务，mock 实现返回确定性伪向量。"""

    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HttpEmbedder:
    """真实向量化：批量请求，失败按指数退避重试。"""

    dimensions = EMBEDDING_DIM

    def __init__(self, url: str, api_key: str, timeout: float = 30.0) -> None:
        self._url = url
        self._api_key = api_key
        self._timeout = timeout

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化。空输入直接返回空列表，不发请求。"""
        if not texts:
            return []
        payload = {"input": texts, "model": "embedding", "dimensions": self.dimensions}
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        last_error: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(self._url, json=payload, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                return [list(map(float, row["embedding"])) for row in data["data"]]
            except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
                last_error = exc
                if attempt < _MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_BACKOFF_BASE * (2**attempt))
        raise RuntimeError(f"向量化服务调用失败（已重试 {_MAX_ATTEMPTS} 次）：{last_error}")


class MockEmbedder:
    """mock 向量化：由文本哈希种子生成确定性单位向量，保证同文同向量。"""

    dimensions = EMBEDDING_DIM

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._unit_vector(text) for text in texts]

    def _unit_vector(self, text: str) -> list[float]:
        """以 sha256(text) 派生随机种子，生成 L2 归一化向量（余弦距离可直接用）。"""
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        rng = random.Random(seed)
        raw = [rng.uniform(-1.0, 1.0) for _ in range(self.dimensions)]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return [v / norm for v in raw]


__all__ = ["EMBEDDING_DIM", "Embedder", "HttpEmbedder", "MockEmbedder"]

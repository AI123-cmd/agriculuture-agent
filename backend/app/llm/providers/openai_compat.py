"""OpenAI 兼容供应商适配器（T2-56）。

OpenAI 兼容端点（含多数国产大模型的兼容模式）支持原生结构化输出：
`response_format={"type": "json_schema", ...}`。这是三层保障的**第一层**——原生能力
由供应商保证输出是合法 JSON 且符合 Schema，能整条省掉"解析失败加修复重试"的路径。

本模块只负责请求构造与响应解析，不含任何降级决策：解析失败返回
`error="parse_error"`，是否重试由 `app/llm/structured.py` 决定，是否换候选由网关决定。
这样"哪一层补救"的分工在代码上清晰可查。

错误分类在适配器内完成（超时、429、5xx 可重试；400/401 不可重试），网关与熔断器
只读 `error` 字符串（技术方案 10.2 的错误分类表）。
"""

import time

import httpx
from pydantic import BaseModel

from app.llm.providers.base import (
    ModelResponse,
    build_error_response,
    elapsed_ms,
    parse_structured,
)

# 结构化输出场景固定 temperature=0.0：降低输出格式的随机性。它是"让模型少发挥"的
# 手段，不改变事实来源（事实由知识库提供，见技术方案 10.2）。
_TEMPERATURE = 0.0


class OpenAICompatProvider:
    """OpenAI 兼容端点：优先用原生结构化输出（response_format=json_schema）。"""

    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(
        self,
        model: str,
        messages: list[dict],
        response_schema: type[BaseModel] | None = None,
    ) -> dict:
        """构造请求体。`response_schema` 非空时注入原生结构化输出参数。"""
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": _TEMPERATURE,
            "stream": False,
        }
        if response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema.__name__,
                    "schema": response_schema.model_json_schema(),
                    "strict": True,
                },
            }
        return payload

    async def chat(
        self,
        model: str,
        messages: list[dict],
        response_schema: type[BaseModel] | None = None,
        timeout: float = 30.0,
    ) -> ModelResponse:
        payload = self._build_payload(model, messages, response_schema)
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException:
            return build_error_response(self.name, model, "timeout", elapsed_ms(started))
        except httpx.HTTPStatusError as exc:
            return build_error_response(self.name, model, f"http_{exc.response.status_code}", elapsed_ms(started))
        except httpx.HTTPError:
            return build_error_response(self.name, model, "connection_error", elapsed_ms(started))
        return self._parse_response(model, data, response_schema, started)

    def _parse_response(
        self,
        model: str,
        data: dict,
        response_schema: type[BaseModel] | None,
        started: float,
    ) -> ModelResponse:
        """从 OpenAI 兼容响应里取内容并解析。`choices[0].message.content` 是文本。"""
        choices = data.get("choices") or []
        message = (choices[0].get("message") if choices else {}) or {}
        content = message.get("content")
        usage = data.get("usage") or {}
        parsed, error = parse_structured(content, response_schema)
        return ModelResponse(
            content=content,
            parsed=parsed,
            model=data.get("model") or model,
            provider=self.name,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            latency_ms=elapsed_ms(started),
            is_fallback=False,
            error=error,
        )


__all__ = ["OpenAICompatProvider"]

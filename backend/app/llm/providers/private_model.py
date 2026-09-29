"""私有化部署模型适配器（T2-56）。

私有化部署的模型（如本地 Qwen 级模型）多数不支持 `response_format` 的 json_schema
模式，因此走**第二层**结构化保障：把 Schema 说明追加到 messages，再解析输出。请求体里
不出现 `response_format`，这是刻意的——对不支持该参数的端点发送它会被判 400，反而把
一次可用的调用打成失败。

`temperature=0.0` 与 OpenAI 兼容适配器一致：结构化输出场景要的是稳定复现，不是多样性。

私有化模型的定位是降级链的最后一跳：主供应商 A 与 B 都不可用时，它保证红线链路在
外部服务中断时仍可用（技术方案 10.3）。因此它的端点通常在内网、超时容忍度更高。
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

_TEMPERATURE = 0.0


def _schema_instruction(response_schema: type[BaseModel]) -> str:
    """把 Schema 转成给模型的自然语言指令（第二层保障的提示词约束）。"""
    return (
        "你必须只输出一个符合下面 JSON Schema 的 JSON 对象，"
        "不要输出任何解释、前后缀或 Markdown 代码块标记。\n"
        f"JSON Schema：{response_schema.model_json_schema()}"
    )


class PrivateModelProvider:
    """私有化部署模型：通常不支持原生结构化输出，走提示词约束路径。"""

    name = "private"

    def __init__(self, base_url: str, api_key: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _build_payload(
        self,
        model: str,
        messages: list[dict],
        response_schema: type[BaseModel] | None = None,
    ) -> dict:
        """构造请求体。`response_schema` 非空时把 Schema 说明追加进 messages。"""
        msgs = messages
        if response_schema is not None:
            msgs = [*messages, {"role": "system", "content": _schema_instruction(response_schema)}]
        return {"model": model, "messages": msgs, "temperature": _TEMPERATURE, "stream": False}

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
        """私有化端点响应形态不稳定，兼容 `answer` 与 `choices[0].message.content` 两种。"""
        content = data.get("answer")
        if content is None:
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


__all__ = ["PrivateModelProvider", "_schema_instruction"]

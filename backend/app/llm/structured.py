"""结构化输出的三层保障（T2-56，技术方案 10.2）。

三层依次是：**原生结构化输出**（适配器内部按能力启用，供应商保证 JSON 合法且合 Schema）
→ **提示词约束加解析**（把 Schema 说明追加进 messages，解析失败用 pydantic 的原始校验
错误构造一次修复提示重试一次）→ **该智能体的降级路径**（返回 `parsed is None`，由智能体
基类按任务类型分派到模板问题、不确定输出、转农技站或模板原文）。

关键原则：**解析失败绝不用正则硬抽**。农业场景下错误的字段值比没有字段值更危险——从
"共 2 条建议，间隔 7 天"里正则抽出的 `7` 可能被当成安全间隔期天数填进处方，农户会照着
打药。因此本模块的全部补救手段都是"让模型重出"（修复提示），重出仍失败就走降级，绝不
构造一个"看起来像"的值。

只重试一次：第二次仍失败说明该模型对这个 Schema 的遵循能力不足，继续重试只是浪费 token
与延迟。第三层的"降级"不在本模块实现，这里只把状态表达清楚（返回 `degrade` 标记）。
"""

from pydantic import BaseModel

from app.llm.providers.base import ModelProvider, ModelResponse

# 层标记。用于指标与日志区分"这次结构化输出走了哪层"。
LAYER_NATIVE = "native"
LAYER_REPAIR = "repair"
LAYER_DEGRADE = "degrade"


class StructuredOutputGuard:
    """三层保障的编排。无状态：可被网关或智能体基类按需构造。"""

    async def chat_with_structured(
        self,
        provider: ModelProvider,
        model: str,
        messages: list[dict],
        schema: type[BaseModel],
        timeout: float = 30.0,
    ) -> tuple[ModelResponse, str]:
        """三层保障的结构化调用。返回响应与采用的层标记。"""
        # 第一层：原生结构化输出。是否启用由适配器按自身能力决定。
        response = await provider.chat(model, messages, schema, timeout)
        if response.parsed is not None:
            return response, LAYER_NATIVE
        # 第二层：提示词约束加解析失败构造修复提示重试一次。
        repaired = await self._retry_with_repair(provider, model, messages, schema, response, timeout)
        if repaired.parsed is not None:
            return repaired, LAYER_REPAIR
        # 第三层：交由该智能体的降级路径（本模块不决定降级语义）。
        return repaired, LAYER_DEGRADE

    async def _retry_with_repair(
        self,
        provider: ModelProvider,
        model: str,
        messages: list[dict],
        schema: type[BaseModel],
        response: ModelResponse,
        timeout: float,
    ) -> ModelResponse:
        """用首次的失败信息构造修复提示，重试一次。只重试一次。"""
        repair_messages = self._build_repair_messages(messages, response)
        return await provider.chat(model, repair_messages, schema, timeout)

    def _build_repair_messages(self, messages: list[dict], response: ModelResponse) -> list[dict]:
        """把失败信息反馈给模型，让它自己修。

        携带原始文本与失败原因：前者让模型看清自己上一轮到底输出了什么，后者（解析
        错误或供应商错误）指出具体错在哪。只要求"仅输出符合 JSON Schema 的修正结果"，
        避免模型在修正时附加解释导致二次解析失败。
        """
        detail = response.error or "解析失败"
        previous = response.content or "（无输出）"
        return [
            *messages,
            {"role": "assistant", "content": previous},
            {
                "role": "user",
                "content": (
                    f"你的上一次输出不符合下面的要求：{detail}\n"
                    "请仅输出符合 JSON Schema 的修正结果，不要附加解释、前后缀或 Markdown 代码块标记。"
                ),
            },
        ]


__all__ = [
    "LAYER_DEGRADE",
    "LAYER_NATIVE",
    "LAYER_REPAIR",
    "StructuredOutputGuard",
]

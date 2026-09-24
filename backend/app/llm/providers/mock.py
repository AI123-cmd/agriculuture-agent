"""Mock 供应商（T2-56 的最小实现）。

`USE_MOCK_LLM=true` 是自动化测试的默认路径（全局约定第七节）：智能体的契约校验、
重试、降级与下游逻辑都要能在不依赖外部服务的前提下被完整跑通。因此 Mock 不是"随便
返回一段文本"，而是"按 `schema` 生成一个满足契约的对象"。

生成分两步：先按契约的 JSON Schema 递归填最小合法取值（必填字段填零值、`enum` 取第
一个取值、字符串给非空占位、数组按 `minItems` 给元素），再套一层按类名的定点修正。
第二步存在的原因是**跨字段约束无法从 Schema 推导**——契约里有一批"某字段为假时另一
字段必须非空"「枚举取某值时另一字段必须同值」的规则（`data_available=False` 必须有
`unavailable_reason`、未触发报告时 `trigger_source` 必须为 `none`、有数值就必须有
溯源），Schema 里看不到它们，只有契约自己知道。

修正表按**类名**索引而不是导入契约类：`app/llm/` 不得导入 `app.agent`（全局约定
L3），因此这里只能以字符串为键。表中任何一项失效（契约改名或约束变更）都会让 mock
返回 `parsed=None` 且 `error="parse_error"`，与真实的解析失败同路——调用方据此重试
一次再降级，不会静默产出一个不满足契约的对象。
"""

from typing import Any

from pydantic import BaseModel, ValidationError

from app.llm.providers.base import ModelResponse

# 按类名的定点修正。只登记"通用填充无法满足"的那些契约，其余契约靠通用规则即可。
_SCHEMA_OVERRIDES: dict[str, dict[str, Any]] = {
    # 一轮至少一问，单选类型必须给选项，且选项里必须含跳过词。
    "InterviewBatch": {
        "questions": [
            {
                "slot_key": "mock_slot",
                "text": "这是 mock 模式下的示例问题",
                "why": "mock 模式下用于跑通契约校验与下游逻辑",
                "options": ["示例选项", "说不清"],
                "answer_type": "single",
            }
        ],
        "stage_hint": "正在了解情况 1/1",
    },
    # basis 必填且逐条非空；取 preliminary 可避免触发"不确定时须说明来源"的分支。
    "DiagnosisResult": {
        "conclusion": "mock 模式下的示例结论",
        "confidence_level": "preliminary",
        "basis": ["mock 模式下的示例依据"],
        "terminology_profile": "mock",
    },
    # 数值字段留空则不需要溯源，避免"有数值但 facts_provenance 为空"的校验失败。
    "Prescription": {
        "diagnosis_summary": "mock 模式下的示例结论",
        "safety_interval_days": None,
        "max_uses_per_season": None,
        "pesticide_items": [],
        "facts_provenance": {},
    },
    # 未触发报告时 trigger_source 必须为 none、stop_consultation 必须为假。
    "ReportDecision": {
        "triggered": False,
        "matched_signals": [],
        "trigger_source": "none",
        "mandatory_actions": [],
        "contacts": [],
        "stop_consultation": False,
    },
}


def _deref(node: dict, defs: dict) -> dict:
    """解 `$ref`：pydantic 把嵌套模型与枚举放进 `$defs`。"""
    ref = node.get("$ref")
    if isinstance(ref, str):
        return defs.get(ref.rsplit("/", 1)[-1], {})
    return node


def _default_payload(node: dict, defs: dict) -> Any:
    """按 JSON Schema 递归生成最小合法取值。

    对象字段**全部填充**（不只是 `required`）：契约里不少跨字段校验是"某字段为假时
    另一字段必须非空"（如 `data_available=False` 时必须有 `unavailable_reason`），
    若可选字段一律省略，这些校验必然失败，mock 就退化成了"总是解析失败"。字符串给
    非空占位文本同理——`InterviewBatch` 要求问题文本非空。
    """
    node = _deref(node, defs)
    for union_key in ("anyOf", "oneOf"):
        branches = [b for b in node.get(union_key, []) if b.get("type") != "null"]
        if branches:
            return _default_payload(branches[0], defs)
    if "const" in node:
        return node["const"]
    if "enum" in node:
        return node["enum"][0]
    kind = node.get("type")
    if kind == "object":
        return {name: _default_payload(spec, defs) for name, spec in node.get("properties", {}).items()}
    if kind == "array":
        item = node.get("items", {})
        return [_default_payload(item, defs) for _ in range(int(node.get("minItems", 0) or 0))]
    if kind == "string":
        return "x" * max(int(node.get("minLength", 0) or 0), 1)
    if kind in ("integer", "number"):
        return int(node.get("minimum", 0) or 0)
    if kind == "boolean":
        return False
    return None


def _default_for_schema(schema: type[BaseModel] | None) -> BaseModel | None:
    """生成一个满足 `schema` 的最小对象；`schema` 为空时返回 `None`。"""
    if schema is None:
        return None
    json_schema = schema.model_json_schema()
    payload: dict[str, Any] = _default_payload(json_schema, json_schema.get("$defs", {}))
    payload.update(_SCHEMA_OVERRIDES.get(schema.__name__, {}))
    return schema.model_validate(payload)


class MockProvider:
    """Mock 适配器：按 schema 生成满足契约的最小对象，不发任何网络请求。"""

    name = "mock"

    async def chat(
        self,
        model: str,
        messages: list[dict],
        response_schema: type[BaseModel] | None = None,
        timeout: float = 30.0,
    ) -> ModelResponse:
        parsed: BaseModel | None = None
        error: str | None = None
        if response_schema is not None:
            try:
                parsed = _default_for_schema(response_schema)
            except ValidationError:
                # 修正表没覆盖到某个交叉约束时，与真实的解析失败同路返回，让调用方走
                # 重试与降级，而不是构造一个"看起来像"的值（技术方案 10.2）。
                error = "parse_error"
        content = parsed.model_dump_json() if parsed is not None else "（模拟响应）"
        return ModelResponse(
            content=content,
            parsed=parsed,
            model=model,
            provider=self.name,
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=0,
            is_fallback=False,
            error=error,
        )


__all__ = ["MockProvider", "_default_for_schema"]

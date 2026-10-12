"""span 属性键常量与截断阈值（T6-06）。

属性键集中在这里而不是各接入点拼字符串：写错一个键的后果是"这条 trace 永远查不出
某个字段"，而后端聚合按 `node.ok = false`、`gate.decision = block` 这类精确键取数，
拼错不会报错只会静默丢数据。

键名沿用技术方案 14.4 的清单，并保留 `node.` / `interview.` / `evidence.` / `gate.` /
`llm.` 五组前缀，与"三处必须写入的字段组"一一对应。

列表型属性的两条约定（14.4）：统一转成逗号分隔字符串（OTel 对序列类型的支持随后端
而异，字符串是共同子集）；超长列表截断——单次问诊引用可能上百条，全量写入会让 span
过大。截断时**必须**同时写 `evidence.ref_ids_count` 保留真实总数，否则截断后的
`evidence.ref_ids` 会被读成"本次只引用了这么几条"。
"""

from collections.abc import Sequence

# ---- 每节点必写（traced_node 产出）----
CONSULTATION_ID = "consultation.id"
PROCESS_ID = "process.id"
NODE_ROUND = "node.round"
NODE_OK = "node.ok"

# ---- 问诊节点 ----
INTERVIEW_SLOTS_HIT = "interview.slots_hit"
INTERVIEW_QUESTION_IDS = "interview.question_ids"

# ---- 证据核查节点 ----
EVIDENCE_REF_IDS = "evidence.ref_ids"
EVIDENCE_REF_IDS_COUNT = "evidence.ref_ids_count"
EVIDENCE_SOURCE_TYPES = "evidence.source_types"

# ---- 闸门子图节点 ----
GATE_DECISION = "gate.decision"
GATE_RULES_HIT = "gate.rules_hit"

# ---- generation span（模型网关内）----
LLM_MODEL = "llm.model"
LLM_PROVIDER = "llm.provider"
LLM_TOKENS = "llm.tokens"
LLM_LATENCY_MS = "llm.latency_ms"
LLM_DEGRADED = "llm.degraded"

# 一次问诊引用条目的写入上限。定在 20：常见问诊引用 3~8 条，写满 20 已能覆盖绝大多数
# 排障场景，而 20 个条目 ID 拼成的字符串只有几百字节，不会让 span 膨胀。
EVIDENCE_REF_IDS_LIMIT = 20

# 属性值缺失时的占位。用空串而不是省略该属性：后端按"属性存在"做查询与看板分组，
# 省略会让该类节点整体从分组里消失，比"取到空值"更难排查。
EMPTY_VALUE = ""


def csv(values: Sequence[str]) -> str:
    """把字符串序列转成逗号分隔属性值（保序、去重）。空序列给空串。"""
    seen: list[str] = []
    for value in values:
        text = str(value)
        if text not in seen:
            seen.append(text)
    return ",".join(seen)


def bounded_ids(values: Sequence[str], limit: int = EVIDENCE_REF_IDS_LIMIT) -> tuple[str, int]:
    """截断后的逗号分隔值 + 真实总数。调用方必须把两者分别写入
    `EVIDENCE_REF_IDS` 与 `EVIDENCE_REF_IDS_COUNT`——只写前者会低估引用量。
    """
    return csv(list(values)[:limit]), len(values)


__all__ = [
    "CONSULTATION_ID",
    "EMPTY_VALUE",
    "EVIDENCE_REF_IDS",
    "EVIDENCE_REF_IDS_COUNT",
    "EVIDENCE_REF_IDS_LIMIT",
    "EVIDENCE_SOURCE_TYPES",
    "GATE_DECISION",
    "GATE_RULES_HIT",
    "INTERVIEW_QUESTION_IDS",
    "INTERVIEW_SLOTS_HIT",
    "LLM_DEGRADED",
    "LLM_LATENCY_MS",
    "LLM_MODEL",
    "LLM_PROVIDER",
    "LLM_TOKENS",
    "NODE_OK",
    "NODE_ROUND",
    "PROCESS_ID",
    "bounded_ids",
    "csv",
]

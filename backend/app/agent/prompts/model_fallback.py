"""知识库为空时的模型兜底提示词构造（②-B：零闸门改动）。

正常情况下"查依据"（`ConsultStage.VERIFY`）的产出全部来自已核准知识库检索，模型只承担
查询改写。知识库为空时这一步只能空转，本模板负责把"已收集到的全部信息"交给模型，请它
补充判据与处置方向，使空库时这一步仍有内容可用。

### 两条硬约束写进提示词，且在代码层二次执行

1. **只输出药剂名称，禁止任何剂量数字**。用量、安全间隔期、稀释倍数、每季次数一律不得
   出现——这些数字必须逐字来自已核准知识库条目（"模板出事实、模型只改写"，技术方案 9.3）。
   提示词里的禁令只是第一道；`ModelBasis` 的 `_drop_dosage_entries` 校验器是第二道，
   含剂量数字的条目在契约构造时即被丢弃。
2. **不得编造条目编号**。模型给的药名没有对应的知识库条目，因此下游不会为它登记溯源，
   闸门的 `R-WHITELIST`/`R-PROVENANCE` 也验不了它——它只能作为"非药剂处置建议"的文本
   出现（见 `PrescriberAgent`），永远不进 `pesticide_items`。

### 与其余模板的边界

本模块与 `formatters` 一致，**不写任何农技事实**：只组织语言与约束，事实来自状态里已
收集的信息与模型自身的领域知识。构造是纯 Python，无 IO、不调模型。

本入口**不新增 `PROMPT_VERSIONS` 的模板名**：兜底复用检索档位（`TaskType.RETRIEVE`），
版本记录也复用 `retriever`。注册表的名字清单恒为七个，这是既有约定（见
`tests/agent/prompts/test_prompt_builders.py` 的 `test_prompt_versions_cover_all_names`），
为一次兜底新增模板名会让评估服务多出一个永远只有默认版本的分组。
"""

from __future__ import annotations

import json
from typing import Any

from app.agent.contracts import ModelBasis
from app.agent.prompts.formatters import (
    format_colloquial,
    format_diagnosis,
    format_history,
    format_observations,
    format_redlines,
    format_slots,
    format_terms,
)
from app.agent.state import ConsultationState
from app.process_config.models import ProcessConfig

# 提示词里注入的信息条数上限。兜底的输入是"已收集的全部信息"，但长上下文会稀释指令遵循度
# （技术方案 10.4），而兜底最需要的是模型严格按"只给药名"输出，因此证据与历史只取前几条。
_MAX_EVIDENCE_ROWS = 4
_CONTENT_CAP = 100


def build_model_fallback_prompt(config: ProcessConfig, state: ConsultationState) -> list[dict[str, Any]]:
    """拼装兜底提示词。system 负责角色与硬约束，user 注入已收集的全部信息。

    与判定/处方模板的差异：不注入药剂候选（本就为空），改为注入"没有检索条目"的事实与
    本流程术语、红线，让模型在明确边界内补充方向；并把"只给药名"这条禁令放在最靠近输出的
    位置，因为它是整份兜底里唯一有安全后果的约束。
    """
    evidence = list(state.get("evidence") or [])[:_MAX_EVIDENCE_ROWS]
    system = [
        f"你是{config.role_definition}，负责在缺乏本地资料时给出专业处置方向。",
        f"你必须输出符合以下 JSON Schema 的内容：{json.dumps(ModelBasis.model_json_schema(), ensure_ascii=False)}",
        "当前没有可用的当地知识库条目，请你依据自身的领域知识补充判断依据与处置方向。",
        "硬规则：suggested_pesticide_names 只能写药剂名称，**禁止出现任何用量、安全间隔期、"
        "稀释倍数、每季使用次数等数字**。用量必须由农户按标签或咨询当地农技站确定。",
        "硬规则：不得编造任何知识库条目编号或来源，你给的内容不带溯源。",
        "把握不足时宁可少给或不给，不要为了凑满清单而猜测。",
        "只输出 JSON，不要解释。",
        f"使用术语：{format_terms(config.terminology.display_metrics)}",
        f"面向农户的表述：{format_colloquial(config.terminology.colloquial_map)}",
        f"严禁输出：{format_redlines(config.redlines)}",
    ]
    user = [
        f"判定结论：{format_diagnosis(state.get('diagnosis'))}",
        f"已确认信息：{format_slots(config, state)}",
        f"现场事实：{format_observations(state.get('observations'))}",
        f"档案用药史：{format_history(state.get('prescription_history'))}",
        f"已有检索条目（可能为空）：{_format_evidence_rows(evidence)}",
    ]
    return [
        {"role": "system", "content": "\n".join(system)},
        {"role": "user", "content": "\n".join(user)},
    ]


def _format_evidence_rows(evidence: list[dict]) -> str:
    """已有检索条目的标题摘要。空库时说明"无"。

    不复用 `format_evidence`：它面向判定，回落的默认文案是"知识库未命中相关条目"，而兜底
    需要明确告诉模型"这不是故障、是请你补充"，两者语义不同。
    """
    if not evidence:
        return "无"
    return "；".join(str(item.get("title") or item.get("knowledge_id") or "未命名条目") for item in evidence)


__all__ = ["build_model_fallback_prompt"]

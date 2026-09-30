"""判定提示词构造（T2-60）。

与问诊模板的差异（技术方案 9.4）：**无候选问题池**，因为判定环节不生成问题；把易混淆项从
"鉴别点提示"改为"判据全文注入"，因为判定需要据此做排除裁定。system 里显式禁止百分比，与
契约层不含分数字段形成双保险——技术方案 5.3.4 要求把握程度用农户听得懂的话表达，而模型在
自由生成时很容易写出"大约 70% 的可能是"。
"""

from __future__ import annotations

import json
from typing import Any

from app.agent.contracts import DiagnosisResult
from app.agent.prompts.formatters import (
    format_confusables,
    format_evidence,
    format_history,
    format_observations,
    format_redlines,
    format_slots,
    format_terms,
)
from app.agent.state import ConsultationState
from app.process_config.models import ProcessConfig

# 提示词里注入的证据条数上限。技术方案 10.4 要求"只注入相关槽位与证据"，全量注入会让
# 长上下文稀释指令遵循度，而判定恰恰是最需要模型严格按 Schema 输出的环节。
_MAX_EVIDENCE_ROWS = 6
# 单条证据的内容摘要长度。判定要看的是判据而不是整篇条目，截断由后端做，不留给模型自己取舍。
_CONTENT_CAP = 120


def build_diagnoser_prompt(
    config: ProcessConfig,
    state: ConsultationState,
    evidence: list[dict] | None = None,
) -> list[dict[str, Any]]:
    """拼装判定提示词。system 九段、user 四段。

    易混淆项的鉴别点逐条注入，是"一份配置驱动三处行为"中判定这一处的落实。user 只注入已
    确认槽位、现场事实、检索到的证据与用药史；证据由调用方裁剪后传入（见 `_MAX_EVIDENCE_ROWS`），
    未显式传入时读 `state["evidence"]`，本函数不做二次筛选。
    """
    rows = evidence if evidence is not None else list(state.get("evidence") or [])
    system = [
        f"你是{config.role_definition}，负责给出判断结论。",
        f"你必须输出符合以下 JSON Schema 的内容：{json.dumps(DiagnosisResult.model_json_schema(), ensure_ascii=False)}",
        "判断依据必须逐条列出，每条对应证据中的具体条目。",
        "把握程度只能取 likely、preliminary、uncertain 三者之一，不得输出百分比或分数。",
        "下列候选必须显式鉴别，不能只给结论：",
        format_confusables(config.confusable_items),
        f"使用术语：{format_terms(config.terminology.display_metrics)}",
        f"严禁输出：{format_redlines(config.redlines)}",
        "证据不足以支撑结论时，选择 uncertain 并说明需要补什么，不要用模糊表述掩盖。",
    ]
    user = [
        f"已确认信息：{format_slots(config, state)}",
        f"现场事实：{format_observations(state.get('observations'))}",
        f"检索到的证据：{format_evidence(rows, max_rows=_MAX_EVIDENCE_ROWS, content_cap=_CONTENT_CAP)}",
        f"档案用药史：{format_history(state.get('prescription_history'))}",
    ]
    return [
        {"role": "system", "content": "\n".join(system)},
        {"role": "user", "content": "\n".join(user)},
    ]


__all__ = ["build_diagnoser_prompt"]

"""分诊提示词构造（T2-60）。

与问诊模板的差异（技术方案 9.4）：用七条流程的识别词替代单个流程的术语与红线——分诊阶段
流程尚未确定，注入某一条流程的红线会误导模型；并显式写"不得生成任何问题"，与契约层
`TriageResult` 不含问题字段形成双保险（第一道防线在契约里）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from app.agent.contracts import TriageResult
from app.agent.prompts.formatters import format_entries, format_process_keywords
from app.agent.state import ConsultationState, ProcessId
from app.process_config.models import ProcessConfig


def build_triage_prompt(
    configs: Mapping[ProcessId, ProcessConfig],
    state: ConsultationState,
    *,
    current_message: str = "",
) -> list[dict[str, Any]]:
    """拼装分诊提示词。system 六段、user 三段。

    可选流程清单来自流程配置而不是硬编码表：新增一条流程只需加一份 YAML，识别词自动进入
    提示词，这是"流程配置是唯一事实来源"在提示词层的落实。农户本次输入由调用方给出，本函数
    不自行回看消息历史，保持与其余模板一致的纯函数形态。
    """
    profile = state.get("profile") or {}
    system = [
        "你是话农点金的分诊模块，唯一任务是把农户的问题归到七条流程之一。",
        f"可选流程及其识别词：{format_process_keywords(configs)}",
        "你必须输出符合以下 JSON Schema 的内容：",
        json.dumps(TriageResult.model_json_schema(), ensure_ascii=False),
        "硬规则：不得生成任何问题。不得输出问句。",
        "若无法确定流程，选择 general 并在 fallback_reason 中说明原因。",
    ]
    user = [
        f"农户所在地区：{profile.get('location') or '未知'}",
        f"档案中的经营条目：{format_entries(profile.get('entries'))}",
        f"农户本次输入：{current_message}",
    ]
    return [
        {"role": "system", "content": "\n".join(system)},
        {"role": "user", "content": "\n".join(user)},
    ]


__all__ = ["build_triage_prompt"]

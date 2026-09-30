"""处方提示词构造（T2-60）。

与问诊模板的差异（技术方案 9.4）：**无候选问题池**，因为处方环节不生成问题；增加两条硬规则
"不得新增、修改或删除任何药剂条目"与"不得改动任何数字"。这两条是"模板出事实"原则在提示词层
的表达，与 T2-21 的代码覆写形成双保险——真正生效的是 `post_validate` 的强制覆写，写进提示词
是为了减少模型的无效尝试。

注入 `section_order` / `max_chars` / `required_sections` 而非在代码里截断：输出模板是"一份配置
驱动三处行为"中处方这一处的落点，字数与必含小节随流程变化。
"""

from __future__ import annotations

import json
from typing import Any, cast

from app.agent.contracts import PesticideItem, Prescription
from app.agent.prompts.formatters import (
    format_colloquial,
    format_diagnosis,
    format_pesticide_candidates,
    format_redlines,
    format_slots,
)
from app.agent.state import ConsultationState
from app.process_config.models import ProcessConfig


def build_prescriber_prompt(
    config: ProcessConfig,
    state: ConsultationState,
    candidates: list[PesticideItem] | None = None,
) -> list[dict[str, Any]]:
    """拼装处方提示词。system 负责角色与硬规则，user 注入判定结论、候选与已确认信息。

    候选未显式传入时读 state 上本轮写入的 `_pesticide_candidates`，使调用方无需关心来源；
    显式传入时用调用方手里的同一份候选，保证提示词输入与代码覆写依据一致。
    """
    rows = candidates if candidates is not None else cast(list[PesticideItem], state.get("_pesticide_candidates") or [])
    template = config.output_template
    system = [
        f"你是{config.role_definition}，负责把已确定的事实组织成农户能看懂的处方。",
        f"你必须输出符合以下 JSON Schema 的内容：{json.dumps(Prescription.model_json_schema(), ensure_ascii=False)}",
        "硬规则：不得新增、修改或删除任何药剂条目。",
        "硬规则：不得改动任何数字。数字只能原样使用下面给出的值。",
        f"输出段落顺序：{'、'.join(template.section_order)}。",
        f"总字数不超过 {template.max_chars} 字。",
        f"必含内容：{'、'.join(template.required_sections) or '无'}。",
        f"面向农户的表述：{format_colloquial(config.terminology.colloquial_map)}",
        f"严禁输出：{format_redlines(config.redlines)}",
    ]
    user = [
        f"判定结论：{format_diagnosis(state.get('diagnosis'))}",
        f"可用药剂候选（不可增删）：{format_pesticide_candidates(rows)}",
        f"已确认信息：{format_slots(config, state)}",
    ]
    return [
        {"role": "system", "content": "\n".join(system)},
        {"role": "user", "content": "\n".join(user)},
    ]


__all__ = ["build_prescriber_prompt"]

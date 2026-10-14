"""处方提示词构造（T2-60）。

与问诊模板的差异（技术方案 9.4）：**无候选问题池**，因为处方环节不生成问题；增加两条硬规则
"不得新增、修改或删除任何药剂条目"与"不得改动任何数字"。这两条是"模板出事实"原则在提示词层
的表达，与 T2-21 的代码覆写形成双保险——真正生效的是 `post_validate` 的强制覆写，写进提示词
是为了减少模型的无效尝试。

注入 `section_order` / `max_chars` / `required_sections` 而非在代码里截断：输出模板是"一份配置
驱动三处行为"中处方这一处的落点，字数与必含小节随流程变化。

知识库为空时（开关开启）追加一段"模型兜底药名"：只给名称、**不给任何数字**，因为这批药名
没有知识库条目与溯源支撑。剂量一律留空并引导到当地农技站。
"""

from __future__ import annotations

import json
from typing import Any, cast

from app.agent.contracts import ModelBasis, PesticideItem, Prescription
from app.agent.prompts.formatters import (
    format_colloquial,
    format_diagnosis,
    format_pesticide_candidates,
    format_redlines,
    format_slots,
)
from app.agent.state import ConsultationState
from app.process_config.models import ProcessConfig

# 兜底药名清单的长度上限与提示词里的固定说明。药名过多会让模型把处方写成一张长清单，
# 而农户需要的是明确的一两条方向。
_FALLBACK_NAME_CAP = 6
_FALLBACK_HEADER = "本地资料暂缺，以下药剂名称仅作方向参考（不含用量，用量必须由当地农技站确认）："
_FALLBACK_RULE = "硬规则：上述药名只能以名称出现，不得为它们写任何用量、安全间隔期、稀释倍数或每季次数。"


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
    fallback_names = _fallback_names(config, state, rows)
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
    if fallback_names:
        # 兜底药名进 user 而不是 system：它随本次状态变化，属"本轮输入"；而两条硬规则
        # （不得写数字）追加在 system 末尾，紧邻输出要求，约束力更强。
        system.append(_FALLBACK_RULE)
        user.append(f"{_FALLBACK_HEADER}{'、'.join(fallback_names)}")
    return [
        {"role": "system", "content": "\n".join(system)},
        {"role": "user", "content": "\n".join(user)},
    ]


def _fallback_names(config: ProcessConfig, state: ConsultationState, rows: list[PesticideItem]) -> list[str]:
    """取兜底药名。只在"没有知识库候选"且兜底依据存在且同流程时给出。

    三条前置条件都必要：有候选说明知识库有据可依，兜底药名与之并列会让模型在"照抄候选"
    与"参考药名"之间摇摆；依据缺失（未开启兜底或未触发）时无名称可用；跨流程的依据不得
    套用（术语与判据不能跨流程混用，技术方案 9.3）。
    """
    if rows:
        return []
    raw = state.get("model_basis")
    if not isinstance(raw, dict):
        return []
    try:
        basis = ModelBasis.model_validate(raw)
    except Exception:
        # state 里的字段来自检查点，可能是旧版本写的；解析失败一律当作"没有兜底依据"，
        # 提示词构造不该因为一个可选增强而抛异常。
        return []
    if basis.process_id is not config.process_id:
        return []
    return basis.suggested_pesticide_names[:_FALLBACK_NAME_CAP]


__all__ = ["build_prescriber_prompt"]


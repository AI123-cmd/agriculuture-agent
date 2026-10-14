"""模型兜底依据契约（知识库为空时的"查依据"兜底产出）。

背景：`ConsultStage.VERIFY`（查依据）由 `RetrieverAgent` 实现，正常情况下它从知识库
检索条目交给下游判定与处方。知识库为空时它只能返回空集，这一步就空转了。本契约承载
"知识库为空时由大模型补充判据"的产出，使这一步在空库时仍有内容可用。

设计上刻意只保留**文本清单**，没有任何剂量字段，原因是一条安全红线：
`app/agent/contracts/prescription.py` 的 `PesticideItem` 里的 `dosage_min`/`dosage_max`/
`safety_interval_days`/`max_uses_per_season` 必须逐字来自已核准的知识库条目
（"模板出事实、模型只改写"，技术方案 9.3）。若让模型给出这些数字，红线当场失效。
因此模型兜底**只能给药剂名称**，名称由处方侧织入非药剂处置建议的文本，真正的剂量区间
与安全间隔期在库空时一律留空、由代码写死"请咨询当地农技站"。

所有字段都是 `list[str]` 而不是整段自由文本，是为了让"禁止剂量数字"这条护栏可以
统一实施：任何一条含剂量数字的条目在构造时被直接丢弃（见 `_sanitize`），不存在
"段落里混进一个用量"却无法局部清除的情况。
"""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.state import ProcessId

# 剂量数字的识别模式：数字（可带小数）+ 剂量单位，或 + "倍"（稀释倍数）。
# 与 `app/gate/rules/provenance.py` 的 `UNIT_PATTERN` 同源但**更宽**：闸门那条只用于
# 反向核对处方文本，这里用于在源头丢弃模型给的剂量。宽一点是故意的——兜底产出的
# 价值远低于漏掉一个用量的代价。
_DOSAGE_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|亩|克|毫升|千克|公斤|升|℃|天|小时|次|头|株|丛|片|倍)"
)

# 单条文本长度上限。提示词约束模型给短句，这里只做兜底截断，防止一条冗长文本挤占
# 提示词预算（档位复用 `TaskType.RETRIEVE`，预算按中等规模设定）。
_ITEM_MAX_CHARS = 60


def _sanitize(raw: str) -> str | None:
    """清洗单条文本：去空白、截断、丢弃含剂量数字的条目。

    返回 `None` 表示该条目必须整条丢弃。丢弃而不是抛错：模型输出不可控，一条坏条目
    不该让整份兜底产出失效；丢完只剩空清单时下游照常走"无可用药剂"的既有路径。
    """
    text = raw.strip()
    if not text or _DOSAGE_PATTERN.search(text):
        return None
    return text[:_ITEM_MAX_CHARS]


class ModelBasis(BaseModel):
    """知识库为空时的模型兜底依据。

    `process_id` 必填：兜底产出只在同一流程内可复用，跨流程套用会重演"术语与判据
    不能跨流程混用"的问题（技术方案 9.3），处方侧消费前会核对它。

    四个清单的用途各不相同：
    - `suggested_pesticide_names` 供处方侧织入非药剂处置建议文本（只给药名，不给用量）；
    - `basis_points` 供判定侧在无检索条目时充当佐证；
    - `observation_suggestions` / `help_suggestions` 是"进一步观察"与"何时求助当地
      农技站"的建议，与药名一起构成空库时仍可交付的处置建议。
    """

    model_config = ConfigDict(extra="forbid")

    process_id: ProcessId
    suggested_pesticide_names: list[str] = Field(default_factory=list, max_length=8)
    basis_points: list[str] = Field(default_factory=list, max_length=6)
    observation_suggestions: list[str] = Field(default_factory=list, max_length=6)
    help_suggestions: list[str] = Field(default_factory=list, max_length=6)

    @field_validator(
        "suggested_pesticide_names",
        "basis_points",
        "observation_suggestions",
        "help_suggestions",
        mode="after",
    )
    @classmethod
    def _drop_dosage_entries(cls, raw: list[str]) -> list[str]:
        """统一清洗：丢弃含剂量数字的条目、去重（保序）、去掉空串。

        去重保序用 `dict.fromkeys`：模型常在相邻条目里重复同一药名，重复会在处方
        文本里显示成两遍，读起来像"用了两次"。
        """
        cleaned = (_sanitize(item) for item in raw)
        return list(dict.fromkeys(item for item in cleaned if item))


__all__ = ["ModelBasis"]

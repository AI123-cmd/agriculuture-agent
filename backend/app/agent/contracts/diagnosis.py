"""判定契约（技术方案 5.3.4）。

三条硬约束在这里落地：把握程度用枚举而不是数值；结论不确定时必须说明"不确定在哪"或
"还缺什么"；超边界必须给出原因。第三条是本模块新增的校验——技术方案 6.4 指出把握不足时
正确做法是给出观察 + 复查的动作，若结论是"不确定"却既不说明来源也不说需要补什么，农户
拿到的就是一句无从照做的模糊话，因此契约层面不允许这种输出。

`excluded` 必填但**允许为空列表**：只有存在易混淆项的流程才要求非空，而"当前流程有没有
易混淆项"是契约不知道的事（它需要读流程配置）。因此"必填且非空"的业务检查放在 T2-19 的
判定智能体里，本模块只保证字段形态。

数值型置信度分数**不在本契约中**。技术方案 5.3.4 允许内部保留分数用于路由判断，但不得进入
展示文案。分数是节点返回的 state 字段（`confidence_score`），由 T2-19 写入、T2-24 读取，
与契约字段彻底分开——这是保证分数不泄漏到展示层的结构性手段，而不是靠约定。
"""

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, model_validator

# 不确定性来源的文本形态。技术方案 5.3.4 把它定义为 `list[str]`；此处给它一个概念别名以
# 表明"这是一条来源说明"，同时保持字符串类型——来源既有系统写入的固定表述（"未检索到支撑
# 条目"），也有带上下文的拼接文本（"判定环节不可用（超时）"），收敛成枚举会迫使调用方
# 为每种情形改契约。
UncertaintySource: TypeAlias = str

# 把握程度的展示文案。展示层只允许读这张表，不允许自行拼装：技术方案 5.3.4 要求把握程度
# 必须用农户听得懂的话表达，分散拼接会让同一档位在不同入口出现不同措辞。
CONFIDENCE_DISPLAY: dict[str, str] = {
    "likely": "很可能是",
    "preliminary": "初步判断",
    "uncertain": "不确定",
}


class DiagnosisResult(BaseModel):
    """判定输出。技术方案 5.3.4 原样定义。"""

    model_config = ConfigDict(extra="forbid")

    conclusion: str
    confidence_level: Literal["likely", "preliminary", "uncertain"]
    basis: list[str]
    uncertainty_source: list[UncertaintySource]
    excluded: list[str]
    terminology_profile: str
    needs_more_info: list[str]
    out_of_scope: bool
    boundary_reason: str | None = None

    @model_validator(mode="after")
    def _basis_not_blank(self) -> "DiagnosisResult":
        """结论必须有依据，且逐条非空。

        空字符串在展示层会呈现为一个没有内容的"依据"条目，农户看到的是一句话都说不出的
        结论。要求逐条去空白后非空，把"给了但等于没给"也一并拦住。
        """
        if not self.basis or not all(b.strip() for b in self.basis):
            raise ValueError("basis 必填且逐条非空")
        return self

    @model_validator(mode="after")
    def _boundary_reason_required(self) -> "DiagnosisResult":
        """超边界时跳过处方直达转人工（技术方案 5.3.4），这条路由依赖 `boundary_reason`
        向农户解释"为什么不能给建议"。没有原因的边界判定在农户侧表现为一次无解释的拒答。
        """
        if self.out_of_scope and not self.boundary_reason:
            raise ValueError("out_of_scope=True 时必须给出 boundary_reason")
        return self

    @model_validator(mode="after")
    def _uncertain_needs_more_info(self) -> "DiagnosisResult":
        """不强行择一的落地：不确定是合法结论，但必须说明不确定在哪或还缺什么。

        两类来源分开记录（`uncertainty_source` 是已知的不可靠因素，`needs_more_info` 是
        可补的信息），线上据此区分"知识库覆盖不足"与"真难判断"——前者要补知识，后者要补判据。
        二者皆空时农户只会收到一句"不确定"，无从照做。
        """
        if self.confidence_level == "uncertain" and not self.needs_more_info and not self.uncertainty_source:
            raise ValueError("confidence_level=uncertain 时必须给出不确定性来源或需补信息")
        return self


__all__ = ["CONFIDENCE_DISPLAY", "DiagnosisResult", "UncertaintySource"]

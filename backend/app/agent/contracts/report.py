"""报告守卫契约（技术方案 5.3.7）。

报告守卫与安全闸门是**仅有的两个 fail-closed 组件**：其余智能体失败时降级但仍服务，
这两个失败时一律选最保守方向。本契约的体现是"触发即必须带可执行内容"——`triggered=True`
时构造不出没有识别信号、没有强制动作或来源为 `none` 的结果，因此农户不会被推到一个
说不出原因也做不了什么的"报告义务"面前。

与问诊/判定环节的优先级：报告守卫的判定**优先于**这两者。实现上它是条件边的旁路监听者，
在 `observe` 与 `interview` 之后各挂一次检查，命中即 `stop_consultation=True` 并跳转
`handoff`，已收集的问诊内容保留但不继续追问（见 T2-24 的路由函数）。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

# trigger_source 语义：
# signal     —— 单个识别信号命中即触发。不是只有群死群伤才需报告（产品说明书 5.7）
# group      —— 同栏舍或同批次多点出现
# mass_death —— 出现群体性死亡
# none       —— 未触发
#
# 注意：signal 的门槛必须低。守卫是 fail-closed 组件，宁可多报一次，
# 不可漏报一次。误报的代价是农户多打一个电话，漏报的代价是动物疫病扩散。
#
# 这段注释的作用是防止后续实现者"为了降低误报率而收紧 signal 判定"：
# 收紧是产品决策而非工程决策，必须显式提出并经过确认，而不是在实现里悄悄做掉。


class ContactInfo(BaseModel):
    """本地联系入口。承载法定报告路径与农技站联系方式。

    `kind` 区分兽医站、农技站、主管机构与热线：养猪流程的法定报告对象与种植流程的农技站
    不是同一类，用枚举区分让前端能按类别分组展示。`phone` 允许为空但 `kind` 必填——
    在地图与地址尚未接入时，至少能告诉农户"该联系哪个机构"。
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["vet_station", "agri_station", "authority", "hotline"]
    name: str
    phone: str | None = None
    region_code: str | None = None
    note: str | None = None


class ReportSignal(BaseModel):
    """识别信号定义。从流程配置的槽位与关键词推导。

    信号的取值内容属于农技确认范围；本模块只定义结构，信号列表由 T2-22 组装。
    """

    model_config = ConfigDict(extra="forbid")

    signal_id: str
    keywords: list[str]
    slot_key: str | None = None
    severity: Literal["signal", "group", "mass_death"]
    description: str


class ReportDecision(BaseModel):
    """报告守卫输出。技术方案 5.3.7 原样定义。"""

    model_config = ConfigDict(extra="forbid")

    triggered: bool
    matched_signals: list[str]
    trigger_source: Literal["signal", "group", "mass_death", "none"]
    mandatory_actions: list[str]  # 立即隔离、消毒、报告
    contacts: list[ContactInfo]
    stop_consultation: bool

    @model_validator(mode="after")
    def _trigger_requires_content(self) -> "ReportDecision":
        """触发时必须带可执行内容。

        产品说明书 5.7 要求输出"立即隔离、消毒、报告 + 本地联系入口"，因此
        `mandatory_actions` 与 `contacts` 在触发时都有意义。这里只强制
        `mandatory_actions` 非空：本地联系入口可能尚未配置，`contacts` 为空时由 T2-22
        记录告警，而不是让整条报告结论构造失败——那会让最需要输出的场景反而无输出。
        """
        if self.triggered:
            if not self.matched_signals:
                raise ValueError("triggered=True 时必须给出命中的识别信号")
            if not self.mandatory_actions:
                raise ValueError("triggered=True 时必须给出强制动作")
            if self.trigger_source == "none":
                raise ValueError("triggered=True 时 trigger_source 不得为 none")
        return self

    @model_validator(mode="after")
    def _stop_only_when_triggered(self) -> "ReportDecision":
        """中止问诊只在触发报告时才成立：未触发却中止会让农户的咨询无故中断。"""
        if self.stop_consultation and not self.triggered:
            raise ValueError("未触发报告时不得中止问诊")
        return self

    @model_validator(mode="after")
    def _non_trigger_defaults(self) -> "ReportDecision":
        """与上一条构成双向约束：`stop_consultation` 与 `triggered` 同真同假。

        `trigger_source` 一并在未触发时钉死为 `none`，避免出现"来源说是 signal 但结论说
        没触发"这种自相矛盾的审计记录。
        """
        if not self.triggered and self.trigger_source != "none":
            raise ValueError("未触发时 trigger_source 必须为 none")
        return self


__all__ = ["ContactInfo", "ReportDecision", "ReportSignal"]

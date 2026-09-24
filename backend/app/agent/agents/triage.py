"""分诊智能体：受理即分流（T2-14）。

它是农户提问后被调用的第一个智能体，唯一产出是"进哪条流程、对象是否已确认、进问诊前
缺哪些槽位"，**不生成任何问题**——`TriageResult` 里没有承载问题的字段，问题由 T2-16 的
问诊智能体生成。这条硬约束让分诊可被单元测试完全覆盖，也让"确认对象"这一次交互由主图
统一处理，而不是混在分诊的输出里。

三处判定刻意不交给模型：

- **对象确认**：档案流程与农户表述的实体匹配是纯函数（`match_object_alias`）。让模型
  生成确认问句，会产出"您问的是茶树还是水稻呢"这类不说明档案内容的问法，农户无从回答。
- **`object_confirmed`**：模型给的值会被代码覆写。若信任模型，它看到输入里有作物词就会
  置 True，恰好跳过最需要确认的场景——档案与表述不符。
- **`slot_gaps`**："必问清单 − 已确认 − 可自动填充"是集合运算，规则明确，交给模型只会
  引入不确定性。

失败出口是回退通用流程（`FailureMode.DEGRADE`），`fallback_reason` 带上失败原因，让线上
能区分"模型说没法归类"与"模型不可用"两种 general 占比。
"""

import json
from functools import lru_cache

from app.agent.agents.base import AgentContext, BaseAgent, FailureMode
from app.agent.contracts import ObjectConfirmRequest, TriageResult, dump_contract, match_object_alias
from app.agent.state import ConsultationState, ConsultStage, ProcessId, TaskType
from app.llm import ModelGateway, build_gateway
from app.process_config.loader import get_process_config, load_all_configs
from app.process_config.models import ProcessConfig

# 无法归类时的兜底说明。契约要求降级到 general 必须给出理由，这里是"模型给了空值"时的补填。
_FALLBACK_NO_SIGNAL = "未匹配到流程专属信号"


class TriageAgent(BaseAgent[TriageResult]):
    """分诊：判流程、判对象是否已确认、算槽位缺口。"""

    name = "triage"
    output_schema = TriageResult
    task_type = TaskType.TRIAGE
    failure_mode = FailureMode.DEGRADE  # 回退通用流程
    max_attempts = 2

    def __init__(self, gateway: ModelGateway, configs: dict[ProcessId, ProcessConfig]) -> None:
        super().__init__(gateway)
        self._configs = configs

    # ---- 输入构造 ----

    def build_messages(self, state: ConsultationState, ctx: AgentContext) -> list[dict]:
        """拼装提示词：识别词与档案条目结构化注入，不拼接自由文本。

        系统提示里显式写出"不得输出问句"是第二道防线，第一道是契约里根本没有问题字段。
        可选流程清单来自流程配置而不是硬编码表：新增一条流程只需加一份 YAML，识别词自动
        进入提示词，这是"流程配置是唯一事实来源"在提示词层的落实。
        """
        profile = state.get("profile") or {}
        system = [
            "你是话农点金的分诊模块，唯一任务是把农户的问题归到七条流程之一。",
            f"可选流程及其识别词：{self._format_process_keywords()}",
            "你必须输出符合以下 JSON Schema 的内容：",
            json.dumps(TriageResult.model_json_schema(), ensure_ascii=False),
            "硬规则：不得生成任何问题。不得输出问句。",
            "若无法确定流程，选择 general 并在 fallback_reason 中说明原因。",
        ]
        user = [
            f"农户所在地区：{profile.get('location') or '未知'}",
            f"档案中的经营条目：{self._format_entries(profile.get('entries'))}",
            f"农户本次输入：{self._current_message_text(state)}",
        ]
        return [
            {"role": "system", "content": "\n".join(system)},
            {"role": "user", "content": "\n".join(user)},
        ]

    def _format_process_keywords(self) -> str:
        """七条流程的识别词清单。每条别名只取前八个。

        技术方案 10.4 指出长上下文会降低指令遵循度，而流程别名是长列表里最不具区分度的
        部分：截断到八个既能覆盖主要表述，也不至于让提示词膨胀。
        """
        lines: list[str] = []
        for pid in ProcessId:
            config = self._configs.get(pid)
            if config is None:
                continue
            aliases = "、".join(config.object_aliases[:8]) or "无"
            lines.append(f"{pid.value}（{config.display_name}）：{aliases}")
        return "\n".join(lines)

    def _display_of(self, process_id: str | ProcessId) -> str:
        """流程的展示名。配置缺失或标识非法时回落到原值，绝不抛异常。

        确认问句是直接给农户看的，不能因为一份配置缺失就让整条问诊链断在拼句子上。
        """
        try:
            pid = ProcessId(process_id)
        except ValueError:
            return str(process_id)
        config = self._configs.get(pid)
        return config.display_name if config is not None else str(process_id)

    # ---- 对象确认（纯代码判定） ----

    def resolve_object_confirmation(
        self,
        triage: TriageResult,
        state: ConsultationState,
    ) -> ObjectConfirmRequest | None:
        """对象确认判定：档案流程与农户表述做实体匹配。

        只在"档案存在且农户明确提到的对象与档案不符"时返回确认请求：

        - 档案为空：没有可对比的对象，问也无从问起（产品说明书 4.2.2 与 T2-14 的口径一致）；
        - 农户没提到任何已知对象：无从与档案比对，不构成冲突；
        - 农户提到的对象与档案一致：直接通过，不必打扰农户。

        技术方案 5.3.2 的规则是"档案为空或农户问的对象与档案不符，系统需先与农户确认对象"，
        后一种情形正是这里的第三个分支。
        """
        profile_pid = state.get("profile_process_id")
        if profile_pid is None:
            return None
        try:
            profile_process_id = ProcessId(profile_pid)
        except ValueError:
            # 档案里的流程标识非法（脏数据）：不构成可比对的对象，交给分诊结果继续。
            return None
        matched = match_object_alias(self._current_message_text(state), self._configs)
        if matched is None or matched == profile_process_id:
            return None
        profile_label = self._display_of(profile_process_id)
        asked_label = self._display_of(triage.process_id)
        return ObjectConfirmRequest(
            asked_process_id=triage.process_id,
            profile_process_id=profile_process_id,
            candidate_labels=[profile_label, asked_label],
            question_text=f"您档案里登记的是{profile_label}，这次问的是{asked_label}吗",
            # 匹配函数返回流程标识而非命中的别名，故此处留空。
            matched_alias=None,
        )

    # ---- 槽位缺口（纯代码计算） ----

    def compute_slot_gaps(self, state: ConsultationState, config: ProcessConfig) -> list[str]:
        """按流程必问清单与现有槽位，算出进入问诊前的缺口。纯代码，不用模型。

        `source_hint` 非空的槽位不算缺口：它们可由档案或环境自动填充，纳入缺口只会让
        问诊去问一个系统本就能自己拿到答案的问题。这一点与 T2-17 的筛选器第二步口径一致。
        """
        gaps: list[str] = []
        for spec in sorted(config.required_slots, key=lambda s: s.ask_order):
            if not spec.required:
                continue
            slot = (state.get("slots") or {}).get(spec.key)
            if slot is None or slot.status == "missing":
                if spec.source_hint:
                    continue
                gaps.append(spec.key)
        return gaps

    # ---- 契约之外的字段覆写 ----

    async def post_validate(self, result: TriageResult, state: ConsultationState, ctx: AgentContext) -> TriageResult:
        """覆写模型给的值：对象确认、兜底原因、槽位缺口。"""
        if self.resolve_object_confirmation(result, state) is not None:
            result = result.model_copy(update={"object_confirmed": False})
        if result.process_id is ProcessId.GENERAL and not result.fallback_reason:
            result = result.model_copy(update={"fallback_reason": _FALLBACK_NO_SIGNAL})
        config = self._configs.get(result.process_id) or ctx.process_config
        return result.model_copy(update={"slot_gaps": self.compute_slot_gaps(state, config)})

    # ---- 降级 ----

    async def degrade(self, state: ConsultationState, ctx: AgentContext, reason: str) -> TriageResult:
        """降级：回退通用流程，并保留档案中的对象标签。

        `object_confirmed=True` 是刻意的：已经无法分诊的情况下再插入一次对象确认交互，对
        农户是纯粹的额外负担，而通用流程本身不依赖对象精度。
        """
        profile = state.get("profile") or {}
        return TriageResult(
            process_id=ProcessId.GENERAL,
            object_confirmed=True,
            object_label=profile.get("object_label") or "未指定作物",
            slot_gaps=[],
            profile_ready=bool(profile),
            fallback_reason=f"分诊服务不可用，已按通用农业流程继续（{reason}）",
            handoff_to_report=False,
        )

    # ---- 提示词输入的形态兼容 ----

    @staticmethod
    def _format_entries(entries: object) -> str:
        """档案经营条目的可读摘要。

        档案结构由调用方注入（T3-03 的 `FarmerProfile`），本层不导入它的模型，故只做形态
        兼容：条目列表、单个条目、或缺失。
        """
        if not entries:
            return "无"
        items: list[object]
        if isinstance(entries, dict):
            items = [entries]
        elif isinstance(entries, list):
            items = list(entries)
        else:
            return str(entries)
        parts: list[str] = []
        for entry in items:
            if isinstance(entry, dict):
                process = entry.get("process_id") or "未指定"
                started = entry.get("start_date") or "未记录"
                parts.append(f"{process}（起于 {started}）")
            else:
                parts.append(str(entry))
        return "、".join(parts) or "无"

    @staticmethod
    def _current_message_text(state: ConsultationState) -> str:
        """农户本次输入：取 `messages` 里最后一条 user 消息，取不到返回空串。"""
        for message in reversed(list(state.get("messages") or [])):
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
                return content if isinstance(content, str) else ""
        return ""


@lru_cache(maxsize=1)
def get_triage_agent() -> TriageAgent:
    """分诊智能体单例：网关与流程配置在进程内只装配一次。"""
    return TriageAgent(build_gateway(), load_all_configs())


async def run_triage(state: ConsultationState, config: dict | None = None) -> dict:
    """主图 `triage` 节点（T2-25 的节点契约）。

    返回 state 增量：分诊结果（序列化）、流程标识与环节。`process_id` 必须由本节点写入
    ——`route_after_triage` 之后由 `subgraph_router` 按它选子图，链路中间没有第二个写入点。
    """
    agent = get_triage_agent()
    general = get_process_config(ProcessId.GENERAL)
    ctx = AgentContext(
        process_config=general,  # 分诊阶段流程尚未确定，用通用配置作为环境依赖
        config_version=state.get("config_version") or general.version,
        task_type=TaskType.TRIAGE,
        trace_id=state.get("trace_id") or "",
    )
    result = await agent.run(state, ctx)
    return {
        "triage": dump_contract(result),
        "process_id": result.process_id,
        "stage": ConsultStage.TRIAGE,
    }


__all__ = ["TriageAgent", "get_triage_agent", "run_triage"]

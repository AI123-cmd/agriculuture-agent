"""快照文案安全校验（T3-08，技术方案 11.1 的 GATE 节点）。

提示条目的最终文案在落库前要过闸门：模板出事实、模型只改写（T3-07），但改写仍可能引入或
改错数字，也可能触碰本流程的禁用输出清单。本模块复用闸门规则做**只读**校验，不复制规则实现
——判据归闸门所有（`app/gate/rules/`），这里只负责把每条提示组装成 `GateContext` 喂给引擎，
再按裁定决定条目去留。

只跑三条规则，它们正好覆盖提示文案可能出的三类安全问题：

1. `R-PROVENANCE` 反向扫描——文案里出现的带单位数字必须在登记表中有来源。提示条目的登记表
   形态与处方不同：`facts_provenance` 的键是字段路径，而文案里是"数值加单位"，故本模块把条目
   四段里出现的数值当作"模板已登记"填进 `numeric_tokens`，供规则比对。
2. `R-NO-DATA` 正向核对——数值字段不得漏登记（提示条目未携带数值字段，此规则主要拦文案里
   多出来的无来源数字）。
3. `R-REDLINE` 拦截命中本流程禁用输出清单的表述。

**与处方处理的差别**：处方是整体交付物，不过关即降级为转人工引导；提示条目是离散的，丢弃
一条不影响其余条目（任务文件第三节），因此这里对判定不是 `allow` 的条目**整条丢弃**——它不
出现在返回值里——同时写一条 `gate_audit` 供复盘。不通过的条目**不落任何业务表**。

**纯读取**：本模块只读入参与配置、只写 `gate_audit` 审计行；不写任何业务表，也不直接查库
（`db` 参数只为与上层链路签名一致而保留，本函数体内不使用）。

**配置不可用即 fail-closed**：流程红线配置取不到时构造 `LoadedRedlines.unavailable`，而不是
"回落到通用规则"——产品说明书 9.3 明确"流程专属红线不可缺省"。此时三条规则都不具备可信前提，
引擎按 `GateResult` 契约走 block 分支，不会有任何条目被放行。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.state import ProcessId as ConfigProcessId
from app.gate.audit import GateAuditWriter
from app.gate.contracts import GateContext, GatePayload, GatePrescription
from app.gate.engine import GateEngine
from app.gate.enums import GateDecision, GateRuleId
from app.gate.enums import ProcessId as GateProcessId
from app.gate.ports import KnowledgeEntrySnapshot
from app.gate.rules.base import build_rules
from app.gate.rules.provenance import UNIT_PATTERN
from app.models import PromptItem
from app.process_config.loader import get_process_config
from app.process_config.redlines import LoadedRedlines, RedlineValidator
from app.repositories.gate_audit import GateAuditRepository

# 本模块只执行的规则清单。刻意不含 `R-REDLINE-LOAD`（引擎的前置规则）与需要知识库、用药史
# 的那几条：提示条目的安全面只有"数字有来源"与"不碰红线"两件事，多跑既不必要也会引入无关
# 判据。清单固定为元组，供 `build_rules` 实例化。
_CHECKED_RULE_IDS: tuple[str, ...] = (
    GateRuleId.PROVENANCE.value,
    GateRuleId.NO_DATA.value,
    GateRuleId.REDLINE.value,
)

# 模板已登记数值在 `numeric_tokens` 中的来源标记。与字段路径区分：它表示该数值取自模板填充的
# 事实段，而非模型改写引入。
_TEMPLATE_SOURCE = "template"


@dataclass(frozen=True)
class RenderedItem:
    """一条待校验的提示条目：原始 ORM 对象与其最终展示文案。

    `text` 与 `item.item` 分开传，是因为展示文案由 `writer.build_text` 生成（模板原文或模型
    改写结果），并不回写到 ORM 对象上；闸门校验的对象是最终文案，溯源登记的对象是条目四段。
    """

    item: PromptItem
    text: str


class _NoKnowledge:
    """只读知识桩：三个方法一律回答"没有这条已核准条目"。

    本模块执行的三条规则都不读知识库，传桩即可。取值一律走 fail-closed 方向——返回
    `None`/`False`/`()`，即便未来有人把一条读知识库的规则加进清单，桩也不会给出"已核准"的
    假象。实现满足 `app.gate.ports.KnowledgeFacts` 协议。
    """

    def get_entry(self, knowledge_id: str) -> KnowledgeEntrySnapshot | None:
        return None

    def is_approved(self, knowledge_id: str) -> bool:
        return False

    def get_registered_crops(self, knowledge_id: str) -> tuple[str, ...]:
        return ()


def _registered_tokens(item: PromptItem) -> dict[str, str]:
    """扫出条目里模板已登记的事实性数值，形如 `{"35.0℃": "template"}`。

    用 `UNIT_PATTERN` 扫 `callout / fact / impact / action` 四段（与 `R-PROVENANCE` /
    `R-NO-DATA` 同一张正则，避免两处口径漂移），命中的"数值加单位"即视为模板已写进事实的
    数字。值固定为 `"template"` 标记：规则只关心 token 是否被登记，不关心来源路径。
    """
    text = " ".join((item.callout, item.fact, item.impact, item.action))
    tokens: dict[str, str] = {}
    for match in UNIT_PATTERN.finditer(text):
        tokens[f"{match.group(1)}{match.group(2)}"] = _TEMPLATE_SOURCE
    return tokens


def _load_redlines(process_id: str, config_version: int) -> LoadedRedlines:
    """取该流程的红线并校验；配置取不到时构造"不可用"，不抛错中断校验。

    配置读取失败（文件缺失、YAML 错误、完整性校验失败）一律折算为 `LoadedRedlines.unavailable`
    ——它把"不可用"表达成一种正常结果，让引擎按 `process_redlines_loaded=False` 走 block 分支，
    而不是在文案校验环节抛异常。`config_version` 在配置取不到时无从得知，用调用方传入的版本号。
    """
    pid = ConfigProcessId(process_id)
    try:
        config = get_process_config(pid)
    except Exception:  # noqa: BLE001 —— 配置不可用按 fail-closed 处置，不中断校验
        return LoadedRedlines.unavailable(pid, config_version, "流程配置不可用")
    return RedlineValidator().validate(config)


async def gate_check(
    db: AsyncSession,
    user_id: str,
    items: Sequence[RenderedItem],
    *,
    process_id: str,
    config_version: int,
    today: date,
    session_id: str | None = None,
) -> list[PromptItem]:
    """对每条提示文案跑闸门三条规则，返回通过校验的 `PromptItem`。

    `decision != "allow"` 的条目整条丢弃（不出现在返回值里），并为它写一行 `gate_audit`；
    `allow` 的条目原样返回其 `PromptItem`，调用方据此落库。

    `db` 与 `user_id` 用于与上层链路保持同一签名：本函数不写业务表、不查库，审计走的
    `GateAuditRepository` 自管事务（见 `app/repositories/gate_audit.py`），故 `db` 本体内不用。
    `session_id` 缺省时生成一个，仅用于审计归属字段。
    """
    gate_process_id = GateProcessId(process_id)
    sid = session_id or uuid4().hex
    redlines = _load_redlines(process_id, config_version)
    knowledge = _NoKnowledge()
    engine = GateEngine(rules=build_rules(_CHECKED_RULE_IDS))
    audit = GateAuditWriter(GateAuditRepository())

    allowed: list[PromptItem] = []
    for rendered in items:
        prescription = GatePrescription(
            diagnosis_summary=rendered.item.fact,
            facts_provenance=dict(rendered.item.facts_provenance),
        )
        ctx = GateContext(
            session_id=sid,
            user_id=user_id,
            process_id=gate_process_id,
            config_version=config_version,
            prescription=prescription,
            payload=GatePayload(rendered_text=rendered.text),
            redlines=redlines,
            knowledge=knowledge,
            today=today,
            numeric_tokens=_registered_tokens(rendered.item),
        )
        result = engine.evaluate(ctx)
        if result.decision == GateDecision.ALLOW.value:
            allowed.append(rendered.item)
            continue
        await audit.record(prescription, ctx, result, shadowed=False)
    return allowed


__all__ = ["RenderedItem", "gate_check"]

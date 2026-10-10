"""文案生成：模板出事实、模型只改写（T3-07，技术方案 11.1 的 REWRITE 节点与 10.3 降级链）。

三条函数串起"结构化条目 → 农户可读文案"：

- `render_template(item)`：模板原文。零模型模式下的最终文案，也是模型改写的对照基线。
- `rewrite_with_model(item, template_text, max_chars)`：模型口语化改写，失败即返回模板原文，
  返回 `(文本, 是否降级)`。
- `build_text(item)`：两者的组合入口，先出模板原文、再尝试改写、最后按上限截断。T3-08 的
  快照生成链路直接 `await build_text(item)` 取回最终文案。

**降级是默认语义，成功是例外**：只有拿到"非空且事实完整"的改写结果才认为成功，其余所有
情况（异常、超时、空响应、事实丢失、网关熔断）统一返回模板原文并把降级标记为 `True`。
这条与产品说明书 6.3 兜底三"不允许整体页面不可用"一致——快照文案降级为模板原文即可，
提示条目的数量、级别、执行勾选、触达能力全部不受影响。

**模板必须本身可读**：`render_template` 产出的句子在零模型模式下就是交付物，故 `rewrite_*`
一旦失败就退回它，而不是退回"直接拼四段"的残句。

**事实保全由代码校验而不是靠模型自觉**：`_facts_preserved` 反向扫描 `item` 四段里的全部
数字，断言改写结果中逐一存在。数字丢失或数字被改是必须拦截的错误（产品说明书 6.4 红线四
"不出现无数据支撑的数字"），不是文风问题。校验思路与闸门 `R-PROVENANCE` 一致（反向扫描），
但难度更低，因为基线是可控的模板原文。
"""

import asyncio
import re

from app.broadcast.limits import MAX_ITEM_CHARS, fit_item_text
from app.broadcast.prompts.snapshot_writer import REWRITE_SYSTEM_PROMPT
from app.broadcast.rules.base import RuleResult
from app.broadcast.rules.templates import render_template
from app.core.config import get_settings
from app.llm import ModelGateway
from app.llm.enums import TaskType

# 改写调用的超时（秒）。短于常规 LLM 调用的 30 秒：这是纯后台任务、与农户交互无关，
# 等待过长不如直接降级。与 T3-08 的锁超时（30 秒）配套——一次改写最多占 8 秒。
REWRITE_TIMEOUT = 8.0

# 文案默认字数上限。取自流程配置 `output_template.max_chars` 的缺省值；T4-05 会在此基础上
# 叠加快照文案的适老化上限并取较小值。仅靠提示词约束长度不可靠，必须在返回前强制截断。
DEFAULT_MAX_CHARS = 400

# 降级标记的落库形态：写入 `PromptItem.facts_provenance`，供灰度期统计"改写成功率"
# 与评估"改写好还是不改写好"。
WRITER_MODEL = "writer:model"
WRITER_TEMPLATE = "writer:template"

# 从四段中抽取的数字（含小数）。用于反向校验事实保全——数字必须原样出现在改写结果里。
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# 句子边界标点。截断时向前找最近的句末标点，避免产出"及时灌溉，避免作"这类断句。
_SENTENCE_END = "。！？；;.!?"


def _facts_preserved(item: RuleResult, text: str) -> bool:
    """改写结果是否保留了 `item` 四段里的全部数字（事实保全的代码校验）。

    基线是模板原文里的数字：`item` 的 `fact` 由模板填入实测值与阈值（如 `36.0`、
    `35.0`），农户照此判断严重程度。数字丢失或被改必须拦截——返回 `False` 即降级为模板
    原文，绝不把改错数字的文案交给农户。

    两侧都按数字 token（`\\d+(?:\\.\\d+)?`）抽取后比对集合，而不是子串包含：后者会让
    `"3"` 被 `"35.0"` 包含而误判为"已保留"。模型若把 `35.0` 改写成 `35`，集合比对会判
    不通过而降级——保守方向正确。
    """
    expected = set(_NUMBER.findall(" ".join((item.callout, item.fact, item.impact, item.action))))
    found = set(_NUMBER.findall(text))
    return expected <= found


def truncate_at_sentence(text: str, max_chars: int) -> str:
    """按句子边界截断到 `max_chars` 以内；无句末标点才硬切。

    硬切会产出"及时灌溉，避免作"这类断句，农户读到残缺句子会怀疑内容未加载完而反复刷新
    （适老化目标）。因此先在 `max_chars` 附近向前找最近的句末标点，找不到才按字符数切。
    保证返回长度 ≤ `max_chars`。
    """
    text = text.strip()
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    idx = max((head.rfind(ch) for ch in _SENTENCE_END), default=-1)
    return head[: idx + 1] if idx >= 0 else head


async def rewrite_with_model(
    item: RuleResult,
    template_text: str,
    max_chars: int,
    *,
    gateway: ModelGateway | None = None,
) -> tuple[str, bool]:
    """模型改写，失败即返回模板原文。返回 `(文本, 是否降级)`。

    零模型模式（`ZERO_MODEL_MODE=true`）下不发起任何调用，直接 `(模板原文, True)`。
    `USE_MOCK_LLM=true` 时不为此写单独分支：mock 响应通过事实校验即视为正常改写、
    不通过即降级，CI 因而能同时覆盖"改写路径"与"降级路径"。

    网关的 `chat(task_type, messages, schema)` **没有** `timeout` 参数（超时由适配器以
    `AsyncClient(timeout=...)` 施加），故这里用 `asyncio.wait_for` 在调用侧兜一层
    `REWRITE_TIMEOUT`，做法与 `panel.py` 一致。
    """
    if get_settings().ZERO_MODEL_MODE:
        return template_text, True
    try:
        if gateway is None:
            from app.llm.factory import get_gateway

            gateway = get_gateway()
        messages = [
            {
                "role": "user",
                "content": REWRITE_SYSTEM_PROMPT.format(template_text=template_text, max_chars=max_chars),
            }
        ]
        response = await asyncio.wait_for(
            gateway.chat(TaskType.SNAPSHOT_WRITER, messages, None),
            timeout=REWRITE_TIMEOUT,
        )
        text = (response.content or "").strip()
        if not text or not _facts_preserved(item, text):
            return template_text, True  # 改写失败或丢事实 → 用模板原文
        return truncate_at_sentence(text, max_chars), False
    except Exception:  # noqa: BLE001  降级是默认语义：任何失败都回落到模板原文
        return template_text, True


async def build_text_with_provenance(
    item: RuleResult,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    gateway: ModelGateway | None = None,
) -> tuple[str, str]:
    """出文案并给出降级来源标记。返回 `(文本, WRITER_MODEL | WRITER_TEMPLATE)`。

    降级标记由消费方（T3-08）写入 `PromptItem.facts_provenance`。单独成一个入口而不塞进
    `build_text` 的返回值，是为了让"只要文案"的调用点保持简单。

    两条路径都在此处统一按 `max_chars` 截断：模板原文也可能超长，只在提示词里约束长度是
    不可靠的，必须在返回前于代码里兜住（T3-07 第四节）。
    """
    template_text = render_template(item)
    text, degraded = await rewrite_with_model(item, template_text, max_chars, gateway=gateway)
    return truncate_at_sentence(text, max_chars), WRITER_TEMPLATE if degraded else WRITER_MODEL


async def build_text(
    item: RuleResult,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_item_chars: int = MAX_ITEM_CHARS,
    gateway: ModelGateway | None = None,
) -> str:
    """组合入口：模板出事实 → 模型改写 → 按上限截断 → 逐段收敛，返回最终文案（T3-08 调用）。

    T4-05 在此叠加适老化上限：`build_text` 的输出对应"整条提示的可见总长"，故有效上限取
    流程配置的 `max_item_chars` 与默认 `MAX_ITEM_CHARS` 的**较小值**，再与 `max_chars`
    取小。`fit_item_text` 负责按句子边界收敛到该上限，绝不返回半句。
    """
    text, _ = await build_text_with_provenance(item, max_chars=max_chars, gateway=gateway)
    return fit_item_text(text, min(max_chars, max_item_chars))


__all__ = [
    "DEFAULT_MAX_CHARS",
    "REWRITE_TIMEOUT",
    "WRITER_MODEL",
    "WRITER_TEMPLATE",
    "build_text",
    "build_text_with_provenance",
    "fit_item_text",
    "render_template",
    "rewrite_with_model",
    "truncate_at_sentence",
]

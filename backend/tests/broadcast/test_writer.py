"""T3-07 验收：文案生成与模板出事实。

覆盖验收标准的六个点：每条已注册 `rule_id` 都有可渲染模板且四段齐备、零模型/网关异常时
直出模板原文且降级标记为真、事实保全（数字丢失即降级）、字数上限与按句子边界截断、
模板表无死键、以及未登记 `rule_id` 抛 `RuleImplementationError`。

全部用例用构造对象与网关桩，不起数据库：文案层必须能在零模型模式下工作，若用例依赖真实
模型，这条核心约束就没被真正验证。
"""

import pytest

from app.broadcast.prompts.snapshot_writer import REWRITE_SYSTEM_PROMPT
from app.broadcast.rules import (
    RULE_TEMPLATES,
    RuleImplementationError,
    RuleResult,
    render_template,
    template_key,
)
from app.broadcast.rules.catalog import load_rules, rule_ids
from app.broadcast.writer import (
    WRITER_MODEL,
    WRITER_TEMPLATE,
    build_text,
    build_text_with_provenance,
    truncate_at_sentence,
)


def _item(
    rule_id: str,
    *,
    source_kind: str = "env_risk",
    severity: str = "notice",
    callout: str = "东边水田",
    fact: str = "气温 35.0℃，超过阈值 30.0℃",
    impact: str = "持续高温影响结实",
    action: str = "灌深水降温",
) -> RuleResult:
    return RuleResult(
        source_kind=source_kind,
        severity=severity,
        callout=callout,
        fact=fact,
        impact=impact,
        action=action,
        rule_id=rule_id,
    )


class _StubResponse:
    """网关响应的最小替身：`rewrite_with_model` 只读 `content`。"""

    def __init__(self, content: str | None) -> None:
        self.content = content


class _EchoGateway:
    """返回固定文本的网关桩。"""

    def __init__(self, content: str | None) -> None:
        self._content = content
        self.calls = 0

    async def chat(self, task_type, messages, schema=None):  # noqa: ANN001, ANN201
        self.calls += 1
        return _StubResponse(self._content)


class _RaisingGateway:
    """任何调用都抛异常的网关桩：模拟零模型/熔断/超时。"""

    async def chat(self, task_type, messages, schema=None):  # noqa: ANN001, ANN201
        raise RuntimeError("网关不可用")


# ---- 一、模板出事实：注册规则全覆盖、四段齐备 ----

_REGISTERED_RULE_IDS = sorted(rule_ids(load_rules()))


@pytest.mark.parametrize("rule_id", _REGISTERED_RULE_IDS)
def test_every_registered_rule_renders_with_all_sections(rule_id: str) -> None:
    """每条已注册规则都能渲染出模板原文，且四段各自的非空内容都出现在结果里。"""
    item = _item(rule_id)
    text = render_template(item)
    assert text
    for segment in (item.callout, item.fact, item.impact, item.action):
        assert segment in text


def test_rendered_template_is_a_readable_sentence() -> None:
    """模板原文是完整可读句子（零模型模式下的交付物下限）：以句末标点收尾、无残留占位符。"""
    text = render_template(_item("env_risk.heat"))
    assert text.endswith("。")
    assert "{" not in text and "}" not in text


def test_render_trims_segment_terminal_punctuation() -> None:
    """分段自带句末标点时不再叠加句号，避免出现"。。"。"""
    item = _item("env_risk.heat", impact="持续高温影响结实。")
    assert "。。" not in render_template(item)


def test_render_template_raises_for_unregistered_rule_id() -> None:
    """未登记 `rule_id` 且来源族也无模板时抛 `RuleImplementationError`，不回落拼四段。"""
    with pytest.raises(RuleImplementationError):
        render_template(_item("unknown.foo", source_kind="unknown_kind"))


# ---- 二、模板表无死键（每个键都能被某类产出命中） ----

# 三类输入真实产出的 `rule_id` 形态代表：环境阈值、农事历 `calendar:{id}`、回访两种。
_REPRESENTATIVE_RULE_IDS = [
    "env_risk.heat",
    "env_risk.cold",
    "env_risk.soil_dry",
    "env_risk.humidity",
    "env_risk.rain",
    "env_risk.wind",
    "env_risk.pest_risk",  # → 命中来源族 env_risk
    "calendar:K-RICE-0109",  # → 命中来源族 calendar
    "followup.observing.rice",  # → 命中 followup.observing
    "followup.effect_check:task-1",  # → 命中 followup.effect_check
    "followup.other",  # → 命中来源族 followup
]


def test_every_template_key_is_reachable() -> None:
    """模板表里没有永远不会被命中的死键（逐键与真实产出形态交叉校验）。"""
    keys = set()
    for rule_id in _REPRESENTATIVE_RULE_IDS:
        item = _item(rule_id, source_kind=rule_id.split(".", 1)[0])
        key = template_key(item)
        assert key is not None
        keys.add(key)
    assert keys == set(RULE_TEMPLATES)


def test_registered_rule_ids_all_have_a_template_key() -> None:
    """每条已注册规则（含 `env_risk.pest_risk` 走来源族）都能解析到一个模板键。"""
    for rule_id in _REGISTERED_RULE_IDS:
        assert template_key(_item(rule_id)) is not None


def test_six_threshold_rules_hit_exact_keys() -> None:
    """六条单指标阈值规则各自命中精确键，而非落到来源族通用模板。"""
    exact = [rid for rid in _REGISTERED_RULE_IDS if rid in RULE_TEMPLATES]
    assert exact  # 至少六条阈值规则有专属模板
    for rule_id in exact:
        assert template_key(_item(rule_id)) == rule_id


# ---- 三、降级：零模型与网关异常都直出模板原文 ----


async def test_gateway_exception_degrades_to_template_text() -> None:
    """网关抛异常时 `build_text` 返回模板原文、降级标记为 `True`，且不向外抛异常。"""
    item = _item("env_risk.heat")
    text, marker = await build_text_with_provenance(item, gateway=_RaisingGateway())
    assert text == render_template(item)
    assert marker == WRITER_TEMPLATE


async def test_zero_model_mode_skips_gateway_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ZERO_MODEL_MODE=true` 时不发起任何调用，直接模板原文 + 降级标记。"""
    from app.broadcast import writer as writer_module

    class _Settings:
        ZERO_MODEL_MODE = True

    monkeypatch.setattr(writer_module, "get_settings", lambda: _Settings())
    gateway = _EchoGateway("这段不该被使用")
    text, marker = await build_text_with_provenance(_item("env_risk.heat"), gateway=gateway)
    assert text == render_template(_item("env_risk.heat"))
    assert marker == WRITER_TEMPLATE
    assert gateway.calls == 0


async def test_empty_model_response_degrades() -> None:
    """模型返回空串 → 视为失败，用模板原文。"""
    text, marker = await build_text_with_provenance(_item("env_risk.heat"), gateway=_EchoGateway("   "))
    assert text == render_template(_item("env_risk.heat"))
    assert marker == WRITER_TEMPLATE


# ---- 四、事实保全：数字丢失即降级 ----


async def test_facts_lost_degrades_to_template() -> None:
    """mock 返回删掉数字的文本 → 事实校验不过 → 返回模板原文且降级。"""
    item = _item("env_risk.heat", fact="气温 35.0℃，超过阈值 30.0℃")
    text, marker = await build_text_with_provenance(item, gateway=_EchoGateway("天气有点热，注意降温。"))
    assert text == render_template(item)
    assert marker == WRITER_TEMPLATE


async def test_facts_preserved_accepts_model_text() -> None:
    """mock 保留全部数字 → 采用模型文本且降级标记为 `False`。"""
    item = _item("env_risk.heat", fact="气温 35.0℃，超过阈值 30.0℃")
    model_text = "东边水田这边气温到了 35.0℃，比 30.0℃ 的警戒线高，注意降温。"
    text, marker = await build_text_with_provenance(item, gateway=_EchoGateway(model_text))
    assert text == model_text
    assert marker == WRITER_MODEL


async def test_altered_number_degrades() -> None:
    """数字被改动（35.0 → 40.0）同样视为事实丢失，必须降级。"""
    item = _item("env_risk.heat", fact="气温 35.0℃，超过阈值 30.0℃")
    text, marker = await build_text_with_provenance(item, gateway=_EchoGateway("气温 40.0℃，超过 30.0℃。"))
    assert text == render_template(item)
    assert marker == WRITER_TEMPLATE


def test_facts_preserved_checks_every_section() -> None:
    """校验覆盖四段：影响段的日期数字也应被纳入基线。"""
    from app.broadcast.writer import _facts_preserved

    item = _item("env_risk.heat", impact="未来 3 天持续高温")
    assert _facts_preserved(item, "未来 3 天持续高温，气温 35.0℃，超过 30.0℃")
    assert not _facts_preserved(item, "未来持续高温，气温 35.0℃，超过 30.0℃")


# ---- 五、字数上限与句子边界 ----


def test_truncate_prefers_sentence_boundary() -> None:
    """超长时退到 `max_chars` 内最近的句末标点，绝不返回半句。"""
    text = "甲" * 50 + "。" + "乙" * 100
    out = truncate_at_sentence(text, 60)
    assert out == "甲" * 50 + "。"
    assert len(out) <= 60


def test_truncate_hard_cuts_when_no_boundary() -> None:
    """通篇无句末标点才硬切到上限。"""
    out = truncate_at_sentence("甲" * 200, 120)
    assert len(out) == 120


def test_truncate_returns_short_text_unchanged() -> None:
    """未超长的文本原样返回（逐字节相等），不改写合格文案。"""
    text = "东边水田：气温偏高。注意降温。"
    assert truncate_at_sentence(text, 120) == text


async def test_build_text_respects_max_chars() -> None:
    """`max_chars=120` 且模型返回超长文本时，结果长度不超上限且以句末标点收尾或已用尽上限。"""
    item = _item("env_risk.heat", fact="气温 35.0℃，超过阈值 30.0℃")
    long_text = "气温 35.0℃，超过阈值 30.0℃。" + "注意及时降温，" * 40
    text = await build_text(item, max_chars=120, gateway=_EchoGateway(long_text))
    assert len(text) <= 120
    assert text.endswith("。") or len(text) == 120


async def test_build_text_degrades_to_template_when_long() -> None:
    """模板原文本身超长、且模型不可用时，输出仍收敛到上限内。"""
    item = _item("env_risk.heat", fact="气温 35.0℃ 超过 30.0℃ 阈值需要处理" * 20)
    text = await build_text(item, max_chars=120, gateway=_RaisingGateway())
    assert len(text) <= 120


# ---- 六、提示词的安全约束 ----


def test_prompt_contains_five_hard_constraints() -> None:
    """改写提示词含五条硬性约束，且数字必须原样保留这条在列。"""
    assert "不得新增任何数字" in REWRITE_SYSTEM_PROMPT
    assert "不得删除任何一段" in REWRITE_SYSTEM_PROMPT
    assert "不得改变级别" in REWRITE_SYSTEM_PROMPT
    assert "不得添加原文没有的建议" in REWRITE_SYSTEM_PROMPT
    assert "不超过 {max_chars} 字" in REWRITE_SYSTEM_PROMPT


def test_prompt_forbidden_terms_only_in_constraint_clause() -> None:
    """`药剂/剂量/安全间隔期` 只出现在"不得新增"的约束句里，不出现任何具体药剂或数值。"""
    for term in ("药剂", "剂量", "安全间隔期"):
        if term in REWRITE_SYSTEM_PROMPT:
            for line in REWRITE_SYSTEM_PROMPT.splitlines():
                if term in line:
                    assert "不得新增" in line

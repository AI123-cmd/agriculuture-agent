"""T4-05 验收：提示文案的字数上限与按句子边界收敛。

两条约束都必须"能被跑出来"：字数（本文件）与选项数（`tests/contracts/test_interview_option_limit.py`）。
全部用例用构造对象与网关桩，不起数据库、不依赖模型——适老化上限是纯代码逻辑，
零模型模式（`USE_MOCK_LLM=true`）下也必须成立。
"""

from app.broadcast.limits import (
    MAX_ACTION_CHARS,
    MAX_CALLOUT_CHARS,
    MAX_FACT_CHARS,
    MAX_IMPACT_CHARS,
    MAX_ITEM_CHARS,
    fit_item_text,
)
from app.broadcast.rules import RuleResult
from app.broadcast.writer import build_text


class _StubResponse:
    """网关响应的最小替身：`rewrite_with_model` 只读 `content`。"""

    def __init__(self, content: str) -> None:
        self.content = content


class _RaisingGateway:
    """任何调用都抛异常的网关桩：等价于零模型/熔断，强制走模板原文。"""

    async def chat(self, task_type, messages, schema=None):  # noqa: ANN001, ANN201
        raise RuntimeError("网关不可用")


class _EchoGateway:
    """返回固定文本的网关桩，用于构造"模型返回超长文案"的场景。"""

    def __init__(self, content: str) -> None:
        self._content = content

    async def chat(self, task_type, messages, schema=None):  # noqa: ANN001, ANN201
        return _StubResponse(self._content)


def _item(*, fact: str = "气温 35.0℃，超过阈值 30.0℃") -> RuleResult:
    return RuleResult(
        source_kind="env_risk",
        severity="notice",
        callout="东边水田",
        fact=fact,
        impact="持续高温影响结实",
        action="灌深水降温",
        rule_id="env_risk.heat",
    )


# ---- 一、逐段常量：和为总上限，保证"逐段不超即总长不超" ----


def test_segment_constants_sum_to_item_limit() -> None:
    assert MAX_CALLOUT_CHARS + MAX_FACT_CHARS + MAX_IMPACT_CHARS + MAX_ACTION_CHARS == MAX_ITEM_CHARS


# ---- 二、`fit_item_text`：合格文案不改写、超长按句末收敛 ----


def test_short_text_returned_unchanged() -> None:
    """未超长的文本原样返回（逐字节相等），证明函数不改写合格文案。"""
    text = "东边水田：气温偏高。注意降温。"
    assert fit_item_text(text, MAX_ITEM_CHARS) == text


def test_over_length_retreats_to_sentence_end() -> None:
    """中段有句号时退到该句号，绝不返回半句。"""
    text = "甲" * 50 + "。" + "乙" * 100
    out = fit_item_text(text, MAX_ITEM_CHARS)
    assert out == "甲" * 50 + "。"
    assert out.endswith("。")
    assert len(out) <= MAX_ITEM_CHARS


def test_no_sentence_end_hard_cuts_to_limit() -> None:
    """通篇无句末标点才硬切到上限，长度恰好等于上限。"""
    out = fit_item_text("甲" * 200, MAX_ITEM_CHARS)
    assert len(out) == MAX_ITEM_CHARS


# ---- 三、`build_text` 集成：模板与模型两条路径都收敛到上限内 ----


async def test_build_text_converges_oversized_fact_to_sentence_end() -> None:
    """`fact` 为 80 字且中段有句号的条目：收敛后不超 100 字且以句末标点结尾。"""
    item = _item(fact="甲" * 38 + "。" + "乙" * 41)
    text = await build_text(item, gateway=_RaisingGateway())
    assert len(text) <= MAX_ITEM_CHARS
    assert text.endswith("。")


async def test_build_text_zero_model_still_converges(monkeypatch) -> None:
    """`USE_MOCK_LLM=true`（零模型）下 mock 返回超长文案，收敛逻辑仍生效、不抛异常。"""
    from app.broadcast import writer as writer_module

    class _Settings:
        ZERO_MODEL_MODE = True

    monkeypatch.setattr(writer_module, "get_settings", lambda: _Settings())
    item = _item()
    text = await build_text(item, gateway=_EchoGateway("这段模型文本在零模型模式下不会被使用"))
    assert len(text) <= MAX_ITEM_CHARS


async def test_build_text_long_model_output_converges() -> None:
    """模型返回超长文案时，`build_text` 结果不超过单条上限。"""
    item = _item(fact="气温 35.0℃，超过阈值 30.0℃")
    long_text = "气温 35.0℃，超过阈值 30.0℃。" + "注意及时降温，" * 40
    text = await build_text(item, gateway=_EchoGateway(long_text))
    assert len(text) <= MAX_ITEM_CHARS

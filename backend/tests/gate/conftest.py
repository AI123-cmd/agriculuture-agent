"""闸门测试的共用夹具。

闸门规则的输入全部来自 `GateContext`，因此这里把构造器集中到 `tests/gate/fixtures/`：
`fake_knowledge.py` 提供 `KnowledgeFacts`/`LoadedRedlines` 假实现，`factories.py` 提供处方与
用药史构造器。conftest 只做再导出与 fixture 包装，避免同一套脚手架写两遍。
"""

from datetime import date

import pytest

from app.gate.contracts import GateContext
from tests.gate.fixtures.factories import gate_context, prescription
from tests.gate.fixtures.fake_knowledge import (
    FakeEntry,
    FakeKnowledge,
    loaded_redlines,
    unavailable_redlines,
)

__all__ = [
    "FakeEntry",
    "FakeKnowledge",
    "gate_context",
    "loaded_redlines",
    "prescription",
    "unavailable_redlines",
]


@pytest.fixture
def ctx() -> GateContext:
    return gate_context()


# `date` 是既有测试文件按需引用的符号，保留在此便于 `from tests.gate.conftest import date`。
_ = date

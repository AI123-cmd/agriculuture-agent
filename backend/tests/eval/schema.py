"""离线评测集 Schema（T6-01，技术方案 14.1）。

`EvalCase` / `EvalExpectation` 两个模型**逐字对齐**技术方案 14.1，不得改名、不得增删字段：
字段就是评测口径，改名等于换了一套判定标准。`EvalInputs` 与 `EvalResult` 是本项补充
（14.1 未给全），前者承载"症状输入、管理记录、环境读数"三类，后者是运行器从图终态抽取出的
可比对中间量。

**口径差异记录**：技术方案 14.1 的断言骨架写的是 `case.expect.process`，而 `EvalExpectation`
的字段名是 `expect_process`。本模块以**字段定义为准**，断言统一用 `case.expect.expect_process`；
方案骨架里的 `case.expect.process` 是笔误。刻意**不新增** `process` 别名属性——别名会让两种
写法都能跑，反而掩盖真实字段名，后续契约漂移无法被发现。

**与生产契约的对接**：`EvalResult.conclusion_key` 对应生产 `DiagnosisResult.conclusion`，
`EvalResult.gate.rules_hit` 对应生产 `GateResult.checked_rules`。此处用评测层的镜像字段名
（文档口径），映射在 `runner.extract_result` 的抽取层完成，生产契约与 DB 字段名一概不动。
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.agent.state import ProcessId

# 评测集目录：`cases/{process_id}.json` 按流程分集（14.1"七条流程各自成集"）。
CASES_DIR = Path(__file__).resolve().parent / "cases"

# 引用来源枚举（技术方案附录 A.1）。`EvalResult.sources` 是用它做的集合，`allowed_sources`
# 的默认三值即用户可见的三类依据；`model_inference` 不在默认集内——模型推断不作为"允许
# 引用来源"的默认项，需要它的流程必须显式声明。
SourceType = Literal["process_config", "knowledge_base", "env_reading", "model_inference"]


class EvalCaseLoadError(ValueError):
    """评测集文件加载失败。

    刻意不用裸 `JSONDecodeError` / `ValidationError`：评测集是人工维护的数据文件，一次
    失败必须直接指出"哪个文件、哪道题、哪个字段"，否则维护者要逐题二分查找。消息里因此
    固定带文件名与 `case_id`。
    """

    def __init__(self, path: Path, case_id: str, reason: str) -> None:
        self.path = path
        self.case_id = case_id
        super().__init__(f"评测集加载失败：文件 {path.name}，用例 {case_id}，原因：{reason}")


class EvalInputs(BaseModel):
    """评测输入，与 7.3 主图入参同构（14.1 未给定义，本项补充）。"""

    model_config = ConfigDict(extra="forbid")

    message: str  # 农户首轮表述
    answers: dict[str, str] = {}  # 预置的各轮追问答案（按 slot_key）
    management: dict[str, str] = {}  # 施肥/用药/灌溉/天气
    env_reading: list[dict] = []  # 环境读数（无设备则空）


class EvalExpectation(BaseModel):
    """评测期望。逐字对齐技术方案 14.1，不得改名、不得增删字段。"""

    model_config = ConfigDict(extra="forbid")

    # 1. 受理分流必须正确（对应 2.2 缺口一）
    expect_process: ProcessId
    # 2. 问诊必须问到的槽位（漏问即失败）
    must_ask_slots: list[str] = []
    # 3. 结论允许的取值集合（不要求逐字一致）。**无默认值**：不给允许集合等于放弃断言。
    allowed_conclusions: list[str]
    # 4. 期望触发的闸门规则（空集表示不应拦截）
    expect_gate_rules: list[str] = []
    # 5. 禁止出现的表述（安全红线，出现即失败）
    forbidden_phrases: list[str] = []
    # 6. 引用来源必须落在允许集合内
    allowed_sources: list[str] = ["process_config", "knowledge_base", "env_reading"]


class EvalCase(BaseModel):
    """一道评测题。逐字对齐技术方案 14.1。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    process_id: ProcessId  # 属于哪条流程
    scene: str  # 场景描述，如"茶园高温干旱"
    inputs: EvalInputs
    expect: EvalExpectation


class EvalGate(BaseModel):
    """闸门结果的**评测层镜像**：只保留断言需要的字段。

    生产 `GateResult` 用 `checked_rules`，评测口径叫 `rules_hit`（技术方案 14.1）。两者
    只是命名差异，映射在抽取层完成。此处不直接复用生产契约，是为了让评测断言独立于生产
    字段改名——生产契约换名字时，只需改抽取层的映射，评测集内容不动。
    """

    model_config = ConfigDict(extra="forbid")

    passed: bool
    decision: Literal["allow", "degrade", "block"]
    rules_hit: list[str] = []
    violations: list[dict] = []


class EvalResult(BaseModel):
    """运行器从图终态抽取出的可比对中间量（14.1 未给定义，本项补充），不是业务对象。"""

    model_config = ConfigDict(extra="forbid")

    process_id: ProcessId
    asked_slots: list[str]  # 图过程中问出的所有槽位
    conclusion_key: str  # 映射生产 DiagnosisResult.conclusion
    gate: EvalGate  # 闸门结果（含 rules_hit）
    final_text: str  # 定稿文本
    sources: list[str]  # 引用的 SourceType 集合
    rounds: int  # 实际轮次（供 14.3 一致口径）
    node_path: list[str]  # 访问过的节点顺序（供编排回归）


def load_cases(process_id: ProcessId, *, cases_dir: Path | None = None) -> list[EvalCase]:
    """按流程分集加载评测题。

    - `cases/{process_id}.json` 不存在或 `cases[]` 为空：返回空列表且不报错（T6-01 交付时
      内容尚未填充，14.1 要求七条流程可各自为空）。
    - 文件不是合法 JSON、或某道题不满足 Schema：抛 `EvalCaseLoadError`，消息带**文件名**与
      **case_id**，而不是裸 `JSONDecodeError` / `ValidationError`。
    """
    directory = cases_dir if cases_dir is not None else CASES_DIR
    path = directory / f"{process_id.value}.json"
    if not path.exists():
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvalCaseLoadError(path, "<文件级>", f"不是合法 JSON：{exc}") from exc

    if not isinstance(raw, dict):
        raise EvalCaseLoadError(path, "<文件级>", "顶层必须是对象，含 cases 数组")
    rows = raw.get("cases")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise EvalCaseLoadError(path, "<文件级>", "cases 必须是数组")

    cases: list[EvalCase] = []
    for row in rows:
        case_id = row.get("case_id") if isinstance(row, dict) else None
        label = str(case_id) if case_id else "<缺 case_id>"
        try:
            cases.append(EvalCase.model_validate(row))
        except ValidationError as exc:
            raise EvalCaseLoadError(path, label, str(exc)) from exc
    return cases


__all__ = [
    "CASES_DIR",
    "EvalCase",
    "EvalCaseLoadError",
    "EvalExpectation",
    "EvalGate",
    "EvalInputs",
    "EvalResult",
    "SourceType",
    "load_cases",
]

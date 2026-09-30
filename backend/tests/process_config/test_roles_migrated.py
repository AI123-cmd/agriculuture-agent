"""T2-61：现有 ai_router 迁移到流程配置的验收用例。

`app/ai_router/` 目录整体删除后，这里的断言分四组守住迁移成果：

1. 目录与引用清除：`app/ai_router/` 不存在，`app/` 与 `app/llm/` 下无残留导入；
2. 角色提示已迁入流程配置：`rice.yaml` 含"水稻"、`general.yaml` 含作物覆盖范围，
   且任何 `seeds/*.yaml` 都不含模型名（模型名归路由，进 YAML 即迁移错误）；
3. 烟草占位：不建 `tobacco.yaml`、`ProcessId` 不含 `tobacco`，留存文本仍在且标记待确认；
4. 替代就位：`TaskType.INTERVIEW`/`DIAGNOSE` 档位配置存在，且以 mock 模式发起一次
   `process_id=rice` 的问诊时，提示词里的角色定位来自 `rice.yaml` 而非代码。
"""

import re
from pathlib import Path
from typing import Any, cast

import pytest

from app.agent.agents.base import AgentContext
from app.agent.agents.interviewer import InterviewerAgent
from app.agent.budget import ConsultBudget
from app.agent.contracts import InterviewBatch
from app.agent.state import ConsultationState, ProcessId, SlotValue, TaskType
from app.llm import ModelGateway
from app.llm.enums import TaskType as LlmTaskType
from app.llm.providers.base import ModelResponse
from app.llm.tiers import DEFAULT_TIER_MAP
from app.process_config.loader import get_process_config, load_all_configs
from app.process_config.models import ProcessConfig

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent
SEEDS_DIR = BACKEND_ROOT / "app" / "process_config" / "seeds"
APP_DIR = BACKEND_ROOT / "app"
LLM_DIR = APP_DIR / "llm"
MIGRATION_DOC = REPO_ROOT / "docs" / "tasks" / "阶段二-流程与问诊重构" / "T2-61-烟草角色提示留存-待确认.md"

_MODEL_NAME = re.compile(r"(deepseek|gpt|claude|qwen|glm|gemini|ernie|moonshot|kimi)", re.IGNORECASE)


@pytest.fixture(scope="module")
def configs() -> dict[ProcessId, ProcessConfig]:
    return load_all_configs()


# ---- 第一组：目录与引用清除 ----


def test_ai_router_directory_deleted() -> None:
    assert not (APP_DIR / "ai_router").exists(), "app/ai_router/ 目录应已删除"


def test_no_ai_router_reference_in_app() -> None:
    """`app/` 下不得残留对已删除模块的导入（`api/v1/router.py` 的局部别名已改名，不再命中）。"""
    pattern = re.compile(r"app\.ai_router|from \.\.?ai_router|ai_router\.registry")
    offenders = [p for p in APP_DIR.rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))]
    assert not offenders, f"仍有残留引用：{offenders}"


def test_llm_has_no_reference_to_deleted_module() -> None:
    """直连逻辑已并入网关，`app/llm/` 不应再指向旧模块。"""
    offenders = [p for p in LLM_DIR.rglob("*.py") if "ai_router" in p.read_text(encoding="utf-8")]
    assert not offenders, f"app/llm 下仍有引用：{offenders}"


# ---- 第二组：角色提示已迁入流程配置 ----


def test_rice_role_definition_mentions_rice(configs: dict[ProcessId, ProcessConfig]) -> None:
    assert "水稻" in configs[ProcessId.RICE].role_definition


def test_general_role_definition_covers_crop_scope(configs: dict[ProcessId, ProcessConfig]) -> None:
    """原 `GeneralModel.system_role` 的覆盖范围（大田作物、经济作物、果蔬）不得在迁移中丢失。"""
    role = configs[ProcessId.GENERAL].role_definition
    assert all(token in role for token in ("大田作物", "经济作物", "果蔬"))


def test_no_seeds_role_definition_contains_model_name(configs: dict[ProcessId, ProcessConfig]) -> None:
    """模型名归路由，写进 `role_definition` 即迁移错误。"""
    for pid, config in configs.items():
        assert not _MODEL_NAME.search(config.role_definition), f"{pid.value}: {config.role_definition}"


def test_no_seeds_yaml_contains_model_name(configs: dict[ProcessId, ProcessConfig]) -> None:
    """更宽的一层：任何 `seeds/*.yaml` 全文都不得出现模型名。"""
    for path in SEEDS_DIR.glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        assert not _MODEL_NAME.search(text), f"{path.name} 含模型名"


# ---- 第三组：烟草占位 ----


def test_no_tobacco_yaml() -> None:
    assert not (SEEDS_DIR / "tobacco.yaml").exists(), "未确认前不新增烟草流程配置"


def test_process_id_has_no_tobacco_member() -> None:
    assert not hasattr(ProcessId, "TOBACCO")
    assert {p.value for p in ProcessId} == {"rice", "tea", "rapeseed", "chili", "weed", "pig", "general"}


def test_tobacco_prompt_retained_and_marked_pending() -> None:
    """原 `TobaccoModel.system_role` 文本必须留存，且标记待确认并引用产品说明书 A9。"""
    assert MIGRATION_DOC.exists(), f"缺少烟草留存文件：{MIGRATION_DOC}"
    text = MIGRATION_DOC.read_text(encoding="utf-8")
    assert "烟草" in text
    assert "花叶病" in text and "赤星病" in text and "黑胫病" in text
    assert "待确认" in text
    assert "A9" in text


# ---- 第四组：替代就位与端到端角色来源 ----


def test_interview_and_diagnose_tier_configured() -> None:
    """原 `model_id` 的替代：模型选择改由 `task_type` 决定，两个环节的档位必须存在。"""
    assert LlmTaskType.INTERVIEW in DEFAULT_TIER_MAP
    assert LlmTaskType.DIAGNOSE in DEFAULT_TIER_MAP


def _state(**overrides: Any) -> ConsultationState:
    state: ConsultationState = {
        "session_id": "s1",
        "user_id": "u1",
        "trace_id": "t-1",
        "round": 0,
        "slot_round": 0,
        "messages": [{"role": "user", "content": "我家水稻叶子上有斑点"}],
    }
    state.update(overrides)
    return state


class _FakeGateway:
    """回放预设结果的网关替身；记录最后一次调用收到的 messages。"""

    def __init__(self, response: ModelResponse) -> None:
        self._response = response
        self.last_messages: list[dict] = []

    async def chat(self, task_type: object, messages: list[dict], schema: object = None) -> ModelResponse:
        self.last_messages = [dict(m) for m in messages]
        return self._response


async def test_rice_consultation_prompt_carries_yaml_role() -> None:
    """以 mock 模式发起一次 `process_id=rice` 的问诊：提示词里的角色定位来自 `rice.yaml`。"""
    config = get_process_config(ProcessId.RICE)
    payload = {
        "questions": [
            {
                "slot_key": "rice_symptom_part",
                "text": "稻子的哪个部位出问题了",
                "why": "看清受害部位才能缩小范围",
                "options": ["叶片", "茎秆", "穗部", "根部"],
                "answer_type": "single",
            }
        ],
        "stage_hint": "请补充受害部位",
        "confidence_note": None,
    }
    response = ModelResponse(
        content="{}",
        parsed=InterviewBatch.model_validate(payload),
        model="mock",
        provider="mock",
        prompt_tokens=1,
        completion_tokens=1,
        latency_ms=1,
    )
    gateway = _FakeGateway(response)
    agent = InterviewerAgent(cast(ModelGateway, gateway), ConsultBudget())
    ctx = AgentContext(
        process_config=config,
        config_version=config.version,
        task_type=TaskType.INTERVIEW,
        trace_id="t-1",
        region_code="330000",
    )

    await agent.run(_state(), ctx)

    system = gateway.last_messages[0]["content"]
    assert "水稻" in system
    assert config.role_definition in system

"""T2-33 验收：问诊接口改为图驱动后的端到端行为。

覆盖点与验收标准一一对应：

1. 新会话（`sessionId` 为空）返回 200 且 `ChatResponse` 字段齐全；
2. 中断续问：首轮返回非空 `questions`，带 `sessionId` + `answers` 的第二次请求返回 200，
   且已答槽位不再被追问；
3. 转人工：养猪入口带报告信号时，状态码为 200（**不是** 4xx/5xx），
   `handoff.reason == "mandatory_report"`、`prescription is None`；
4. `environmentNote` 三态：`none` / `sensor_stale` 非空且含"暂无"或"基于"，`sensor` 为 None；
5. `processHint="weed"` 时 trace 跳过 `triage` 直达 `subgraph_weed`（受理即分流）；
6. 归属校验：拿另一用户的 `sessionId` 请求，读不到其会话内容（换新会话 id）。
"""

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import main as main_graph
from app.schemas.consult import ChatResponse
from app.services.ai_service import AIService, build_environment_note

# 技术方案 13.2 约定的 `ChatResponse` 字段名集合（camelCase）。
_EXPECTED_FIELDS = {
    "sessionId",
    "stage",
    "stageLabel",
    "progress",
    "reply",
    "questions",
    "prescription",
    "environmentNote",
    "handoff",
    "quotaPrompt",
    "degraded",
}


def _login_headers(client, code: str) -> dict:
    """用任意开发登录码换一组真实凭证（每次调用即一个独立用户）。"""
    resp = client.post("/api/login", json={"code": code})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['data']['token']}"}


# ---- 字段契约 ----


def test_chat_response_field_names_match_spec() -> None:
    """`ChatResponse` 字段名集合与技术方案 13.2 相等（多一个少一个都失败）。"""
    assert set(ChatResponse.model_fields.keys()) == _EXPECTED_FIELDS


# ---- 新会话 ----


def test_new_session_returns_full_contract(client, auth_headers) -> None:
    """`sessionId` 为空即新会话：返回 200 且字段齐全。"""
    resp = client.post("/api/ai/chat", json={"message": "我家水稻叶片发黄"}, headers=auth_headers)
    assert resp.status_code == 200, resp.text

    data = resp.json()["data"]
    assert set(data.keys()) == _EXPECTED_FIELDS
    assert data["sessionId"]
    assert data["stageLabel"]
    assert set(data["progress"].keys()) == {"current", "total"}
    assert data["sessionId"]


# ---- 中断续问 ----


def test_resume_does_not_reask_answered_slot(client, auth_headers) -> None:
    """首轮问、次轮带上答案续问：已答槽位不再出现在新一轮问题里。"""
    first = client.post("/api/ai/chat", json={"message": "我家水稻叶片发黄"}, headers=auth_headers)
    assert first.status_code == 200, first.text
    data1 = first.json()["data"]
    assert data1["questions"], "首轮应返回待答问题"

    session_id = data1["sessionId"]
    answered = data1["questions"][0]["slotKey"]

    second = client.post(
        "/api/ai/chat",
        json={
            "sessionId": session_id,
            "message": "按提示补充",
            "answers": [{"slotKey": answered, "value": "发黄", "status": "filled"}],
        },
        headers=auth_headers,
    )
    assert second.status_code == 200, second.text
    data2 = second.json()["data"]
    assert data2["sessionId"] == session_id
    assert data2["questions"], "槽位未齐备时应继续追问"
    asked_again = {row["slotKey"] for row in data2["questions"]}
    assert answered not in asked_again


# ---- 转人工（养猪报告守卫） ----


def test_pig_report_guard_hands_off_mandatory_report(client, auth_headers) -> None:
    """`processHint="pig"` + 报告信号：200 转人工，reason 为 mandatory_report，且无处方。"""
    resp = client.post(
        "/api/ai/chat",
        json={"processHint": "pig", "message": "昨天还好好的，今天突然死了一头"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text

    data = resp.json()["data"]
    assert data["handoff"] is not None
    assert data["handoff"]["reason"] == "mandatory_report"
    assert data["prescription"] is None


# ---- environmentNote 三态 ----


def test_environment_note_when_source_none() -> None:
    """无环境读数时必须给出提示文案（含"暂无"/"基于"）。"""
    note = build_environment_note({"observations": {"env_source": "none"}})
    assert note
    assert "暂无" in note or "基于" in note


def test_environment_note_when_source_stale() -> None:
    """传感器数据过期时同样必须给出提示文案。"""
    note = build_environment_note({"observations": {"env_source": "sensor_stale"}})
    assert note
    assert "暂无" in note or "基于" in note


def test_environment_note_defaults_to_none_source() -> None:
    """`observations` 缺失时 `env_source` 视作 none，仍要给提示。"""
    note = build_environment_note({})
    assert note
    assert "暂无" in note or "基于" in note


def test_environment_note_when_source_sensor() -> None:
    """有实时读数时不得再标注缺失，返回 None。"""
    assert build_environment_note({"observations": {"env_source": "sensor"}}) is None


# ---- 受理即分流：processHint 跳过 triage ----


async def _in_memory_checkpointer() -> InMemorySaver:
    return InMemorySaver()


async def test_process_hint_weed_skips_triage(monkeypatch) -> None:
    """`processHint="weed"` 直达 `subgraph_weed`，trace 里没有 `triage`。"""
    monkeypatch.setattr(main_graph, "get_checkpointer", _in_memory_checkpointer)
    graph = await main_graph.build_graph()

    state: dict[str, Any] = {
        "session_id": "trace-weed",
        "user_id": "u-trace-weed",
        "process_hint": "weed",
        "user_message": "地里杂草很多",
        "messages": [{"role": "user", "content": "地里杂草很多"}],
    }
    AIService._inject_config(state, "weed")

    trace: list[str] = []
    config = {"configurable": {"thread_id": "trace-weed"}}
    async for chunk in graph.astream(state, config, stream_mode="updates"):
        trace.extend(chunk.keys())

    assert trace[0] == "intake"
    assert trace[1] == "subgraph_router"
    assert "triage" not in trace
    assert "subgraph_weed" in trace


# ---- 归属校验 ----


def test_foreign_session_id_does_not_take_over(client) -> None:
    """用另一用户的 `sessionId` 请求：换发新会话 id，不接管原会话。"""
    headers_a = _login_headers(client, "chat-own-0001")
    headers_b = _login_headers(client, "chat-oth-0002")

    first = client.post("/api/ai/chat", json={"message": "我家水稻叶片发黄"}, headers=headers_a)
    assert first.status_code == 200, first.text
    session_a = first.json()["data"]["sessionId"]

    intruder = client.post(
        "/api/ai/chat",
        json={"sessionId": session_a, "message": "继续"},
        headers=headers_b,
    )
    assert intruder.status_code == 200, intruder.text
    assert intruder.json()["data"]["sessionId"] != session_a

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
from app.agent.state import ProcessId
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
    """`processHint="weed"` 直达 `subgraph_weed`，trace 里没有 `triage`。

    真实杂草子图会在 `interview` 节点 `interrupt`，此时 `stream_mode="updates"` 只发
    顶层键 `__interrupt__`（子图名不出现），故"进入哪个子图"用跑完后的 `process_id` 佐证。
    """
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
    # 受理即分流锁定杂草流程，问诊节点中断（`__interrupt__`），说明已进入杂草子图。
    assert "__interrupt__" in trace
    snapshot = await graph.aget_state(config)
    assert snapshot.values["process_id"] == ProcessId.WEED
    assert snapshot.next  # 线程停在问诊中断处，仍未跑完


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


# ---- 小程序作物模型入口：中文作物名作 processHint ----


def test_process_hint_crop_name_does_not_crash(client, auth_headers) -> None:
    """小程序「作物模型」按钮传的是中文作物名，接口不得因此 500。

    此前 `intake` 直接 `ProcessId("水稻")` 会抛 `ValueError`，只要农户选了作物就必然失败。
    断言只针对"不再崩"这一回归点：`reply` 与否取决于 mock LLM 的问诊产出，与本修复无关。
    """
    resp = client.post(
        "/api/ai/chat",
        json={"processHint": "水稻", "message": "叶片发黄"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["data"].keys()) == _EXPECTED_FIELDS


def test_process_hint_crop_name_locks_that_process(client, auth_headers) -> None:
    """中文作物名归一后应锁定对应流程，而非落到通用兜底。

    用「辣椒」验证：辣椒流程 `panel_enabled=true`，其问诊中断会带回待答问题；
    锁定成功即说明入口直达生效，而不是被 `general` 兜底吞掉。
    """
    resp = client.post(
        "/api/ai/chat",
        json={"processHint": "辣椒", "message": "叶子有斑点"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["questions"], "锁定辣椒流程后应进入问诊并带回待答问题"


def test_process_hint_unknown_crop_falls_back_without_error(client, auth_headers) -> None:
    """未登记专属流程的作物（如「番茄」「小麦」）不锁定流程，走分诊兜底且不报错。"""
    for hint in ("番茄", "小麦"):
        resp = client.post(
            "/api/ai/chat",
            json={"processHint": hint, "message": "叶子发黄"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, f"{hint}: {resp.text}"
        assert set(resp.json()["data"].keys()) == _EXPECTED_FIELDS

"""AI 对话链路测试：chat、history、delete。"""


def test_chat_returns_reply(client, auth_headers):
    resp = client.post(
        "/api/ai/chat",
        json={"message": "水稻分蘖期怎么施肥？", "sessionId": "sess-001"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["sessionId"] == "sess-001"
    assert data["reply"]


def test_history_after_chat(client, auth_headers):
    client.post(
        "/api/ai/chat",
        json={"message": "怎么防治稻飞虱？", "sessionId": "sess-001"},
        headers=auth_headers,
    )
    resp = client.get("/api/ai/history", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] >= 1
    assert any(item["id"] == "sess-001" for item in data["list"])


def test_delete_history(client, auth_headers):
    client.post(
        "/api/ai/chat",
        json={"message": "hello", "sessionId": "sess-del"},
        headers=auth_headers,
    )
    resp = client.delete("/api/ai/history/sess-del", headers=auth_headers)
    assert resp.status_code == 200

    resp = client.get("/api/ai/history", headers=auth_headers)
    data = resp.json()["data"]
    assert all(item["id"] != "sess-del" for item in data["list"])


def test_consult_detail_returns_messages(client, auth_headers):
    client.post(
        "/api/ai/chat",
        json={"message": "玉米叶子发黄怎么办？", "sessionId": "sess-detail"},
        headers=auth_headers,
    )
    resp = client.get("/api/ai/consult/sess-detail", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["sessionId"] == "sess-detail"
    roles = [m["role"] for m in data["messages"]]
    assert "user" in roles and "ai" in roles
    assert any("玉米叶子发黄" in m["content"] for m in data["messages"] if m["role"] == "user")


def test_consult_detail_unknown_session_returns_403(client, auth_headers):
    resp = client.get("/api/ai/consult/sess-not-exist", headers=auth_headers)
    assert resp.status_code == 403


def test_answers_persist_readable_content(client, auth_headers):
    """卡片作答路径只发 answers、不带 message，落库仍须有可读文本（历史回看不空白）。"""
    first = client.post(
        "/api/ai/chat",
        json={"message": "我的叶片发黄", "sessionId": "sess-ans", "processHint": "水稻"},
        headers=auth_headers,
    )
    data = first.json()["data"]
    assert data["sessionId"] == "sess-ans"
    questions = data.get("questions") or []
    # 追问轮才需要作答；若首轮直接出方/转人工，则跳过该用例的作答分支
    if not questions:
        return

    answers = [
        {"slotKey": q["slotKey"], "value": (q.get("options") or ["正常"])[0], "status": "filled"}
        for q in questions
    ]
    client.post(
        "/api/ai/chat",
        json={"sessionId": "sess-ans", "answers": answers},
        headers=auth_headers,
    )

    resp = client.get("/api/ai/consult/sess-ans", headers=auth_headers)
    assert resp.status_code == 200
    messages = resp.json()["data"]["messages"]
    # 每条消息都应有非空内容，不能再出现空串
    assert all((m["content"] or "").strip() for m in messages), messages

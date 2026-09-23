"""AI 对话链路测试：chat、history、delete。"""


def test_chat_returns_reply(client, auth_headers):
    resp = client.post(
        "/api/ai/chat",
        json={"message": "水稻分蘖期怎么施肥？", "sessionId": "sess-001", "cropTag": "水稻"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["sessionId"] == "sess-001"
    assert data["reply"]
    assert "水稻" in data["tagsAdded"] or data["tagsAdded"] == []


def test_history_after_chat(client, auth_headers):
    client.post(
        "/api/ai/chat",
        json={"message": "怎么防治稻飞虱？", "sessionId": "sess-001", "cropTag": "水稻"},
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
        json={"message": "hello", "sessionId": "sess-del", "cropTag": None},
        headers=auth_headers,
    )
    resp = client.delete("/api/ai/history/sess-del", headers=auth_headers)
    assert resp.status_code == 200

    resp = client.get("/api/ai/history", headers=auth_headers)
    data = resp.json()["data"]
    assert all(item["id"] != "sess-del" for item in data["list"])

"""认证链路测试：登录、token 签发、鉴权失败。"""


def test_login_success(client):
    resp = client.post("/api/login", json={"code": "test-code-001"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    data = body["data"]
    assert data["token"]
    assert data["userId"]
    assert data["isNewUser"] is True


def test_login_repeat_is_not_new(client):
    client.post("/api/login", json={"code": "test-code-repeat"})
    resp = client.post("/api/login", json={"code": "test-code-repeat"})
    body = resp.json()
    assert body["data"]["isNewUser"] is False


def test_authorized_require_token(client):
    resp = client.get("/api/farmer/profile")
    assert resp.status_code == 401


def test_authorized_with_token(client, auth_headers):
    resp = client.get("/api/farmer/profile", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["hasProfile"] is False

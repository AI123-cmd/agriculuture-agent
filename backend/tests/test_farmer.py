"""农户档案链路测试：保存、读取、脱敏上下文集入。"""


def _profile_payload():
    return {
        "realName": "李四",
        "age": 45,
        "gender": 1,
        "phone": "13800000000",
        "experienceYear": 10,
        "province": "江西省",
        "city": "抚州市",
        "district": "临川区",
        "address": "XX村",
        "totalMu": 12.5,
        "cultivationType": 1,
        "waterSource": "沟渠水",
        "remark": "主种水稻",
        "crops": [
            {"cropName": "水稻", "plantMu": 12.5, "growthStage": "分蘖期"},
            {"cropName": "烟草", "plantMu": 2.0, "growthStage": "旺长期"},
        ],
    }


def test_save_and_get_profile(client, auth_headers):
    resp = client.post("/api/farmer/profile", json=_profile_payload(), headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["hasProfile"] is True
    assert body["data"]["profile"]["realName"] == "李四"
    assert len(body["data"]["profile"]["crops"]) == 2

    resp = client.get("/api/farmer/profile", headers=auth_headers)
    assert resp.json()["data"]["profile"]["totalMu"] == 12.5


def test_get_profile_empty(client, auth_headers):
    resp = client.get("/api/farmer/profile", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["hasProfile"] is False
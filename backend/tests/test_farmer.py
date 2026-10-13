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
            {
                "cropName": "水稻",
                "processType": "rice",
                "cropVariety": "湘两优900",
                "plantMu": 12.5,
                "sowingDate": "2026-04-10",
                "growthStage": "分蘖期",
            },
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


def test_crop_fields_round_trip(client, auth_headers):
    """P0-A/P1-A：播种日期与品种要能往返，否则农事历与流程归一无从谈起。"""
    client.post("/api/farmer/profile", json=_profile_payload(), headers=auth_headers)

    crops = client.get("/api/farmer/profile", headers=auth_headers).json()["data"]["profile"]["crops"]
    rice = crops[0]
    assert rice["cropVariety"] == "湘两优900"
    assert rice["sowingDate"] == "2026-04-10"
    assert rice["growthStage"] == "分蘖期"
    # 未填的作物不伪造值
    assert crops[1]["sowingDate"] is None
    assert crops[1]["cropVariety"] is None


def test_process_type_normalizes_crop_name(client, auth_headers):
    """P1-A：选了流程类型就把 `crop_name` 归一为流程展示名，使 `object_aliases` 稳定命中。

    农户写"稻子"、类型选"水稻"，落库应为流程展示名"水稻"；`other` 保留自由输入。
    """
    payload = _profile_payload()
    payload["crops"] = [
        {"cropName": "稻子", "processType": "rice", "growthStage": "分蘖期"},
        {"cropName": "番石榴", "processType": "other"},
    ]
    client.post("/api/farmer/profile", json=payload, headers=auth_headers)

    crops = client.get("/api/farmer/profile", headers=auth_headers).json()["data"]["profile"]["crops"]
    assert crops[0]["cropName"] == "水稻"
    assert crops[1]["cropName"] == "番石榴"


def test_get_profile_empty(client, auth_headers):
    resp = client.get("/api/farmer/profile", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["hasProfile"] is False

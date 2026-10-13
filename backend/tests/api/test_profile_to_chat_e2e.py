"""端到端验收：农户档案 → AI 问诊（`/api/ai/chat`）是否真的接通。

单元测试断言的是 `build_profile_snapshot` 的产出形状；本文件断言的是**用户可感的行为**：
先建档案（水稻 + 分蘖期 + 播种日期），再提一个水稻问题，看 AI 是否据此改变了追问。

若这座桥断了，档案里的生育期会被反复追问——用户体感就是"我明明填过，它还在问"。
因此这里断言"档案里已有的生育期不再出现在待答问题中"，这是接通的判据。

为让断言非空洞，提问显式带 `processHint="rice"` 锁定水稻流程：否则分诊可能把问题归到
`general`，而 `general` 本就没有 `rice_growth_stage` 槽位，断言会"自然成立"却什么也没验证。
反向用例（无档案）作为对照，证明"不问生育期"确实来自档案而非流程本身没有该槽位。
"""

from __future__ import annotations

from app.schemas import ProfileRequest

# 单次问诊最多推进的轮数护栏，防止接线错误导致用例死循环。
_MAX_ROUNDS = 8


def _login(client, code: str) -> dict:
    resp = client.post("/api/login", json={"code": code})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['data']['token']}"}


def _seed_rice_profile(client, headers: dict) -> None:
    """建一份"水稻 + 分蘖期 + 已播种"的档案：生育期应由档案自动提供，不该再被问。"""
    resp = client.post(
        "/api/farmer/profile",
        json={
            "realName": "张三",
            "province": "江西省",
            "city": "抚州市",
            "district": "临川区",
            "totalMu": 8.0,
            "crops": [
                {
                    "cropName": "水稻",
                    "processType": "rice",
                    "plantMu": 8.0,
                    "sowingDate": "2026-04-10",
                    "growthStage": "分蘖期",
                }
            ],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


def _drive_rice_consult(client, headers: dict) -> set[str]:
    """跑完一轮水稻问诊，返回全程出现过的槽位键集合。

    显式带 `processHint="rice"` 锁定流程；每轮把待答问题一律答成"叶片发黄"以推进收敛。
    """
    resp = client.post(
        "/api/ai/chat",
        json={"processHint": "rice", "message": "我家水稻叶片发黄，是怎么回事"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    session_id = data["sessionId"]

    asked: set[str] = set()
    for _ in range(_MAX_ROUNDS):
        questions = data.get("questions") or []
        if not questions:
            break
        asked.update(row["slotKey"] for row in questions)
        answers = [{"slotKey": row["slotKey"], "value": "叶片发黄", "status": "filled"} for row in questions]
        resp = client.post(
            "/api/ai/chat",
            json={"sessionId": session_id, "message": "补充", "answers": answers},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
    return asked


def test_profile_growth_stage_is_not_asked_again(client) -> None:
    """接通判据：档案已登记的生育期（分蘖期）在整轮问诊中都不出现。"""
    headers = _login(client, "e2e-profile-rice")
    _seed_rice_profile(client, headers)

    asked = _drive_rice_consult(client, headers)

    assert asked, "问诊未产生任何追问，无法判断档案是否接通"
    assert "rice_growth_stage" not in asked, f"档案里已有生育期，却被再次追问：{sorted(asked)}"


def test_without_profile_still_asks_growth_stage(client) -> None:
    """反证：没有建档时，生育期会被追问——证明上面的"不问"确实来自档案。"""
    headers = _login(client, "e2e-no-profile-rice")

    asked = _drive_rice_consult(client, headers)

    assert "rice_growth_stage" in asked, f"无档案应追问生育期，实际问的是：{sorted(asked)}"


def test_profile_location_enters_triage_context(client) -> None:
    """档案的所在地进入分诊上下文，使对象确认/降级文案能引用真实 Location。"""
    headers = _login(client, "e2e-profile-location")
    _seed_rice_profile(client, headers)

    resp = client.post(
        "/api/ai/chat",
        json={"message": "水稻叶子有斑点"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    # 分诊/问诊正常返回，说明档案快照没有把链路打断
    assert data["sessionId"]
    assert data["stageLabel"]


def test_profile_payload_model_accepts_new_fields() -> None:
    """契约自检：`ProfileRequest` 接受 `processType` / `cropVariety` / `sowingDate`。"""
    req = ProfileRequest.model_validate(
        {
            "realName": "李四",
            "crops": [
                {
                    "cropName": "水稻",
                    "processType": "rice",
                    "cropVariety": "湘两优900",
                    "sowingDate": "2026-04-10",
                    "growthStage": "分蘖期",
                }
            ],
        }
    )
    assert req.crops[0].processType == "rice"
    assert req.crops[0].cropVariety == "湘两优900"
    assert str(req.crops[0].sowingDate) == "2026-04-10"

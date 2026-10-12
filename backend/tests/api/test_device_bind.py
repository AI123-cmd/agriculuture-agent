"""T5-03 验收：设备绑定接口的绑定、幂等、冲突、格式校验与密钥存储。

用真实 `TestClient` 打真实路由，证明的正是接口层行为：新 SN 绑定 200、同一农户重复绑定返回同一
`deviceId`、他人已绑 409、非法编号 422、无令牌 401。数据库断言用会话工厂直接读回，避免只信响应
体——密钥是否明文落库、`secret_digest` 是否为加长摘要，只有真读一遍才可证。

模板匹配断言分两种情形：模板库为空时命中失败（`provide_sample`），导入内置模板后命中通用兜底
（`confirm_template` + `tpl_generic_http`）。绑定本身不依赖模板库是否已导入。
"""

import asyncio
import hashlib
import re

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.device_secret import verify_device_secret
from app.models import DeviceConfig
from app.services.template_seed import seed_vendor_templates

pytestmark = pytest.mark.usefixtures("requires_postgres")

_BIND_URL = "/api/device/bind"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _login(client, code: str) -> tuple[str, str]:
    """登录并返回 (token, userId)。不同 code 派生不同用户（mock 登录）。"""
    resp = client.post("/api/login", json={"code": code})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    return data["token"], data["userId"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _bind(client, token: str, sn: str, farm_entry_id: str | None = None):
    body: dict = {"deviceSn": sn}
    if farm_entry_id is not None:
        body["farmEntryId"] = farm_entry_id
    return client.post(_BIND_URL, json=body, headers=_headers(token))


def _seed_templates(engine) -> None:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> None:
        async with factory() as db:
            await seed_vendor_templates(db)
            await db.commit()

    asyncio.run(_run())


def _read_device(engine, device_sn: str) -> tuple[str, str | None, str | None] | None:
    """直接读回设备行的 (user_id, secret_ref, secret_digest)。找不到返回 None。"""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> tuple[str, str | None, str | None] | None:
        async with factory() as db:
            row = (
                await db.execute(select(DeviceConfig).where(DeviceConfig.device_sn == device_sn))
            ).scalar_one_or_none()
            if row is None:
                return None
            return row.user_id, row.secret_ref, row.secret_digest

    return asyncio.run(_run())


def _count_devices(engine) -> int:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> int:
        async with factory() as db:
            rows = (await db.execute(select(DeviceConfig))).scalars().all()
            return len(rows)

    return asyncio.run(_run())


# ---- 绑定正例 ----


def test_bind_new_sn_returns_200_and_device_id(client, engine) -> None:
    """新 SN 绑定返回 200、`deviceId` 非空、`nextStep` 为两值之一。"""
    token, _ = _login(client, "farmer-bind-01")

    resp = _bind(client, token, "HN-BIND-0001")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["deviceId"]
    assert data["nextStep"] in {"provide_sample", "confirm_template"}


def test_bind_downloads_secret_once_and_stores_digest(client, engine) -> None:
    """响应含明文密钥与上报地址；库中只存摘要与引用，不存明文。"""
    token, _ = _login(client, "farmer-bind-02")

    resp = _bind(client, token, "HN-BIND-0002")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    secret = data["deviceSecret"]
    assert secret
    assert data["reportUrl"] and "HN-BIND-0002" in data["reportUrl"]

    stored = _read_device(engine, "HN-BIND-0002")
    assert stored is not None
    _, secret_ref, digest = stored
    assert secret_ref and secret_ref.startswith("device_secret:")
    assert digest is not None and _HEX64.match(digest)
    # 明文密钥不落库：摘要不等于明文本身
    assert digest != secret


def test_bind_secret_verifies_against_stored_digest(client, engine) -> None:
    """下发的明文密钥能用 `verify_device_secret` 通过，错误密钥不通过。"""
    token, _ = _login(client, "farmer-bind-03")
    resp = _bind(client, token, "HN-BIND-0003")
    secret = resp.json()["data"]["deviceSecret"]

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> tuple[bool, bool]:
        async with factory() as db:
            row = (await db.execute(select(DeviceConfig).where(DeviceConfig.device_sn == "HN-BIND-0003"))).scalar_one()
            return verify_device_secret(row, secret), verify_device_secret(row, secret + "x")

    ok_correct, ok_wrong = asyncio.run(_run())
    assert ok_correct is True
    assert ok_wrong is False


# ---- 幂等 ----


def test_rebind_same_farmer_returns_same_device_id_without_new_secret(client, engine) -> None:
    """同一农户重复绑定同一 SN：返回同一 `deviceId`、不重复建行、不重发密钥。"""
    token, user_id = _login(client, "farmer-bind-04")

    first = _bind(client, token, "HN-BIND-0004")
    second = _bind(client, token, "HN-BIND-0004")

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["data"]["deviceId"] == second.json()["data"]["deviceId"]
    # 幂等分支不重发密钥（否则已配进设备的密钥会立即失效）
    assert second.json()["data"]["deviceSecret"] is None
    assert _count_devices(engine) == 1
    assert _read_device(engine, "HN-BIND-0004")[0] == user_id


# ---- 冲突与越权 ----


def test_bind_sn_owned_by_other_returns_409(client, engine) -> None:
    """他人已绑定的 SN：返回 409，且不改变原归属。"""
    token_a, user_a = _login(client, "farmer-bind-05a")
    token_b, _ = _login(client, "farmer-bind-05b")
    assert _bind(client, token_a, "HN-BIND-0005").status_code == 200

    resp = _bind(client, token_b, "HN-BIND-0005")

    assert resp.status_code == 409, resp.text
    assert _read_device(engine, "HN-BIND-0005")[0] == user_a


def test_rebind_after_unbind_reuses_row(client, engine) -> None:
    """曾解绑的行被复用：仍是一行，归属改为新绑定者，密钥被重置。"""
    token_a, _ = _login(client, "farmer-bind-06a")
    token_b, user_b = _login(client, "farmer-bind-06b")
    assert _bind(client, token_a, "HN-BIND-0006").status_code == 200

    # 手工置空绑定（模拟解绑，T5-09 落地前用直改行代表）
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _unbind() -> None:
        async with factory() as db:
            row = (await db.execute(select(DeviceConfig).where(DeviceConfig.device_sn == "HN-BIND-0006"))).scalar_one()
            row.unbound_at = row.bound_at
            await db.commit()

    asyncio.run(_unbind())

    resp = _bind(client, token_b, "HN-BIND-0006")
    assert resp.status_code == 200, resp.text
    assert _count_devices(engine) == 1
    assert _read_device(engine, "HN-BIND-0006")[0] == user_b


# ---- 入参校验 ----


def test_bind_empty_sn_returns_422(client, engine) -> None:
    """`deviceSn` 为空：契约长度约束拦下，返回 422。"""
    token, _ = _login(client, "farmer-bind-07")
    resp = _bind(client, token, "")
    assert resp.status_code == 422


def test_bind_sn_too_long_returns_422(client, engine) -> None:
    """`deviceSn` 超 60 字符：返回 422。"""
    token, _ = _login(client, "farmer-bind-08")
    resp = _bind(client, token, "H" * 61)
    assert resp.status_code == 422


def test_bind_sn_with_whitespace_returns_400(client, engine) -> None:
    """`deviceSn` 含空白：业务规则拒绝，返回 400（带中文提示）。"""
    token, _ = _login(client, "farmer-bind-09")
    resp = _bind(client, token, "HN 0009")
    assert resp.status_code == 400
    assert resp.json()["code"] == 400


def test_bind_without_token_returns_401(client, engine) -> None:
    """无令牌调用：返回 401（接口属于小程序域，走令牌体系）。"""
    resp = client.post(_BIND_URL, json={"deviceSn": "HN-BIND-401"})
    assert resp.status_code == 401
    assert _count_devices(engine) == 0


# ---- 模板分支 ----


def test_bind_without_templates_falls_to_provide_sample(client, engine) -> None:
    """模板库为空：无法命中模板，`nextStep == "provide_sample"` 且 `vendorTemplateMatched` 为假。"""
    token, _ = _login(client, "farmer-bind-10")

    resp = _bind(client, token, "HN-BIND-0010")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["vendorTemplateMatched"] is False
    assert data["nextStep"] == "provide_sample"
    assert data["templateId"] is None


def test_bind_with_templates_matches_generic(client, engine) -> None:
    """导入内置模板后：通用兜底命中，`templateId == "tpl_generic_http"` 且 `nextStep` 为确认模板。"""
    _seed_templates(engine)
    token, _ = _login(client, "farmer-bind-11")

    resp = _bind(client, token, "HN-BIND-0011")

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["vendorTemplateMatched"] is True
    assert data["templateId"] == "tpl_generic_http"
    assert data["nextStep"] == "confirm_template"


def test_bind_accepts_optional_farm_entry(client, engine) -> None:
    """`farmEntryId` 可空、也可传入；传入时落库。"""
    token, _ = _login(client, "farmer-bind-12")
    resp = _bind(client, token, "HN-BIND-0012", farm_entry_id="fe_0001")
    assert resp.status_code == 200, resp.text

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def _run() -> str | None:
        async with factory() as db:
            row = (await db.execute(select(DeviceConfig).where(DeviceConfig.device_sn == "HN-BIND-0012"))).scalar_one()
            return row.farm_entry_id

    assert asyncio.run(_run()) == "fe_0001"


def test_bind_rejects_extra_field(client, engine) -> None:
    """`extra="forbid"`：请求体多带字段返回 422。"""
    token, _ = _login(client, "farmer-bind-13")
    resp = client.post(_BIND_URL, json={"deviceSn": "HN-BIND-0013", "extra": 1}, headers=_headers(token))
    assert resp.status_code == 422


def test_bind_does_not_leak_secret_digest(client, engine) -> None:
    """响应体只含明文密钥，不含摘要或他人信息。"""
    token, _ = _login(client, "farmer-bind-14")
    resp = _bind(client, token, "HN-BIND-0014")
    data = resp.json()["data"]
    assert "secretDigest" not in data and "secretRef" not in data
    assert set(data) <= {
        "deviceId",
        "vendorTemplateMatched",
        "templateId",
        "nextStep",
        "reportUrl",
        "deviceSecret",
    }


def test_digest_matches_manual_salted_hash(client, engine) -> None:
    """`secret_digest` 等于 `sha256(salt + secret)`（salt 为空时即裸 sha256），证明存储口径。"""
    token, _ = _login(client, "farmer-bind-15")
    secret = _bind(client, token, "HN-BIND-0015").json()["data"]["deviceSecret"]

    _, _, digest = _read_device(engine, "HN-BIND-0015")
    assert digest == hashlib.sha256(secret.encode()).hexdigest()

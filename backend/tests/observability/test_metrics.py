"""T6-09 验收（一）：六项产品级指标的口径、影子行排除与"超 3 轮问诊"。

三层断言：

1. **纯函数口径**（不需要数据库）：`over_round_rate` 的分母只算已完成问诊、阈值取
   `Settings.MAX_COLLECT_ROUNDS`；`DONE_STATUSES` 是 `ConsultStatus` 的镜像（逐一相等）。
2. **聚合口径**（真实 PostgreSQL）：六项产品级指标各有一条断言。闸门拦截率的三条
   `gate_audit` 里有一条 `shadowed=true`，用来钉死"影子行必须被排除"。
3. **接口鉴权**：`GET /api/metrics/summary` 无令牌 401、农户令牌 403、运营令牌 200。
   路径不带 `v1`：本服务的路由前缀是 `/api`（见 `app/main.py` 的 `include_router(prefix="/api")`），
   技术方案正文写的 `/api/v1/...` 是整体规划口径，各阶段任务按实际前缀实现。

用真实库而非桩：口径本身就是"SQL 怎么写的"，用桩会把待验证的聚合语句替换掉。
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.models import Consultation, EvalRun, GateAudit, PromptItem, PromptSnapshot, ReachRecord
from app.models.enums import Channel, ConsultStatus, ReachStatus
from app.models.eval_run import KIND_QUALITY_SAMPLE, QUALITY_LABEL_UNUSABLE
from app.observability.metrics import DONE_STATUSES, over_round_rate
from app.repositories.metrics import MetricsRepository

pytestmark = pytest.mark.usefixtures("requires_postgres")

_USER = "user-t609-metrics"
_SUMMARY_URL = "/api/metrics/summary"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """用例改环境变量（阈值）后必须清 `get_settings` 的 lru_cache，收尾再清一次。"""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _today() -> date:
    return datetime.now(UTC).date()


def _window(days: int = 1) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    return now - timedelta(days=days), now + timedelta(days=1)


def _consultation(
    *, process_id: str = "rice", rounds: int = 1, status: str = ConsultStatus.RESOLVED.value
) -> Consultation:
    from app.db.base import new_id

    return Consultation(
        id=new_id("sess"),
        process_id=process_id,
        process_locked=False,
        title="指标用例问诊",
        object_label="水稻·3亩",
        topic_summary="叶片发黄",
        stage="prescribe",
        rounds=rounds,
        status=status,
        triage_snapshot={},
        slots_snapshot={},
        user_id=_USER,
    )


# ---- 1. 纯函数口径 ----


def test_done_statuses_mirrors_consult_status_without_observing() -> None:
    """`DONE_STATUSES` 与 `ConsultStatus` 的镜像关系：除 `observing` 外逐一相等。

    可观测性刻意不导入 ORM 的枚举（要能脱离数据库单测），两侧取值同步只能靠这条断言保证。
    """
    expected = {status.value for status in ConsultStatus if status is not ConsultStatus.OBSERVING}
    assert DONE_STATUSES == expected


def test_over_round_rate_counts_only_done_consultations() -> None:
    """2 条 rounds=3 与 1 条 rounds=4 的已完成问诊 → 1/3；`observing` 不进分母。"""
    rows = [
        SimpleNamespace(status=ConsultStatus.RESOLVED.value, rounds=3),
        SimpleNamespace(status=ConsultStatus.FOLLOWUP.value, rounds=3),
        SimpleNamespace(status=ConsultStatus.ESCALATED.value, rounds=4),
        # 中间态：即使轮次超限也不进分母，否则长期挂着的问诊会稀释该比例
        SimpleNamespace(status=ConsultStatus.OBSERVING.value, rounds=9),
    ]
    assert over_round_rate(rows) == pytest.approx(1 / 3)


def test_over_round_rate_reads_threshold_from_settings(monkeypatch) -> None:
    """阈值取 `Settings.MAX_COLLECT_ROUNDS`：改成 4 后同数据集返回 0.0，不写魔法数字。"""
    rows = [
        SimpleNamespace(status=ConsultStatus.RESOLVED.value, rounds=3),
        SimpleNamespace(status=ConsultStatus.RESOLVED.value, rounds=3),
        SimpleNamespace(status=ConsultStatus.RESOLVED.value, rounds=4),
    ]
    assert over_round_rate(rows) == pytest.approx(1 / 3)

    monkeypatch.setenv("MAX_COLLECT_ROUNDS", "4")
    get_settings.cache_clear()
    assert over_round_rate(rows) == 0.0


def test_over_round_rate_empty_denominator_is_zero() -> None:
    assert over_round_rate([]) == 0.0


def test_quality_threshold_is_independent_from_harness_threshold() -> None:
    """抽检阈值与评测阈值是两个独立配置项（复用会让两个口径互相污染）。"""
    settings = get_settings()
    assert settings.QUALITY_BAD_RATE_THRESHOLD == 0.10
    assert settings.HARNESS_PASS_RATE_THRESHOLD == 0.90
    assert settings.QUALITY_BAD_RATE_THRESHOLD is not settings.HARNESS_PASS_RATE_THRESHOLD


# ---- 2. 聚合口径（真实库） ----


async def test_triage_accuracy_counts_only_labeled(session_factory) -> None:
    """分流准确率：分母只含已标注行（`triage_correct` 非空）。"""
    async with session_factory() as db:
        for i, flag in enumerate([True, True, False, None]):
            db.add(
                EvalRun(
                    id=f"evr_triage_{i}",
                    kind=KIND_QUALITY_SAMPLE,
                    run_date=_today(),
                    process_id="rice",
                    prescription_card_id=f"card_triage_{i}",
                    triage_correct=flag,
                    payload={},
                )
            )
        await db.commit()

        since, until = _window()
        metric = await MetricsRepository(db).triage_accuracy(since, until)

    # 4 行里 3 行有标注，其中 2 行正确
    assert metric.denominator == 3
    assert metric.numerator == 2
    assert metric.rate == pytest.approx(2 / 3)


async def test_gate_block_rate_excludes_shadow_rows(session_factory) -> None:
    """三条 `gate_audit` 中一条 `shadowed=true`：该行被排除，拦截率为 1/2 而非 1/3。"""
    from uuid import uuid4

    def _audit(decision: str, *, shadowed: bool) -> GateAudit:
        # `id` 有 `ck_gate_audit_id_len` 检查约束（恰好 36 字符），`new_id` 只有 29 字符
        # 过不了约束，故用标准 UUID 字符串。与 `tests/gate/test_audit.py` 的造数口径一致。
        return GateAudit(
            id=str(uuid4()),
            session_id=str(uuid4()),
            process_id="rice",
            config_version=1,
            decision=decision,
            violations=[],
            checked_rules=[],
            prescription_snapshot={},
            shadowed=shadowed,
            user_id=_USER,
        )

    async with session_factory() as db:
        db.add_all(
            [
                # 影子行：decision 是真实判定但对外未拦截，计入会让灰度期拦截率虚高
                _audit("block", shadowed=True),
                _audit("allow", shadowed=False),
                _audit("block", shadowed=False),
            ]
        )
        await db.commit()

        since, until = _window()
        metric = await MetricsRepository(db).gate_block_rate_excluding_shadow(since, until)

    assert metric.denominator == 2, "影子行必须被排除在分母外"
    assert metric.numerator == 1
    assert metric.rate == pytest.approx(0.5)


async def test_subscribe_once_delivery_rate(session_factory) -> None:
    """一次性订阅达成率：`subscribe_once` 渠道的已送达 / 尝试；`in_app` 不参与。"""
    from app.db.base import new_id

    snapshot_id = new_id("snap")
    item_id = new_id("item")
    async with session_factory() as db:
        db.add(
            PromptSnapshot(
                id=snapshot_id,
                snapshot_date=_today(),
                generated_at=datetime.now(UTC),
                env_source="sensor",
                revision=1,
                invalidated=False,
                user_id=_USER,
            )
        )
        # 表间只有外键、无 ORM relationship，SQLAlchemy 推不出落库顺序，故显式 flush。
        await db.flush()
        db.add(
            PromptItem(
                id=item_id,
                snapshot_id=snapshot_id,
                source_kind="env_risk",
                severity="notice",
                callout="水稻",
                fact="土壤偏干",
                impact="分蘖受影响",
                action="适度灌溉",
                facts_provenance={},
                rule_id="R-TEST",
                priority_score=Decimal("1.000"),
                suppressed=False,
                executed=False,
                user_id=_USER,
            )
        )
        await db.flush()
        now = datetime.now(UTC)
        db.add_all(
            [
                ReachRecord(
                    id=new_id("rr"),
                    prompt_item_id=item_id,
                    channel=Channel.SUBSCRIBE_ONCE.value,
                    status=ReachStatus.DELIVERED.value,
                    attempted_at=now,
                    user_id=_USER,
                ),
                ReachRecord(
                    id=new_id("rr"),
                    prompt_item_id=item_id,
                    channel=Channel.SUBSCRIBE_ONCE.value,
                    status=ReachStatus.FAILED.value,
                    fail_reason="api_error",
                    attempted_at=now,
                    user_id=_USER,
                ),
                # 站内触达不计入该指标
                ReachRecord(
                    id=new_id("rr"),
                    prompt_item_id=item_id,
                    channel=Channel.IN_APP.value,
                    status=ReachStatus.DELIVERED.value,
                    attempted_at=now,
                    user_id=_USER,
                ),
            ]
        )
        await db.commit()

        since, until = _window()
        metric = await MetricsRepository(db).subscribe_once_delivery_rate(since, until)

    assert metric.denominator == 2
    assert metric.numerator == 1
    assert metric.rate == pytest.approx(0.5)


async def test_snapshot_click_rate_reports_unavailable(session_factory) -> None:
    """快照点击率：业务库无落库源，显式返回不可得，而不是拿别的字段冒充。"""
    async with session_factory() as db:
        metric = MetricsRepository(db).snapshot_click_rate()

    assert metric.available is False
    assert metric.rate is None
    assert metric.note


async def test_rounds_distribution_and_over_round(session_factory) -> None:
    """追问轮次分布：直方图按轮次计数，超轮次占比只算已完成问诊。"""
    async with session_factory() as db:
        for rounds, status in [
            (3, ConsultStatus.RESOLVED.value),
            (3, ConsultStatus.RESOLVED.value),
            (4, ConsultStatus.FOLLOWUP.value),
            (1, ConsultStatus.OBSERVING.value),
        ]:
            db.add(_consultation(rounds=rounds, status=status))
        await db.commit()

        since, until = _window()
        dist = await MetricsRepository(db).rounds_distribution(since, until)

    assert dist.counts == {1: 1, 3: 2, 4: 1}
    assert dist.done == 3, "observing 不进分母"
    assert dist.over_round == 1
    assert dist.over_round_rate == pytest.approx(1 / 3)


async def test_handoff_rate_denominator_is_all_consultations(session_factory) -> None:
    """转人工率分母是问诊总数（不是已开方数），否则转人工案例被整体排除、指标恒偏低。"""
    async with session_factory() as db:
        db.add(_consultation(status=ConsultStatus.ESCALATED.value))
        db.add(_consultation(status=ConsultStatus.RESOLVED.value))
        db.add(_consultation(status=ConsultStatus.BLOCKED.value))
        await db.commit()

        since, until = _window()
        metric = await MetricsRepository(db).handoff_rate(since, until)

    assert metric.denominator == 3
    assert metric.numerator == 1
    assert metric.rate == pytest.approx(1 / 3)


async def test_product_snapshot_payload_covers_six_metrics(session_factory) -> None:
    """快照口径六项齐全：落库底稿的键名稳定，看板与告警都按这些键取数。"""
    from app.observability.sampling import _product_snapshot_payload

    async with session_factory() as db:
        payload = await _product_snapshot_payload(MetricsRepository(db), datetime.now(UTC))

    assert set(payload) == {
        "triage_accuracy",
        "gate_block_rate",
        "subscribe_once_delivery_rate",
        "snapshot_click_rate",
        "rounds_distribution",
        "handoff_rate",
    }


# ---- 3. 接口鉴权 ----


def _login(client, code: str) -> tuple[str, str]:
    resp = client.post("/api/login", json={"code": code})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    return data["token"], data["userId"]


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_summary_without_token_gets_401(client) -> None:
    """未带令牌：401（身份未确认），区别于已认证的 403。"""
    resp = client.get(_SUMMARY_URL)
    assert resp.status_code == 401


def test_summary_with_farmer_token_gets_403(client, monkeypatch) -> None:
    """农户令牌：403——指标是全平台经营口径，不是单个农户能看的数据。"""
    _, operator_id = _login(client, "operator-t609")
    farmer_token, _ = _login(client, "farmer-t609")
    monkeypatch.setenv("KNOWLEDGE_REVIEW_OPERATOR_IDS", operator_id)
    get_settings.cache_clear()

    resp = client.get(_SUMMARY_URL, headers=_headers(farmer_token))
    assert resp.status_code == 403


def test_summary_with_operator_token_returns_six_metrics(client, monkeypatch) -> None:
    """运营令牌：200，返回六项产品级指标与统计窗口。"""
    token, operator_id = _login(client, "operator-t609")
    monkeypatch.setenv("KNOWLEDGE_REVIEW_OPERATOR_IDS", operator_id)
    get_settings.cache_clear()

    resp = client.get(_SUMMARY_URL, headers=_headers(token))

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert set(data) >= {
        "window",
        "triage_accuracy",
        "gate_block_rate",
        "subscribe_once_delivery_rate",
        "snapshot_click_rate",
        "rounds_distribution",
        "handoff_rate",
    }
    assert data["window"]["days"] == 1


def test_summary_days_window_is_validated(client, monkeypatch) -> None:
    """`days` 超界（如 0 或 31）被 Query 校验拦下，返回 422。"""
    token, operator_id = _login(client, "operator-t609")
    monkeypatch.setenv("KNOWLEDGE_REVIEW_OPERATOR_IDS", operator_id)
    get_settings.cache_clear()

    assert client.get(f"{_SUMMARY_URL}?days=0", headers=_headers(token)).status_code == 422
    assert client.get(f"{_SUMMARY_URL}?days=31", headers=_headers(token)).status_code == 422


def test_unusable_label_constant_value() -> None:
    """三值标注的"不可用"取值是 `unusable`：回退判定的过滤条件依赖它。"""
    assert QUALITY_LABEL_UNUSABLE == "unusable"

"""T2-05：版本注册表与热加载。

热加载的正确性判据不是「能不能换」，而是三件事：
1. 内容变了才替换（哈希比对，而非版本号比对）；
2. 内容没变时什么都不做（不刷日志、不换对象）；
3. 加载失败时保留上一生效版本（而不是把流程判为不可用）。

第 3 条是与启动引导的关键差别：启动时失败 → 标记不可用；运行中转热加载失败 →
继续用旧版。把两者写成同一套会让一次手误的配置提交直接打断线上服务。
"""

import asyncio
import logging

import pytest

from app.agent.state import ProcessId
from app.process_config import registry as registry_mod
from app.process_config.errors import ConfigUnavailableError
from app.process_config.loader import load_process_config
from app.process_config.registry import ProcessConfigRegistry, bootstrap, pin_version, watch_and_reload
from app.process_config.version import build_ref, compute_config_hash


class _FakeSession:
    """最小会话替身：注册表只把它透传给 store_factory。

    必须支持 `async with`，因为 `bootstrap` / `_reload_one` 用的是
    `async with session_factory() as session`。少了这两条魔术方法，异常会被
    入口处的兜底 `except` 吞掉，测试退化成「永远走 seeds 兜底」却仍然通过。
    """

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeRow:
    def __init__(self, config_json: dict) -> None:
        self.config_json = config_json


class _FakeStore:
    """内存版配置读取。用以在不连数据库的前提下驱动热加载。"""

    def __init__(self, rows: dict[ProcessId, dict] | None = None) -> None:
        self.rows = rows or {}

    async def latest_per_process(self) -> dict[ProcessId, _FakeRow]:
        return {pid: _FakeRow(payload) for pid, payload in self.rows.items()}

    async def latest(self, process_id: ProcessId) -> _FakeRow | None:
        payload = self.rows.get(process_id)
        return _FakeRow(payload) if payload is not None else None


def _session_factory() -> _FakeSession:
    return _FakeSession()


def _store_factory(store: _FakeStore):
    def _make(_session) -> _FakeStore:
        return store

    return _make


def test_bootstrap_uses_seeds_when_store_empty() -> None:
    registry = ProcessConfigRegistry()
    asyncio.run(bootstrap(registry, _session_factory, _store_factory(_FakeStore())))

    assert len(registry.snapshot()) == len(ProcessId)
    for pid in ProcessId:
        assert registry.is_available(pid), pid.value


def test_bootstrap_prefers_store_over_seeds() -> None:
    """数据库优先：进程重启后回退到 seeds 会把线上修订退掉。"""
    seed = load_process_config(ProcessId.RICE)
    payload = seed.model_dump(mode="json")
    payload["version"] = 99
    store = _FakeStore({ProcessId.RICE: payload})

    registry = ProcessConfigRegistry()
    asyncio.run(bootstrap(registry, _session_factory, _store_factory(store)))

    assert registry.active(ProcessId.RICE).version == 99


def test_bootstrap_survives_store_failure() -> None:
    """数据库不可用时退化为纯 seeds 启动，而不是整体不可用。"""

    def _broken_factory(_session):
        raise RuntimeError("db down")

    registry = ProcessConfigRegistry()
    asyncio.run(bootstrap(registry, _session_factory, _broken_factory))
    assert registry.is_available(ProcessId.TEA)


def test_active_raises_when_unavailable() -> None:
    registry = ProcessConfigRegistry()
    registry.mark_unavailable(ProcessId.PIG, "配置文件缺失")
    assert registry.is_available(ProcessId.PIG) is False
    assert registry.unavailable_reason(ProcessId.PIG) == "配置文件缺失"
    with pytest.raises(ConfigUnavailableError) as exc:
        registry.active(ProcessId.PIG)
    assert "配置文件缺失" in str(exc.value)


def test_activate_clears_unavailable() -> None:
    registry = ProcessConfigRegistry()
    registry.mark_unavailable(ProcessId.PIG, "临时故障")
    registry.activate(load_process_config(ProcessId.PIG))
    assert registry.is_available(ProcessId.PIG) is True
    assert registry.unavailable_reason(ProcessId.PIG) is None


def test_content_hash_stable_across_calls() -> None:
    config = load_process_config(ProcessId.TEA)
    first = build_ref(config)
    second = build_ref(config)
    assert first.content_hash == second.content_hash
    assert first.matches(second) is True


def test_content_hash_changes_with_content() -> None:
    config = load_process_config(ProcessId.TEA)
    changed = config.model_copy(update={"display_name": "茶树（修订）"})
    assert compute_config_hash(config) != compute_config_hash(changed)
    assert build_ref(config).matches(build_ref(changed)) is False


def test_matches_ignores_version_only_change() -> None:
    """版本号变了但内容没变，`matches` 应为 True——哈希才是判据。"""
    config = load_process_config(ProcessId.RICE)
    bumped = config.model_copy(update={"version": config.version + 1})
    assert build_ref(config).matches(build_ref(bumped)) is True


def test_history_keeps_recent_versions() -> None:
    registry = ProcessConfigRegistry()
    config = load_process_config(ProcessId.RICE)
    for i in range(3):
        registry.activate(config.model_copy(update={"version": config.version + i}))
    versions = [ref.version for ref in registry.history(ProcessId.RICE)]
    assert versions[0] == config.version + 2  # 最新在前
    assert len(versions) == 3


def test_hot_reload_activates_changed_content() -> None:
    seed = load_process_config(ProcessId.RICE)
    registry = ProcessConfigRegistry()
    registry.activate(seed)

    changed = seed.model_copy(update={"version": seed.version + 1, "display_name": "水稻（新版）"})
    store = _FakeStore({ProcessId.RICE: changed.model_dump(mode="json")})

    asyncio.run(watch_and_reload(registry, _session_factory, _store_factory(store), 0, max_iterations=1))

    assert registry.active(ProcessId.RICE).version == seed.version + 1
    assert registry.active(ProcessId.RICE).display_name == "水稻（新版）"


def test_hot_reload_skips_unchanged_content(caplog) -> None:
    """内容未变时不替换、不打 reloaded 日志。"""
    seed = load_process_config(ProcessId.RICE)
    registry = ProcessConfigRegistry()
    registry.activate(seed)
    before = registry.active(ProcessId.RICE)

    store = _FakeStore({ProcessId.RICE: seed.model_dump(mode="json")})
    with caplog.at_level(logging.INFO, logger=registry_mod.__name__):
        asyncio.run(watch_and_reload(registry, _session_factory, _store_factory(store), 0, max_iterations=1))

    assert registry.active(ProcessId.RICE) is before
    assert not [r for r in caplog.records if r.getMessage() == "process config reloaded"]


def test_hot_reload_keeps_previous_on_invalid_config(caplog) -> None:
    """坏配置不得让流程立刻停摆：必须保留旧版并记 ERROR。"""
    seed = load_process_config(ProcessId.RICE)
    registry = ProcessConfigRegistry()
    registry.activate(seed)

    # 删掉 redlines，模型层能过，但 _check_integrity 不是热加载的调用点；
    # 这里直接给一个字段缺失的载荷，让 model_validate 失败。
    broken = seed.model_dump(mode="json")
    broken.pop("redlines")
    store = _FakeStore({ProcessId.RICE: broken})

    with caplog.at_level(logging.ERROR, logger=registry_mod.__name__):
        asyncio.run(watch_and_reload(registry, _session_factory, _store_factory(store), 0, max_iterations=1))

    assert registry.is_available(ProcessId.RICE) is True
    assert registry.active(ProcessId.RICE) is seed
    assert [r for r in caplog.records if r.levelno == logging.ERROR]


def test_hot_reload_failure_does_not_stop_other_flows() -> None:
    """单条流程加载失败，其余流程仍可被 active 读取。"""
    registry = ProcessConfigRegistry()
    for pid in ProcessId:
        registry.activate(load_process_config(pid))

    broken = load_process_config(ProcessId.RICE).model_dump(mode="json")
    broken["version"] = "not-an-int"
    store = _FakeStore({ProcessId.RICE: broken})

    asyncio.run(watch_and_reload(registry, _session_factory, _store_factory(store), 0, max_iterations=1))

    for pid in ProcessId:
        if pid is ProcessId.RICE:
            continue
        assert registry.is_available(pid), pid.value


def test_missing_row_is_noop() -> None:
    seed = load_process_config(ProcessId.RICE)
    registry = ProcessConfigRegistry()
    registry.activate(seed)
    asyncio.run(watch_and_reload(registry, _session_factory, _store_factory(_FakeStore()), 0, max_iterations=1))
    assert registry.active(ProcessId.RICE) is seed


def test_pin_version_writes_state() -> None:
    registry = ProcessConfigRegistry()
    config = load_process_config(ProcessId.TEA)
    registry.activate(config)

    state: dict = {}
    ref = pin_version(registry, ProcessId.TEA, state)

    assert state["config_version"] == config.version
    assert state["config_hash"] == ref.content_hash


def test_concurrent_active_reads_are_consistent() -> None:
    """100 次读取与 10 次写入并行：每次读取到的 process_id 必须与请求一致。

    这条验证「整体替换字典条目」的原子性：若写入方改成原地修改配置对象，
    读取方就可能拿到半更新状态。
    """
    configs = {pid: load_process_config(pid) for pid in ProcessId}
    registry = ProcessConfigRegistry()
    for config in configs.values():
        registry.activate(config)

    ids = list(ProcessId)

    async def _reader(pid: ProcessId) -> ProcessId:
        return registry.active(pid).process_id

    async def _writer(i: int) -> None:
        pid = ids[i % len(ids)]
        registry.activate(configs[pid].model_copy(update={"version": configs[pid].version + i + 1}))

    async def _run() -> list[ProcessId]:
        tasks = [_reader(ids[i % len(ids)]) for i in range(100)]
        tasks += [_writer(i) for i in range(10)]
        return await asyncio.gather(*tasks)

    results = asyncio.run(_run())
    for i, got in enumerate(results[:100]):
        assert got is ids[i % len(ids)]


def test_get_registry_returns_singleton() -> None:
    registry_mod.reset_registry()
    try:
        assert registry_mod.get_registry() is registry_mod.get_registry()
    finally:
        registry_mod.reset_registry()

"""流程配置的版本注册表与热加载。

每次配置变更插入新版本而非覆盖旧版本，闸门与问诊在会话开始时记录所使用的
`config_version`，使任意一次历史问诊都能按当时生效的配置复现（技术方案 15.6）。

热加载的用途不是「随时改随时生效」，而是让配置更新不必重启服务，同时保持一次会话
内的配置恒定。配置加载失败时必须保留上一生效版本继续服务，并把失败写入告警，绝不
出现「配置更新失败导致流程整体不可用」的情况。

**读取路径无锁**是刻意的设计：`active` 读普通字典，热加载写入时整体替换字典条目
（Python 的字典单条赋值是原子的），因此读取方永远看到完整的一份配置，不会读到半更新
的中间状态。锁只留在写入路径上，避免每次智能体读取配置都要竞争锁。
"""

import asyncio
import logging
from collections import defaultdict, deque
from collections.abc import Callable, MutableMapping
from typing import Any, Protocol

from app.agent.state import ProcessId
from app.process_config.errors import ConfigUnavailableError
from app.process_config.loader import load_process_config
from app.process_config.models import ProcessConfig
from app.process_config.version import ConfigVersionRef, build_ref

logger = logging.getLogger(__name__)

# 每个流程保留的历史版本数。用于回答「这条流程在过去两周换过几版配置」这类排障问题，
# 取 10 是因为它远多于实际需要，又能把内存占用压到可忽略。
_HISTORY_SIZE = 10


class ConfigStore(Protocol):
    """热加载所需的配置读取能力。

    刻意声明为 Protocol 而不是直接依赖 T2-07 的 `ProcessConfigRepository`，原因有二：
    一是 `process_config` 包要保持纯数据层（技术方案 15.4），不引入 ORM 类型；
    二是热加载逻辑可以在不连数据库的情况下被测。

    返回的行对象只需具备 `config_json` 属性（可被 `ProcessConfig.model_validate`
    还原的 dict）。
    """

    async def latest_per_process(self) -> dict[ProcessId, Any]:
        """返回每条流程当前生效的配置行。"""
        ...

    async def latest(self, process_id: ProcessId) -> Any | None:
        """返回指定流程当前生效的配置行，没有则返回 `None`。"""
        ...


# 会话工厂与存储工厂的签名。`session_factory` 由调用方（应用 lifespan）提供，
# 使本模块不必知道 `app.db` 的存在。
SessionFactory = Callable[[], Any]
StoreFactory = Callable[[Any], ConfigStore]


class ProcessConfigRegistry:
    """流程配置版本注册表。进程内单例，读取路径无锁。"""

    def __init__(self) -> None:
        self._active: dict[ProcessId, ProcessConfig] = {}
        self._refs: dict[ProcessId, ConfigVersionRef] = {}
        self._history: dict[ProcessId, deque[ConfigVersionRef]] = defaultdict(lambda: deque(maxlen=_HISTORY_SIZE))
        self._unavailable: dict[ProcessId, str] = {}
        self._lock = asyncio.Lock()

    def active(self, process_id: ProcessId) -> ProcessConfig:
        """读取当前生效配置。未加载或不可用时抛 `ConfigUnavailableError`。"""
        config = self._active.get(process_id)
        if config is None:
            raise ConfigUnavailableError(process_id.value, self._unavailable.get(process_id, "未加载"))
        return config

    def is_available(self, process_id: ProcessId) -> bool:
        return process_id in self._active

    def unavailable_reason(self, process_id: ProcessId) -> str | None:
        return self._unavailable.get(process_id)

    def ref_of(self, process_id: ProcessId) -> ConfigVersionRef:
        """取某流程当前生效版本的引用，供 `pin_version` 使用。"""
        ref = self._refs.get(process_id)
        if ref is None:
            raise ConfigUnavailableError(process_id.value, self._unavailable.get(process_id, "未加载"))
        return ref

    def current_ref(self, process_id: ProcessId) -> ConfigVersionRef | None:
        """取引用但允许缺失，供热加载比对使用。"""
        return self._refs.get(process_id)

    def history(self, process_id: ProcessId) -> list[ConfigVersionRef]:
        """最近若干版的引用，最新的在前。"""
        return list(self._history.get(process_id, ()))

    def activate(self, config: ProcessConfig) -> ConfigVersionRef:
        """启用一份配置。是仅有的两个写入口之一。"""
        ref = build_ref(config)
        self._active[config.process_id] = config
        self._refs[config.process_id] = ref
        self._history[config.process_id].appendleft(ref)
        self._unavailable.pop(config.process_id, None)
        logger.info(
            "process config activated",
            extra={
                "process_id": config.process_id.value,
                "version": ref.version,
                "content_hash": ref.content_hash,
            },
        )
        return ref

    def mark_unavailable(self, process_id: ProcessId, reason: str) -> None:
        """把流程标记为不可用。

        只由启动引导与红线完整性校验失败调用。热加载过程中**不调用**本方法：
        一份坏配置不应该让正在服务的流程立刻停摆。
        """
        self._active.pop(process_id, None)
        self._refs.pop(process_id, None)
        self._unavailable[process_id] = reason
        logger.error(
            "process config marked unavailable",
            extra={"process_id": process_id.value, "reason": reason},
        )

    def snapshot(self) -> dict[ProcessId, ConfigVersionRef]:
        """当前生效版本引用的快照，供健康检查与排障读取。"""
        return dict(self._refs)


async def bootstrap(
    registry: ProcessConfigRegistry,
    session_factory: SessionFactory,
    store_factory: StoreFactory,
) -> None:
    """启动时加载：先读数据库，再以 seeds 兜底。

    数据库优先、seeds 兜底的理由是：热加载写入的是数据库，若进程重启后从 seeds 读，
    会把线上已经生效的修订退回到文件版本。seeds 只在数据库尚无记录时（首次部署）
    作为初始数据源。
    """
    stored: dict[ProcessId, Any] = {}
    try:
        async with _session(session_factory) as session:
            stored = await store_factory(session).latest_per_process()
    except Exception as exc:  # noqa: BLE001  数据库不可用时退化为纯 seeds 启动
        logger.error("process config store unavailable, fallback to seeds", extra={"reason": str(exc)})

    for pid in ProcessId:
        try:
            row = stored.get(pid)
            config = ProcessConfig.model_validate(row.config_json) if row is not None else load_process_config(pid)
        except Exception as exc:  # noqa: BLE001  单条流程坏掉不应影响其余流程
            registry.mark_unavailable(pid, str(exc))
            logger.error("process config bootstrap failed", extra={"process_id": pid.value})
            continue
        registry.activate(config)


async def watch_and_reload(
    registry: ProcessConfigRegistry,
    session_factory: SessionFactory,
    store_factory: StoreFactory,
    interval: int,
    *,
    max_iterations: int | None = None,
) -> None:
    """周期性比对版本，有变化则整体替换。异常不得中断循环。

    `max_iterations` 仅供测试使用：没有它，测试只能靠取消任务来停止循环，而那会让
    「循环体里抛异常被吞掉」这类问题难以断言。
    """
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        await asyncio.sleep(interval)
        for pid in ProcessId:
            try:
                await _reload_one(registry, session_factory, store_factory, pid)
            except Exception as exc:  # noqa: BLE001  热加载失败必须保留旧版继续服务
                logger.error(
                    "hot reload failed, keep previous",
                    extra={"process_id": pid.value, "reason": str(exc)},
                )
                continue


async def _reload_one(
    registry: ProcessConfigRegistry,
    session_factory: SessionFactory,
    store_factory: StoreFactory,
    process_id: ProcessId,
) -> None:
    """比对单条流程的版本，内容变了才替换。"""
    async with _session(session_factory) as session:
        row = await store_factory(session).latest(process_id)
    if row is None:
        return
    incoming = ProcessConfig.model_validate(row.config_json)
    incoming_ref = build_ref(incoming)
    current = registry.current_ref(process_id)
    if current is not None and current.matches(incoming_ref):
        return  # 内容未变，不动
    registry.activate(incoming)
    logger.info(
        "process config reloaded",
        extra={"process_id": process_id.value, "version": incoming.version},
    )


def pin_version(
    registry: ProcessConfigRegistry,
    process_id: ProcessId,
    state: MutableMapping[str, Any],
) -> ConfigVersionRef:
    """在会话开始时固定版本，写入 state 与 consultation 表。

    版本固定发生在 `triage` 节点完成后、`observe` 之前。会话中途即使热加载把配置换掉，
    `observe` 到 `prescribe` 仍用 `triage` 时刻的那一份。实现方式是在图组装的节点工厂里
    把配置实例闭包进节点，而不是在节点内部每次现取——这一点由 T2-26 的工厂函数保证，
    本函数只负责提供固定值。
    """
    ref = registry.ref_of(process_id)
    state["config_version"] = ref.version
    state["config_hash"] = ref.content_hash
    return ref


def _session(session_factory: SessionFactory) -> Any:
    """把 session_factory 的调用点收敛到一处，便于注入假实现。"""
    return session_factory()


# 进程内单例。刻意不做成模块级常量之外的任何形态：注册表的语义就是「每个进程
# 各持一份当前生效配置」，多实例只会让「当前生效的是哪一份」变得含糊。
_registry: ProcessConfigRegistry | None = None


def get_registry() -> ProcessConfigRegistry:
    """取进程内注册表单例。"""
    global _registry
    if _registry is None:
        _registry = ProcessConfigRegistry()
    return _registry


def reset_registry() -> None:
    """重建注册表，供测试隔离使用。"""
    global _registry
    _registry = None


__all__ = [
    "ConfigStore",
    "ProcessConfigRegistry",
    "SessionFactory",
    "StoreFactory",
    "bootstrap",
    "get_registry",
    "pin_version",
    "reset_registry",
    "watch_and_reload",
]

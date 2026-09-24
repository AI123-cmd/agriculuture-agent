"""流程配置加载器：从 YAML 文件到 `ProcessConfig` 对象。

加载器一次性完成语法解析、模型校验与跨字段一致性检查，让任何配置错误都在进程
启动时报出来，而不是在农户提问时才暴露。

技术方案 8.4 明确「红线缺失即不可用」。加载器是这条规则的执行点：七条流程中任一条的
红线为空、缺失或校验失败，该流程在路由表中即被标记为不可用，入口处直接返回转人工
引导，禁止以通用规则代替。这个动作只做检测与标记，不做修复。

加载失败一律抛异常，不返回默认配置。若在这里「给一份空壳配置让它跑起来」，所有下游的
`None` 检查都会退化成「反正不会为 None」的假设，反而是引入不可控状态。配置不可用应该
在加载阶段就终结，由路由层把该流程指向 `handoff`。
"""

import logging
import threading
from importlib.resources import files

import yaml
from pydantic import ValidationError

from app.agent.state import ProcessId
from app.process_config.errors import ConfigIntegrityError, ConfigLoadError
from app.process_config.models import ProcessConfig
from app.process_config.redlines import RedlineValidator

logger = logging.getLogger(__name__)

# 配置以包内资源方式读取（`importlib.resources`），保证打包安装后仍可加载
SEEDS_PACKAGE = "app.process_config.seeds"
CONFIG_FILENAME = "{process_id}.yaml"

# 已实现的三类槽位写入者。配置里写了未实现的 owner，等于声明了一个没人能填的槽位。
_KNOWN_OWNERS = {"field_observer", "interviewer", "profile_loader"}


def load_process_config(process_id: ProcessId) -> ProcessConfig:
    """按流程标识加载单份配置。任何失败都抛 `ConfigLoadError`。"""
    path = files(SEEDS_PACKAGE).joinpath(CONFIG_FILENAME.format(process_id=process_id.value))
    if not path.is_file():
        raise ConfigLoadError(f"流程配置缺失: {process_id.value}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigLoadError(f"YAML 语法错误: {process_id.value}") from exc
    if not isinstance(raw, dict):
        raise ConfigLoadError(f"配置根节点必须是映射: {process_id.value}")
    try:
        config = ProcessConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigLoadError(f"配置校验失败: {process_id.value}: {exc.error_count()} 项") from exc
    _check_integrity(config)
    return config


def load_all_configs() -> dict[ProcessId, ProcessConfig]:
    """加载七条流程配置。加载失败的流程不进入返回值，由调用方标记不可用。

    刻意不抛异常而是跳过：一条流程的配置坏了不应让整个服务起不来，只应让那一条
    流程不可用。但跳过的每一条都必须写 ERROR 级结构化日志，由启动自检汇总后在
    健康检查里暴露——静默跳过会让「流程不可用」变成没人知道的线上事实。
    """
    available: dict[ProcessId, ProcessConfig] = {}
    for pid in ProcessId:
        try:
            available[pid] = load_process_config(pid)
        except ConfigLoadError as exc:
            logger.error(
                "process config unavailable",
                extra={"process_id": pid.value, "reason": str(exc)},
            )
    return available


def _check_integrity(config: ProcessConfig) -> None:
    """模型层查不出的跨字段问题在这里拦截。

    逐条检查的取舍：能由 Pydantic 字段类型表达的（如枚举封闭）不重复实现；只有
    「两个字段之间的关系」才在这里判——这类错误模型层看不见，却会让渲染或检索
    静默丢数据。
    """
    problems: list[str] = []

    # 1. 红线不可缺省，且必须通过红线完整性校验（技术方案 8.4）
    #    委托给 RedlineValidator 而不是在这里重写一遍：红线结构校验有两个消费方
    #    （加载期与闸门运行时），两处判据必须来自同一份实现，否则会出现「加载时通过、
    #    闸门运行时判不可用」的矛盾。
    redline_result = RedlineValidator().validate(config)
    if not redline_result.loaded:
        problems.append(f"红线校验未通过: {redline_result.unavailable_reason}")

    # 2. 槽位 key 唯一
    keys = [s.key for s in config.required_slots]
    duplicates = {k for k in keys if keys.count(k) > 1}
    if duplicates:
        problems.append(f"必问槽位 key 重复: {sorted(duplicates)}")

    # 3. owner 必须在已实现的智能体集合内
    unknown = {s.owner for s in config.required_slots} - _KNOWN_OWNERS
    if unknown:
        problems.append(f"槽位 owner 未实现: {sorted(unknown)}")

    # 4. 启用并行会诊就必须配易混淆项，否则聚合器无判据可依（技术方案 6.4）
    if config.panel_enabled and not config.confusable_items:
        problems.append("启用并行会诊但未配置易混淆项")

    # 5. 输出模板的必含小节必须属于小节顺序
    #    若必含小节不在顺序表里，渲染时会静默丢段落，属于「配置写了但没生效」。
    extra_sections = set(config.output_template.required_sections) - set(config.output_template.section_order)
    if extra_sections:
        problems.append(f"必含小节不在 section_order 内: {sorted(extra_sections)}")

    # 6. 时机判据与回访策略的 id 唯一
    for label, ids in (("timing_rules", [r.id for r in config.timing_rules]),):
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            problems.append(f"{label} id 重复: {sorted(dup)}")

    _check_rapeseed_restriction(config, problems)

    if problems:
        raise ConfigIntegrityError(config.process_id.value, problems)


def _check_rapeseed_restriction(config: ProcessConfig, problems: list[str]) -> None:
    """产品说明书 5.4：补齐调研前，油菜流程不得输出药剂推荐类内容。

    这份限制的落实有两道：加载时必须有这条红线，闸门运行时靠 `R-REDLINE` 拦截。
    双保险的理由是知识库中可能误入油菜药剂条目，配置层的声明不能作为唯一的防线。
    """
    if config.process_id is not ProcessId.RAPESEED:
        return
    if not any("药剂推荐" in item.desc for item in config.redlines):
        problems.append("缺少 RAPE-01 禁止输出药剂推荐红线")


_CONFIG_CACHE: dict[ProcessId, ProcessConfig] = {}
_CACHE_LOCK = threading.Lock()


def get_process_config(process_id: ProcessId, *, refresh: bool = False) -> ProcessConfig:
    """带缓存的读取入口。

    `load_process_config` 是给启动自检与热加载使用的入口（每次都读盘，用于发现
    文件变更）；本函数是给智能体使用的入口，走缓存。缓存用锁保护，因为热加载
    可能由另一个线程触发；是否重新加载由 T2-05 决定，本项只提供 `refresh` 开关。
    """
    with _CACHE_LOCK:
        if refresh or process_id not in _CONFIG_CACHE:
            _CONFIG_CACHE[process_id] = load_process_config(process_id)
        return _CONFIG_CACHE[process_id]


def clear_config_cache() -> None:
    """清空进程内缓存，供测试与热加载使用。"""
    with _CACHE_LOCK:
        _CONFIG_CACHE.clear()


def reload_process_config(process_id: ProcessId) -> ProcessConfig:
    """强制重新读盘并刷新缓存，供 T2-05 的热加载调用。

    与 `get_process_config(refresh=True)` 的区别在于语义而非实现：本函数表达的是
    「以文件系统为准重载」，因此加载失败时**不保留**旧缓存——调用方（注册表）会捕获
    异常并继续使用自己手里的上一生效版本。若在这里吞掉异常返回旧值，注册表就失去了
    「这次加载到底成不成功」的判断依据。
    """
    config = load_process_config(process_id)
    with _CACHE_LOCK:
        _CONFIG_CACHE[process_id] = config
    return config


__all__ = [
    "CONFIG_FILENAME",
    "SEEDS_PACKAGE",
    "clear_config_cache",
    "get_process_config",
    "load_all_configs",
    "load_process_config",
    "reload_process_config",
]

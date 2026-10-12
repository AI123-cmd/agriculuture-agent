"""指标采集（T6-09）。

本阶段提供最朴素的进程内计数器与直方图：智能体需要上报"降级了多少次""重试了多少
次"，这些数据在排障时比日志更早暴露问题，因此采集点必须先于看板存在。

标签（`tags`）在本实现里参与计数键：同一指标名下不同标签组合各计一份。因此标签值
必须可哈希且顺序无关——内部按键排序后转成元组做键，避免 `{"a": 1, "b": 2}` 与
`{"b": 2, "a": 1}` 被当成两个不同的桶。

**产品级指标口径**（技术方案 14.3）中"追问轮次分布"与"超 3 轮问诊占比"是确定性计数，
不需要样本落库即可计算，故在这里以纯函数给出（`over_round_rate` / `DONE_STATUSES`）；
其余需要跨表聚合的指标走 `app/repositories/metrics.py`。阈值一律从 `Settings` 读，
不写魔法数字——`MAX_COLLECT_ROUNDS` 与 `QUALITY_BAD_RATE_THRESHOLD` 都能改一处即改口径。

`DONE_STATUSES` 是 `ConsultStatus` 的镜像，**不直接导入 `app.models.enums`**：可观测性
要能脱离 ORM 单独测试（与 `app/gate/enums.py` 的镜像同一动机），取值同步由
`tests/observability/test_metrics.py` 断言两侧逐一相等来保证。
"""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

Tags = Mapping[str, str]
_Key = tuple[str, tuple[tuple[str, str], ...]]

# 已完成（可计入分母）的问诊状态。刻意排除 `observing`：它是"有结论但未闭环"的中间态，
# 长时间挂着的问诊若进分母会把"超轮次占比"稀释掉（技术方案 14.3）。
DONE_STATUSES: frozenset[str] = frozenset({"resolved", "followup", "escalated", "blocked"})

# 检查点写入耗时的指标名。写入发生在图推进的空隙、不在 span 树里（14.3 特意点出
# "LangGraph checkpoint 是隐式成本"），故单独埋点。
CHECKPOINT_WRITE_METRIC = "checkpoint_write_ms"

# 节点耗时的指标名。由 `traced_node` 在每次节点执行后上报，`observability/sampling.py`
# 的定时任务把它落进 `eval_run`（kind=node_timing），P50/P95 再由 SQL 从落库明细算——
# 进程内直方图在重启后丢失，跨天对比必须落库。
NODE_DURATION_METRIC = "node_duration_ms"

# 节点失败与降级计数器的指标名（14.3 链路级指标"各节点失败率与重试次数"）。
NODE_FAILURE_METRIC = "node_failure_total"
NODE_DEGRADED_METRIC = "node_degraded_total"


class ConsultationRow(Protocol):
    """`over_round_rate` 需要的最小行形状：状态与轮次。

    用协议而不是 `Consultation` 模型：指标口径要能被单测喂假行（技术方案 14.3 的
    "轮次是确定性计数"正因如此才好测），耦合 ORM 就无法脱离数据库。
    """

    status: str
    rounds: int


def _key(name: str, tags: Tags | None) -> _Key:
    return (name, tuple(sorted((tags or {}).items())))


class MetricsCollector:
    """进程内计数器与直方图。多进程部署时由落库写入器汇总。"""

    def __init__(self) -> None:
        self._counters: dict[_Key, int] = {}
        self._histograms: dict[_Key, list[float]] = {}

    def incr(self, name: str, *, tags: Tags | None = None, value: int = 1) -> None:
        """计数器 +value。智能体降级、重试、闸门拦截这类"发生次数"都走这里。"""
        key = _key(name, tags)
        self._counters[key] = self._counters.get(key, 0) + value

    def observe(self, name: str, value: float, *, tags: Tags | None = None) -> None:
        """直方图观测值。耗时类指标走这里，取值由聚合查询算分位数。"""
        self._histograms.setdefault(_key(name, tags), []).append(value)

    def counter_value(self, name: str, *, tags: Tags | None = None) -> int:
        """读计数器当前值，供单测断言与调试。"""
        return self._counters.get(_key(name, tags), 0)

    def histogram_values(self, name: str, *, tags: Tags | None = None) -> list[float]:
        """读某组标签下的观测值列表（副本，调用方修改不影响采集器内部状态）。"""
        return list(self._histograms.get(_key(name, tags), []))

    def histogram_all(self, name: str) -> list[float]:
        """读某指标名下**全部标签组合**的观测值，落库写入器用它导出直方图。

        与 `histogram_values` 的区别在于不指定标签：检查点写入耗时的标签是
        `process_id`，导出时按指标名取全量再逐值落库，不必先枚举标签。
        """
        merged: list[float] = []
        for (metric, _tags), values in self._histograms.items():
            if metric == name:
                merged.extend(values)
        return merged

    def histogram_groups(self, name: str) -> list[tuple[dict[str, str], list[float]]]:
        """按标签组合分组读某指标的所有观测值，供落库时保留 `node` / `process_id` 维度。

        节点耗时落库必须带上节点名与流程，否则 P50/P95 无从按 `node` / `process_id` 切分。
        """
        groups: list[tuple[dict[str, str], list[float]]] = []
        for (metric, tags), values in self._histograms.items():
            if metric == name:
                groups.append((dict(tags), list(values)))
        return groups

    def drain_histogram(self, name: str) -> list[tuple[dict[str, str], list[float]]]:
        """读出并按标签组合**移除**某指标的全部观测值，供落库后清空避免重复导出。

        与 `histogram_groups` 的差别只在"读完删"：调度入口每轮把累积样本导出到 `eval_run`
        后必须清空本进程的桶，否则下一轮会把同一批样本再写一次，P50/P95 随轮数膨胀。只清
        指定的指标名，不动其它指标（如 `reach_failed_total`）。
        """
        groups = self.histogram_groups(name)
        for key in [k for k in self._histograms if k[0] == name]:
            del self._histograms[key]
        return groups

    def take_counter(self, name: str, *, tags: Tags | None = None) -> int:
        """读出并清零某计数器，供落库后清空（同 `drain_histogram` 的动机）。"""
        key = _key(name, tags)
        value = self._counters.pop(key, 0)
        return value

    def reset(self) -> None:
        """清空全部指标。测试之间必须调用，否则用例相互污染。"""
        self._counters.clear()
        self._histograms.clear()

    def snapshot(self) -> dict[str, Any]:
        """导出当前全部指标，供排障接口或测试整体断言。"""
        return {
            "counters": {f"{name}{dict(tags) or ''}": value for (name, tags), value in self._counters.items()},
            "histograms": {f"{name}{dict(tags) or ''}": values for (name, tags), values in self._histograms.items()},
        }


# 全局采集器。各层直接用 `from app.observability import metrics` 上报，不传实例，
# 否则每个构造函数都要多一个参数，而指标上报是横切关注点。
metrics = MetricsCollector()


def _max_collect_rounds() -> int:
    """读"问诊最多收集轮次"阈值。延迟导入避免 config 与可观测性在导入期形成环。"""
    from app.core.config import get_settings

    return int(get_settings().MAX_COLLECT_ROUNDS)


def _bad_rate_threshold() -> float:
    """读"不可用占比"阈值。与评测阈值是两个常量，刻意不复用（14.5）。"""
    from app.core.config import get_settings

    return float(get_settings().QUALITY_BAD_RATE_THRESHOLD)


def over_round_rate(consultations: Sequence[ConsultationRow], *, max_rounds: int | None = None) -> float:
    """超过最多收集轮次的问诊占比。分母为**已完成**问诊数。

    这是 14.3 里最重要的单一指标：轮次是确定性计数，不受抽样与标注噪声影响，一旦升高
    直接说明槽位设计或提问话术有问题。阈值取 `Settings.MAX_COLLECT_ROUNDS`（默认 3），
    不写死；分母排除 `observing` 这类中间态，否则长期挂着的问诊会稀释该比例。
    """
    limit = max_rounds if max_rounds is not None else _max_collect_rounds()
    done = [c for c in consultations if c.status in DONE_STATUSES]
    if not done:
        return 0.0
    return sum(1 for c in done if c.rounds > limit) / len(done)


def should_rollback_prompt(
    today_bad_rate: float,
    yesterday_bad_rate: float,
    *,
    threshold: float | None = None,
) -> bool:
    """不可用占比连续两日高于阈值即提示词版本回退（14.5）。

    单日越线可能是抽样波动，连续两日才指向提示词约束不足。阈值取
    `Settings.QUALITY_BAD_RATE_THRESHOLD`（默认 0.10），与评测一致率阈值是两个口径。
    返回 `True` 只表示"该生成回退工单"，实际回退仍需人工确认（15.5 灰度原则）。
    """
    limit = threshold if threshold is not None else _bad_rate_threshold()
    return today_bad_rate > limit and yesterday_bad_rate > limit


def observe_checkpoint_write_ms(elapsed_ms: float, *, process_id: str | None = None) -> None:
    """记录一次检查点写入耗时（毫秒）。

    在 `app/agent/checkpointer.py` 的写入路径上调用。写进进程内直方图；跨进程汇总由
    `MetricsRepository.record_checkpoint_timing` 落库（`eval_run`），因为直方图在进程重启后
    丢失，而"某次发版后 P95 劣化"通常需要跨天对比。
    """
    tags: Tags | None = {"process_id": process_id} if process_id else None
    metrics.observe(CHECKPOINT_WRITE_METRIC, float(elapsed_ms), tags=tags)


def checkpoint_write_samples() -> list[float]:
    """导出进程内累积的全部检查点写入耗时样本，供落库写入器使用。"""
    return metrics.histogram_all(CHECKPOINT_WRITE_METRIC)


def drain_checkpoint_write_samples() -> list[float]:
    """导出并清空检查点写入耗时样本，供每轮落库后调用（避免重复导出）。"""
    drained = metrics.drain_histogram(CHECKPOINT_WRITE_METRIC)
    return [value for _tags, values in drained for value in values]


def observe_node_duration(
    node: str,
    elapsed_ms: float,
    *,
    process_id: str | None = None,
    ok: bool = True,
    degraded: bool = False,
) -> None:
    """记录一次节点执行的耗时与成败（14.3 链路级指标）。

    由 `traced_node` 在节点返回后调用。`node` / `process_id` 进标签，落库时保留为
    `eval_run.node` / `process_id`，使 P50/P95 与失败率可按节点与流程切分。

    失败与降级各进一个计数器，**标签与直方图同构**（同样带 `process_id`）：落库时按同一组
    标签取回计数，才能把"这一批样本里有几次失败/降级"摊到对应的行上。若计数器只按 `node`
    打标签，按流程切分的失败率就无从还原。
    """
    tags: dict[str, str] = {"node": node}
    if process_id:
        tags["process_id"] = process_id
    metrics.observe(NODE_DURATION_METRIC, float(elapsed_ms), tags=tags)
    if not ok:
        metrics.incr(NODE_FAILURE_METRIC, tags=tags)
    if degraded:
        metrics.incr(NODE_DEGRADED_METRIC, tags=tags)


def node_duration_groups() -> list[tuple[dict[str, str], list[float]]]:
    """导出进程内累积的节点耗时样本分组，供落库写入器使用。"""
    return metrics.histogram_groups(NODE_DURATION_METRIC)


def drain_node_duration_groups() -> list[tuple[dict[str, str], list[float]]]:
    """导出并清空节点耗时样本分组，供每轮落库后调用（避免重复导出）。"""
    return metrics.drain_histogram(NODE_DURATION_METRIC)


__all__ = [
    "CHECKPOINT_WRITE_METRIC",
    "DONE_STATUSES",
    "NODE_DEGRADED_METRIC",
    "NODE_DURATION_METRIC",
    "NODE_FAILURE_METRIC",
    "ConsultationRow",
    "MetricsCollector",
    "Tags",
    "checkpoint_write_samples",
    "drain_checkpoint_write_samples",
    "drain_node_duration_groups",
    "metrics",
    "node_duration_groups",
    "observe_checkpoint_write_ms",
    "observe_node_duration",
    "over_round_rate",
    "should_rollback_prompt",
]

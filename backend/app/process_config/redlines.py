"""红线的领域模型与完整性校验。

把 T2-02 里作为引用外壳的 `RedlineSpec` 展开为闸门可直接消费的结构。校验器要回答
三个问题：这条红线引用的闸门规则是否真实存在、红线声明的 `data` 载荷是否符合该规则的
期望结构、七条流程的红线是否在启动时全部齐备。

技术方案 8.4 明确「红线缺失即不可用」。本模块是这条规则的判定者，它产出一个
「可用性清单」交给 T2-05 的注册表与 T2-24 的路由函数；任何一条流程的红线不完整，
该流程在路由表中被标记不可用，入口处直接返回转人工引导。

校验器**返回结果而不抛异常**，因为它要被两处调用：启动时汇总七条流程的可用性清单，
以及闸门运行时判断 `process_redlines_loaded`。抛异常的形态会让调用方不得不写 `try`，
而 `try` 里又必须处理所有异常，容易掩盖真正的问题。
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.agent.state import ProcessId, ViolationSeverity
from app.process_config.models import ProcessConfig, RedlineSpec
from app.process_config.rule_ids import RULE_PAYLOAD_SPEC, GateRuleId, parse_rule_id


class Redline(BaseModel):
    """一条展开后的红线，闸门可直接消费。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # `rule_ids` 是元组而不是列表：红线对象要放进 `frozen=True` 的配置里，
    # 元组可哈希、可安全共享。
    rule_ids: tuple[GateRuleId, ...]
    id: str
    desc: str
    severity: ViolationSeverity
    payload: dict[str, Any] = {}

    @property
    def is_blocking(self) -> bool:
        return self.severity is ViolationSeverity.BLOCK

    def labels(self) -> list[str]:
        """命中即拦截的文本清单（禁用品种与禁用短语的并集）。

        `forbidden`（禁用品种）与 `forbidden_phrases`（禁用短语）统一暴露，供闸门的
        `R-REDLINE` 规则做一次文本包含扫描。
        """
        return list(self.payload.get("forbidden", [])) + list(self.payload.get("forbidden_phrases", []))


class LoadedRedlines(BaseModel):
    """一条流程的红线加载结果，是 `GateResult.process_redlines_loaded` 的数据源。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    process_id: ProcessId
    version: int
    redlines: tuple[Redline, ...]
    # `loaded` 是布尔而不是「红线列表非空」的隐式判断：一条流程可能配置了红线但其
    # 结构不合法，此时列表非空但本质不可用。显式布尔让闸门的字段有明确来源。
    loaded: bool
    unavailable_reason: str | None = None

    @classmethod
    def unavailable(cls, process_id: ProcessId, version: int, reason: str) -> "LoadedRedlines":
        """把「不可用」构造成一种正常结果，而不是靠 `None` 表达。

        这样闸门侧无须判空，只判 `loaded`；`blocking_labels()` 在不可用时返回空列表，
        不需要调用方写 `if redlines:` 之类的防御。
        """
        return cls(process_id=process_id, version=version, redlines=(), loaded=False, unavailable_reason=reason)

    def blocking_labels(self) -> list[str]:
        """全部 block 级红线的禁用文本清单。"""
        out: list[str] = []
        for item in self.redlines:
            if item.is_blocking:
                out.extend(item.labels())
        return out

    def declared_rule_ids(self) -> list[str]:
        """本流程声明的全部规则 id（去重排序），供闸门做「声明了但没执行」的反查。"""
        return sorted({r.value for item in self.redlines for r in item.rule_ids})


class RedlineValidator:
    """红线完整性校验。启动时对七条流程各跑一次。"""

    def validate(self, config: ProcessConfig) -> LoadedRedlines:
        problems: list[str] = []
        parsed: list[Redline] = []
        seen_ids: set[str] = set()

        if not config.redlines:
            return LoadedRedlines.unavailable(config.process_id, config.version, "redlines 为空")

        for spec in config.redlines:
            if spec.id in seen_ids:
                problems.append(f"红线 id 重复: {spec.id}")
                continue
            seen_ids.add(spec.id)
            rule_ids = self._resolve_rule_ids(spec, problems)
            self._check_payload(spec, rule_ids, problems)
            if not rule_ids:
                # 一条规则都没解析出来时跳过构造：它已经作为问题被记录，
                # 再塞进结果只会让后续按 rule_ids 遍历的地方拿到空元组。
                continue
            parsed.append(
                Redline(
                    id=spec.id,
                    desc=spec.desc,
                    rule_ids=tuple(rule_ids),
                    severity=spec.severity,
                    payload=spec.data,
                )
            )

        if problems:
            return LoadedRedlines.unavailable(config.process_id, config.version, "; ".join(problems))
        return LoadedRedlines(
            process_id=config.process_id,
            version=config.version,
            redlines=tuple(parsed),
            loaded=True,
        )

    @staticmethod
    def _resolve_rule_ids(spec: RedlineSpec, problems: list[str]) -> list[GateRuleId]:
        """展开逗号分隔的 `rule` 字段，逐项解析为规则 id。"""
        rule_ids: list[GateRuleId] = []
        for raw in spec.rule.split(","):
            token = raw.strip()
            if not token:
                problems.append(f"{spec.id} 的 rule 字段含空项")
                continue
            rule_id = parse_rule_id(token)
            if rule_id is None:
                problems.append(f"{spec.id} 引用了不存在的规则 {token!r}")
                continue
            rule_ids.append(rule_id)
        return rule_ids

    @staticmethod
    def _check_payload(spec: RedlineSpec, rule_ids: list[GateRuleId], problems: list[str]) -> None:
        """校验 `data` 载荷的键名是否在该规则允许的集合内。

        只查键名不查取值：「最小间隔 7 天」这类取值对不对，只有农技人员能判断；
        而「写了一个不会被读取的键」是工具能确定的问题。
        """
        payload_keys = set(spec.data)
        if not payload_keys:
            return
        for rid in rule_ids:
            allowed = RULE_PAYLOAD_SPEC.get(rid)
            if allowed is None:
                problems.append(f"{spec.id} 的规则 {rid.value} 不接受 data，但配置了 {sorted(payload_keys)}")
            elif not payload_keys <= allowed:
                problems.append(f"{spec.id} 的 data 键 {sorted(payload_keys - allowed)} 不在 {rid.value} 的允许集合内")


def build_availability(
    validator: RedlineValidator, configs: dict[ProcessId, ProcessConfig]
) -> dict[ProcessId, LoadedRedlines]:
    """产出七条流程的红线可用性清单，缺项一律标记不可用（不静默跳过）。

    对七条流程遍历而不是只遍历已加载的：若只遍历已加载的，一条流程「因为配置文件被
    删掉」而消失时就会没有任何记录，而「配置消失」恰恰是必须暴露的状态。

    清单的消费方有两个：路由层（T2-24 在进入流程前查清单，不可用则指向 `handoff`）
    与启动自检（T1-08 汇总后写健康检查）。两处都不接受「红线不可用但继续按通用规则跑」，
    这正是产品说明书 9.3 所禁止的。
    """
    manifest: dict[ProcessId, LoadedRedlines] = {}
    for pid in ProcessId:
        config = configs.get(pid)
        if config is None:
            manifest[pid] = LoadedRedlines.unavailable(pid, 0, "配置未加载")
            continue
        manifest[pid] = validator.validate(config)
    return manifest


__all__ = ["LoadedRedlines", "Redline", "RedlineValidator", "build_availability"]

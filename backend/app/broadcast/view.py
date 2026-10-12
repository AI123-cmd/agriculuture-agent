"""提示快照展示视图（T3-10，技术方案 11.5 / 11.2）。

把 T3-08 生成的快照与条目装配成前端可直接渲染的视图：跨日弱化状态、环境来源标注、
被抑制条目的过滤、紧急条目的视觉强化。**后端不做内容变更**——跨日弱化只是
`display_status` 的取值变化，条目文案与结构保持生成时的样子，农户第二天打开看到的内容
与他昨天看到的一致，不会因"过期"被系统改写或删减。

四件事集中在本模块，前端不再自行判断：

1. `compute_display_status` 由 `generated_at` 与当前日期算天数差，产三值状态。天数由后端
   单列 `days_ago`：天数计算涉及时区，前端本地时区与服务端不一致时会算出不同天数。
2. `build_env_note` 把环境来源四级与地区回退命中级翻成界面文案，`none` 与 `sensor_stale`
   必填，口径与 T2-33 的 `environmentNote` 一致（透明度是信任与安全红线的一部分）。
3. `build_snapshot_view` 只收 `suppressed=False` 的条目：被条数控制压制的条目留在库里供
   产品复盘，但不进展示列表，过滤放在此处而非交给前端，避免某处漏判就把被压制的提示露出。
4. `urgent` 条目在 `PromptItemView.emphasis` 上恒为 `high`：状态弱化针对"今天该做什么"的
   常规农事提示，不是"马上要出事"的紧急提示，前端据此决定是否忽略透明度降级。

分层：本模块只做计算与装配，不访问数据库（守全局约定 L4）；条目由调用方给出，或从生成
链路挂在快照对象上的 `items` 读取。

字段命名：内部属性用蛇形，与技术方案 11.5 的 `generated_at` / `display_status` /
`days_ago` 逐字一致；接口输出经别名生成器转小驼峰（`generatedAt` 等），符合"后端内部
蛇形、转换发生在 schema 边界"的既有约定。
"""

import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.domain.metrics import SOURCE_LABELS
from app.models import PromptItem, PromptSnapshot

DisplayStatus = Literal["today", "yesterday", "stale"]

# 环境来源四级 -> 界面文案。四值全覆盖，不允许"没数据就留空"；`sensor_stale` 的
# `{stale_hours}` 由 `_hours_of` 从采集层的时效标注里规整提取后填入。
# 四个基本文案与 `SOURCE_LABELS`（`app/domain/metrics.py`）同源：同一句话在环境看板
# （T5-08）、问诊的 `environmentNote`（T2-33）与本模块三处出现，措辞只能有一处定义。
ENV_SOURCE_LABEL: dict[str, str] = {
    **SOURCE_LABELS,
    "sensor_stale": "数据来源：本地传感器（数据为 {stale_hours} 小时前）",
}

# 时效标注里的小时数：采集层措辞可能是"数据为 3.5 小时前"，界面要的是规整说法。
_HOURS_PATTERN = re.compile(r"(\d+(?:\.\d+)?)")


def _to_camel(name: str) -> str:
    """属性名（蛇形）转接口字段名（小驼峰）：`generated_at` -> `generatedAt`。"""
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


def compute_display_status(generated_at: datetime, today: date) -> tuple[DisplayStatus, int]:
    """展示状态由天数差计算，不由快照内容决定。

    三值而非两值：`today` 正常展示、`yesterday` 弱化但内容仍可信（农业生产变化缓慢）、
    `stale` 需要更强的更新引导——两值会让放置一周的快照与昨天的快照得到同样的展示强度，
    引导力度不够。`days_ago <= 0`（含负值）统一按 `today`：负值在时钟回拨或服务器时区切换
    时可能出现，此时按"今天"展示比按"未来"展示更安全。
    """
    days_ago = (today - generated_at.date()).days
    if days_ago <= 0:
        return "today", 0
    if days_ago == 1:
        return "yesterday", 1
    return "stale", days_ago


def _hours_of(staleness_note: str | None) -> int | None:
    """从时效标注里提取小时数并向下取整；取不到返回 `None`。

    不足 1 小时显示"1 小时前"而不是"0 小时前"（后者语义上等于实时，与 `sensor_stale`
    的定义矛盾）。取不到小时数时返回 `None`，由调用方决定是否退化文案，绝不臆造数字。
    """
    if not staleness_note:
        return None
    match = _HOURS_PATTERN.search(staleness_note)
    if match is None:
        return None
    return max(int(float(match.group(1))), 1)


def build_env_note(env_source: str, staleness_note: str | None, region_hit_level: str | None) -> str:
    """环境来源标注：四值全覆盖，`none` 与 `sensor_stale` 必填说明。

    `none` 说明"判断基于你的描述"、`sensor_stale` 说明"数据是几小时前的"——农户有权知道
    AI 的判断建立在什么之上（产品说明书 4.4.2）。`none` 且带地区回退命中级时，补一句
    "农时提示按 XX 通用口径给出"，交代农时提示的地域依据。

    `sensor_stale` 但取不到小时数（标注缺失或不可解析）时退化为"数据来源：本地传感器"，
    不带时效数字：宁可少说一句，也不给出没有依据的小时数。
    """
    if env_source == "none":
        base = ENV_SOURCE_LABEL["none"]
        if region_hit_level:
            return f"{base}；农时提示按{region_hit_level}通用口径给出。"
        return base
    if env_source == "sensor_stale":
        hours = _hours_of(staleness_note)
        if hours is None:
            return ENV_SOURCE_LABEL["sensor"]
        return ENV_SOURCE_LABEL["sensor_stale"].format(stale_hours=hours)
    return ENV_SOURCE_LABEL[env_source]


def _emphasis_of(severity: str) -> Literal["high", "normal"]:
    """紧急条目恒为最高视觉权重；其余按常规。"""
    return "high" if severity == "urgent" else "normal"


def _generated_label(generated_at: datetime) -> str:
    """生成时间的预格式化兜底（"M 月 D 日 HH:MM"）；前端仍可用 ISO 时间戳自行本地化。"""
    return f"{generated_at.month} 月 {generated_at.day} 日 {generated_at.hour:02d}:{generated_at.minute:02d}"


class PromptItemView(BaseModel):
    """一条展示条目：五段式字段（呼告/事实/影响/行动）+ 级别 + 勾选 + 视觉权重。

    `emphasis` 供前端决定是否忽略 `SnapshotView.display_status` 的透明度降级：`urgent`
    条目的时效性以小时计，不随跨日弱化。
    """

    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True)

    item_id: str
    source_kind: str
    severity: str
    callout: str
    fact: str
    impact: str
    action: str
    executed: bool
    rule_id: str
    emphasis: Literal["high", "normal"]


class SnapshotView(BaseModel):
    """提示快照的展示视图（技术方案 11.5 的四字段 + 渲染所需的补充字段）。

    `items` 只含未被压制的条目；`display_status` / `days_ago` 由 `generated_at` 算出；
    `generated_label` / `revision` 供"今日提示 · 9 月 23 日 07:12 更新"一类的标注使用。
    """

    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True)

    items: list[PromptItemView]
    generated_at: datetime
    display_status: DisplayStatus
    days_ago: int
    snapshot_id: str
    farm_entry_id: str | None = None
    revision: int = 1
    generated_label: str = ""
    env_source: str = "none"
    env_note: str = ""


def build_item_view(item: PromptItem) -> PromptItemView:
    """单条条目转视图：`severity` 决定 `emphasis`。"""
    return PromptItemView(
        item_id=item.id,
        source_kind=item.source_kind,
        severity=item.severity,
        callout=item.callout,
        fact=item.fact,
        impact=item.impact,
        action=item.action,
        executed=bool(item.executed),
        rule_id=item.rule_id,
        emphasis=_emphasis_of(item.severity),
    )


def build_snapshot_view(
    snapshot: PromptSnapshot,
    *,
    today: date,
    items: Sequence[PromptItem] | None = None,
    staleness_note: str | None = None,
    region_hit_level: str | None = None,
) -> SnapshotView | None:
    """把快照 ORM 对象装配成展示视图；`invalidated=True` 返回 `None`（视同尚未生成）。

    `items` 缺省时从生成链路挂在快照对象上的同名属性读取（`getattr` 取值，缺省空列表，
    避免"没有条目"变成一次 `AttributeError`）。展示列表只含 `suppressed=False` 的条目。

    `staleness_note` / `region_hit_level` 由调用方按需传入用于环境标注；快照表本身只持久化
    `env_source`，两者缺失时 `build_env_note` 会退化为不带时效数字的文案。
    """
    if snapshot.invalidated:
        return None
    source_items = items if items is not None else (getattr(snapshot, "items", None) or ())
    kept = [item for item in source_items if not item.suppressed]
    display_status, days_ago = compute_display_status(snapshot.generated_at, today)
    return SnapshotView(
        items=[build_item_view(item) for item in kept],
        generated_at=snapshot.generated_at,
        display_status=display_status,
        days_ago=days_ago,
        snapshot_id=snapshot.id,
        farm_entry_id=snapshot.farm_entry_id,
        revision=snapshot.revision,
        generated_label=_generated_label(snapshot.generated_at),
        env_source=snapshot.env_source,
        env_note=build_env_note(snapshot.env_source, staleness_note, region_hit_level),
    )


__all__ = [
    "ENV_SOURCE_LABEL",
    "DisplayStatus",
    "PromptItemView",
    "SnapshotView",
    "build_env_note",
    "build_item_view",
    "build_snapshot_view",
    "compute_display_status",
]

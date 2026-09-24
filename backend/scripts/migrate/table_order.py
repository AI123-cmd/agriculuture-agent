"""表迁移顺序、列规格与外键依赖（T1-14）。

迁移脚本与校验脚本共用这里的定义，避免"列清单"出现两个真相来源。三件事：

1. 按外键依赖排出导入顺序。PG 不提供"临时关闭外键检查"的可用开关
   （`session_replication_role = replica` 会让源数据本身违反外键的问题被掩盖），
   因此只能拓扑排序导入，并把"外键违反"作为校验项之一。
2. 标出每张表哪些列要做 JSON / 布尔 / 时区转换。
3. 标出哪些表必须流式读（整表读进内存会 OOM）。

列清单按 MySQL 源库的实际结构写（`information_schema.columns`），不是按任务文件
举例的字段名——例如任务文件提到"`location` 的坐标字段"要做 JSON 转换，但源库里
`location.latitude/longitude` 与 `field.longitude/latitude` 都是 `decimal(10,6)`，
不是 JSON 列，按实际类型直传即可。
"""

from dataclasses import dataclass

_TIMESTAMPS: tuple[str, ...] = ("create_time", "update_time")

# 未绑定设备：`device.user_id` 在源库可空。PG 侧 T1-05 的 `OwnedMixin` 已把归属列
# 收紧为 NOT NULL（解绑走软删除），未绑定设备无法判定归属，只能显式排除。
UNBOUND_DEVICE_FILTER = "`user_id` IS NOT NULL AND `user_id` <> ''"


@dataclass(frozen=True)
class TableSpec:
    """一张表的完整迁移契约。"""

    name: str
    columns: tuple[str, ...]
    primary_key: str
    json_columns: tuple[str, ...] = ()
    # 做 `convert_bool` 值域校验的列：出现 0/1/None 以外的值即报错终止，
    # 防止"某列语义被误判"要到线上才暴露。落库形态由目标表列类型决定。
    bool_validated_columns: tuple[str, ...] = ()
    datetime_columns: tuple[str, ...] = ()
    # 大表标记：用 SSCursor 流式读，不整表读进内存
    stream: bool = False
    # 归属列非空校验（T1-05 的 OwnedMixin 要求）。`user` 表自身无 user_id 列，
    # `ai_message` 的归属由 `conversation_id` 间接表达，两者不参与该校验。
    owner_required: bool = True
    # 源库侧过滤片段与它在报告里的理由（为空表示整表迁移）
    source_filter: str | None = None
    filter_reason: str | None = None


TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec(
        name="user",
        columns=(
            "id",
            "openid",
            "unionid",
            "nick_name",
            "avatar_url",
            "gender",
            "country",
            "province",
            "city",
            *_TIMESTAMPS,
        ),
        primary_key="id",
        datetime_columns=_TIMESTAMPS,
        owner_required=False,
    ),
    TableSpec(
        name="farmer_profile",
        columns=(
            "id",
            "user_id",
            "real_name",
            "age",
            "gender",
            "phone",
            "experience_year",
            "province",
            "city",
            "district",
            "address",
            "total_mu",
            "cultivation_type",
            "water_source",
            "remark",
            *_TIMESTAMPS,
        ),
        primary_key="id",
        datetime_columns=_TIMESTAMPS,
    ),
    TableSpec(
        name="crop",
        columns=(
            "id",
            "user_id",
            "field_id",
            "crop_name",
            "crop_variety",
            "sowing_date",
            "growth_stage",
            "plant_mu",
            "is_main",
            "status",
            "harvest_date",
            *_TIMESTAMPS,
        ),
        primary_key="id",
        datetime_columns=_TIMESTAMPS,
    ),
    TableSpec(
        name="field",
        columns=(
            "id",
            "user_id",
            "field_name",
            "mu",
            "soil_type",
            "soil_ph",
            "longitude",
            "latitude",
            *_TIMESTAMPS,
        ),
        primary_key="id",
        datetime_columns=_TIMESTAMPS,
    ),
    TableSpec(
        name="device",
        columns=(
            "id",
            "user_id",
            "field_id",
            "device_sn",
            "device_type",
            "device_name",
            "status",
            "last_online",
            "bind_time",
            *_TIMESTAMPS,
        ),
        primary_key="id",
        datetime_columns=(*_TIMESTAMPS, "last_online", "bind_time"),
        source_filter=UNBOUND_DEVICE_FILTER,
        filter_reason="未绑定设备（user_id 为空）不迁移：PG 侧归属列已收紧为 NOT NULL，无归属的设备无法落库",
    ),
    TableSpec(
        name="location",
        columns=("id", "user_id", "location_name", "latitude", "longitude", "source", *_TIMESTAMPS),
        primary_key="id",
        datetime_columns=_TIMESTAMPS,
    ),
    TableSpec(
        name="user_profile_tag",
        columns=("id", "user_id", "tags", "context", "last_chat_at", "chat_count", *_TIMESTAMPS),
        primary_key="id",
        json_columns=("tags", "context"),
        datetime_columns=(*_TIMESTAMPS, "last_chat_at"),
    ),
    TableSpec(
        name="memory_summary",
        columns=("id", "user_id", "session_id", "summary", "key_facts", *_TIMESTAMPS),
        primary_key="id",
        json_columns=("key_facts",),
        datetime_columns=_TIMESTAMPS,
    ),
    TableSpec(
        name="ai_conversation",
        columns=("id", "user_id", "title", "crop_tag", *_TIMESTAMPS),
        primary_key="id",
        datetime_columns=_TIMESTAMPS,
    ),
    TableSpec(
        name="ai_message",
        columns=("id", "conversation_id", "role", "content", "image_url", *_TIMESTAMPS),
        primary_key="id",
        datetime_columns=_TIMESTAMPS,
        stream=True,
        owner_required=False,
    ),
    TableSpec(
        name="broadcast_log",
        columns=(
            "id",
            "user_id",
            "date",
            "time",
            "title",
            "content",
            "type",
            "level",
            "source",
            "meta",
            "done",
            *_TIMESTAMPS,
        ),
        primary_key="id",
        json_columns=("meta",),
        # `done` 在源库是 smallint(0/1)，PG 基线（0001_baseline）也建成 SmallInteger，
        # 因此这里用它做**值域校验**而不改落库形态：出现 0/1 以外的值即报错终止，
        # 与 convert_bool 同等严格。列类型维持整数，理由见方案 12.8 与 0001_baseline
        # 的列定义——改类型需要新增迁移，超出本项"改动范围"表所列的文件。
        bool_validated_columns=("done",),
        datetime_columns=_TIMESTAMPS,
    ),
)

TABLE_ORDER: tuple[str, ...] = tuple(spec.name for spec in TABLE_SPECS)

SPEC_BY_NAME: dict[str, TableSpec] = {spec.name: spec for spec in TABLE_SPECS}

# 主键为自增整数的表：显式写入源 id 后必须把序列推到 max(id)，
# 否则迁移后第一条新插入会从 1 开始并撞上已迁入的主键。
SERIAL_TABLES: frozenset[str] = frozenset({"ai_message"})


@dataclass(frozen=True)
class ForeignKeySpec:
    """一条外键关系：子表列 → 父表列。用于迁移后孤儿行校验。"""

    child_table: str
    child_column: str
    parent_table: str
    parent_column: str


# 与 `0001_baseline` 建出的外键一致（技术方案 12.7 的高优先级外键）。
# 其余"逻辑关联"（如 `crop.field_id`）在 PG 侧没有外键约束，无法校验孤儿行，
# 因此不列入——列进来只会得到一个必然失败的校验项。
FOREIGN_KEYS: tuple[ForeignKeySpec, ...] = (
    ForeignKeySpec("farmer_profile", "user_id", "user", "id"),
    ForeignKeySpec("ai_message", "conversation_id", "ai_conversation", "id"),
)


def spec(table: str) -> TableSpec:
    """按表名取规格；未知表名报错而不是静默跳过。"""
    try:
        return SPEC_BY_NAME[table]
    except KeyError:
        raise KeyError(f"未登记的表：{table}，请先在 TABLE_SPECS 中补充列规格") from None


__all__ = [
    "FOREIGN_KEYS",
    "SERIAL_TABLES",
    "SPEC_BY_NAME",
    "TABLE_ORDER",
    "TABLE_SPECS",
    "UNBOUND_DEVICE_FILTER",
    "ForeignKeySpec",
    "TableSpec",
    "spec",
]

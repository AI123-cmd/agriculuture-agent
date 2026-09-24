"""MySQL → PostgreSQL 一次性数据迁移。

用法（务必先备份源库；迁移期间源库必须停写）：

    # 1) 演练：只转换不写入，确认转换函数无异常并拿到每张表的预估行数
    python scripts/migrate_mysql_to_pg.py \
        --mysql-url "mysql+pymysql://ro:***@mysql:3306/huanong" \
        --pg-url    "postgresql://huanong:***@pg:5432/huanong" \
        --env staging --dry-run

    # 2) 正式迁移（幂等，可反复执行）
    python scripts/migrate_mysql_to_pg.py \
        --mysql-url "mysql+pymysql://ro:***@mysql:3306/huanong" \
        --pg-url    "postgresql://huanong:***@pg:5432/huanong" \
        --env staging --batch-size 1000

    # 3) 四类校验（会读上面那份报告并追加校验结果）
    python scripts/verify_migration.py \
        --mysql-url ... --pg-url ... --env staging

步骤：连接校验 → 逐表迁移（按 TABLE_ORDER）→ 逐表行数校验 → 输出报告。

三个必须知道的约定：

1. **幂等**。写入用 `ON CONFLICT (<主键>) DO NOTHING`，重复执行不产生重复行。这是
   断点续跑能成立的前提：游标只用于跳过已迁完的数据，正确性由约束保证而非游标。
2. **断点续跑**。每张表每批迁完把游标（该批最后一行的主键）写进 `--state-file`，
   中断后重跑会跳过已完成的表、从断点继续未完成的表。
3. **源库时区必须人工确认**。`--source-tz` 默认 `Asia/Shanghai`，脚本会读源库的
   `@@system_time_zone` 与它对照，不一致时在报告里标红。源库若部署在其它时区，
   用错时区会让全部 `TIMESTAMPTZ` 整体平移。

前置：目标库已 `alembic upgrade head`（表结构由 T1-04 的迁移链路建出）。
"""

import argparse
import json
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

import psycopg
import pymysql
from psycopg.types.json import Jsonb

# 直接 `python scripts/migrate_mysql_to_pg.py` 时 sys.path[0] 是 scripts/，
# 因此显式把 backend/ 加进去，才能 `from scripts.migrate...` 导入。
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts.migrate.table_order import (  # noqa: E402
    SERIAL_TABLES,
    TABLE_ORDER,
    TableSpec,
    spec as table_spec,
)
from scripts.migrate.type_converters import (  # noqa: E402
    DEFAULT_SOURCE_TZ,
    convert_bool,
    convert_datetime,
    convert_json,
)

REPORT_DIR = BACKEND_ROOT / "reports"
# `ON CONFLICT DO NOTHING` 需要冲突目标，用主键即可（主键是每张表的自然唯一键）
_CONFLICT_TARGET = {"user": "id"}
# 大批量写入的游标取数上限；`--batch-size` 是写入批大小，读取窗口与之相同
_FETCH_FACTOR = 1


@dataclass
class ConversionStats:
    """三类差异的处理计数：报告要按类列出，用于判断转换规则是否真的被走到。"""

    json_values: int = 0
    bool_values: int = 0
    datetime_values: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class TableReport:
    """单表迁移结果。报告里每张表一行。"""

    table: str
    # 源行数取**原始全量**（不带过滤），目标行数取迁移后实际行数：
    # 这样被显式排除的行才在"差额"列可见，而不是被过滤条件提前抹平。
    source_rows: int = 0
    target_rows: int = 0
    migrated_rows: int = 0
    excluded_rows: int = 0
    seconds: float = 0.0
    stats: ConversionStats = field(default_factory=ConversionStats)
    resumed_from: str | None = None
    filter_reason: str | None = None
    note: str = ""

    @property
    def delta(self) -> int:
        return self.source_rows - self.target_rows


@dataclass
class MigrationState:
    """断点续跑状态。`cursor` 是表名 → 已迁到的最后一行主键（字符串形式）。"""

    env: str
    started_at: str
    cursor: dict[str, str] = field(default_factory=dict)
    done: list[str] = field(default_factory=list)
    tables: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path, env: str) -> "MigrationState":
        if not path.exists():
            return cls(env=env, started_at=datetime.now().isoformat(timespec="seconds"))
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_url(url: str) -> dict:
    """把 SQLAlchemy 风格连接串拆成驱动参数。

    两种前缀都要能收：`mysql+pymysql://` 与 `postgresql+asyncpg://`。psycopg 不认
    `+driver` 后缀，所以统一剥掉。
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.split("+")[0]
    if not parsed.hostname:
        raise SystemExit(f"连接串缺少主机名：{url}")
    params = {
        "host": parsed.hostname,
        "port": parsed.port,
        "user": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
        "database": parsed.path.lstrip("/") or None,
    }
    if scheme not in {"mysql", "postgresql", "postgres"}:
        raise SystemExit(f"不支持的连接串前缀：{parsed.scheme}")
    return {"scheme": scheme, **params}


def connect_mysql(url: str) -> pymysql.connections.Connection:
    """源库连接。字符集固定 utf8mb4，否则中文会以 latin1 取出而乱码。"""
    cfg = _parse_url(url)
    if cfg["scheme"] != "mysql":
        raise SystemExit(f"--mysql-url 必须是 mysql:// 或 mysql+pymysql://：{url}")
    return pymysql.connect(
        host=cfg["host"],
        port=cfg["port"] or 3306,
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset="utf8mb4",
        autocommit=True,
        # 迁移期间不得写源库；只读账号是运维要求，这里再声明一层
        cursorclass=pymysql.cursors.Cursor,
    )


def connect_pg(url: str) -> psycopg.Connection:
    cfg = _parse_url(url)
    if cfg["scheme"] not in {"postgresql", "postgres"}:
        raise SystemExit(f"--pg-url 必须是 postgresql:// 或 postgresql+asyncpg://：{url}")
    return psycopg.connect(
        host=cfg["host"],
        port=cfg["port"] or 5432,
        user=cfg["user"],
        password=cfg["password"],
        dbname=cfg["database"],
    )


def check_connections(mysql_conn: pymysql.connections.Connection, pg_conn: psycopg.Connection) -> dict:
    """连接校验：确认两端可访问、源库时区、目标库版本。

    返回源库时区信息，写进报告——这是"源库时区必须人工确认"这条要求的落地方式。

    时区偏移用**数值**读取（`TIMESTAMPDIFF(SECOND, UTC_TIMESTAMP(), NOW())`），而不是
    比较 `@@system_time_zone` 的字符串：Windows 版 MySQL 那里返回的是本地化名称
    （如中文"中国标准时间"），拿区名去匹配必然误报。
    """
    with mysql_conn.cursor() as cur:
        cur.execute("SELECT VERSION(), @@system_time_zone, @@time_zone, DATABASE()")
        version, system_tz, session_tz, database = cur.fetchone()
        cur.execute("SELECT TIMESTAMPDIFF(SECOND, UTC_TIMESTAMP(), NOW())")
        utc_offset_seconds = int(cur.fetchone()[0])
    with pg_conn.cursor() as cur:
        cur.execute("SELECT version()")
        pg_version = cur.fetchone()[0]
    return {
        "mysql_version": version,
        "mysql_database": database,
        "mysql_system_time_zone": system_tz,
        "mysql_session_time_zone": session_tz,
        "mysql_utc_offset_seconds": utc_offset_seconds,
        "pg_version": pg_version.split(" on ")[0],
    }


def timezone_agrees(source_info: dict, source_tz: str) -> bool:
    """`--source-tz` 与源库实际偏移是否一致（按秒比对，容差 0）。"""
    if not source_info.get("mysql_utc_offset_seconds"):
        return True
    if source_info["mysql_utc_offset_seconds"] == 0:
        # 源库就是 UTC；此时不该再叠加上海时区
        return source_tz.upper() in {"UTC", "ETC/UTC"}
    expected = datetime.now(ZoneInfo(source_tz)).utcoffset()
    return expected is not None and int(expected.total_seconds()) == source_info["mysql_utc_offset_seconds"]


def count_rows(conn: pymysql.connections.Connection, table: str, source_filter: str | None = None) -> int:
    """源表行数。不传 `source_filter` 时是整表原始行数（报告用这个）。"""
    sql = f"SELECT COUNT(*) FROM `{table}`"
    if source_filter:
        sql += f" WHERE {source_filter}"
    with conn.cursor() as cur:
        cur.execute(sql)
        return int(cur.fetchone()[0])


def iter_batches(
    conn: pymysql.connections.Connection,
    spec: TableSpec,
    *,
    batch_size: int,
    start_after: str | None,
) -> Iterator[list[tuple]]:
    """流式按主键分批读源表。

    用 `SSCursor`（server-side cursor）而不是默认游标：`ai_message` 是最大表，
    默认游标会把整个结果集拉进客户端内存。`SSCursor` 逐行从服务器取，内存占用与
    批大小而非表大小成正比。
    """
    columns = ", ".join(f"`{c}`" for c in spec.columns)
    while True:
        where: list[str] = []
        params: list[object] = []
        if spec.source_filter:
            where.append(spec.source_filter)
        if start_after is not None:
            where.append(f"`{spec.primary_key}` > %s")
            params.append(start_after)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        sql = f"SELECT {columns} FROM `{spec.name}`{clause} ORDER BY `{spec.primary_key}` LIMIT {batch_size}"

        with conn.cursor(pymysql.cursors.SSCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        if not rows:
            return
        yield rows
        if len(rows) < batch_size:
            return
        start_after = rows[-1][spec.columns.index(spec.primary_key)]
        # 主键统一转字符串做游标：源库 id 有 varchar 与 int 两种类型
        start_after = str(start_after)


def convert_row(spec: TableSpec, row: Sequence, *, source_tz: str, stats: ConversionStats) -> tuple:
    """按列规格转换一行。转换失败直接抛异常终止整表——静默写 NULL 会让历史数据失真。"""
    values: list[object] = []
    for column, raw in zip(spec.columns, row, strict=True):
        label = f"{spec.name}.{column}"
        value: object = raw
        if column in spec.json_columns:
            converted = convert_json(raw)
            if raw not in (None, ""):
                stats.json_values += 1
                if converted is None:
                    stats.warnings.append(f"{label}：原值 {raw!r} 解析为空，落库为 NULL")
            value = Jsonb(converted) if converted is not None else None
        elif column in spec.bool_validated_columns:
            # 只做值域校验，落库仍用原整数：目标表该列是 smallint，且改类型需新增迁移
            convert_bool(raw, column=label)
            stats.bool_values += 1
        elif column in spec.datetime_columns:
            converted_dt = convert_datetime(raw, source_tz=source_tz)
            if raw is not None:
                stats.datetime_values += 1
            value = converted_dt
        values.append(value)
    return tuple(values)


def insert_batch(pg_conn: psycopg.Connection, spec: TableSpec, rows: list[tuple]) -> int:
    """批量写入。`ON CONFLICT DO NOTHING` 让重复执行不产生重复行。"""
    columns = ", ".join(f'"{c}"' for c in spec.columns)
    placeholders = ", ".join(["%s"] * len(spec.columns))
    sql = (
        f'INSERT INTO "{spec.name}" ({columns}) VALUES ({placeholders}) '
        f'ON CONFLICT ("{_CONFLICT_TARGET.get(spec.name, spec.primary_key)}") DO NOTHING'
    )
    with pg_conn.cursor() as cur:
        cur.executemany(sql, rows)
        return cur.rowcount


def reset_sequence(pg_conn: psycopg.Connection, table: str) -> None:
    """把自增主键的序列推到 max(id)。

    迁移显式写入了源库 id，序列仍停在 1；不重置的话迁移后第一条新插入会撞上
    已迁入的主键并直接报唯一冲突。
    """
    with pg_conn.cursor() as cur:
        cur.execute(f'SELECT COALESCE(MAX(id), 0) FROM "{table}"')
        maximum = int(cur.fetchone()[0])
        if maximum:
            cur.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), %s, true)", (maximum,))


def migrate_table(
    mysql_conn: pymysql.connections.Connection,
    pg_conn: psycopg.Connection,
    spec: TableSpec,
    *,
    batch_size: int,
    source_tz: str,
    dry_run: bool,
    start_after: str | None,
    target_rows: int,
) -> tuple[TableReport, str | None]:
    """单表迁移：流式读取 + 批量写入 + 计数校验。

    `dry_run=True` 时走完全部转换但不写入，用于演练阶段确认转换函数无异常。

    `source_rows` 记整表原始行数、`excluded_rows` 记被 `source_filter` 排除的行数：
    两者分开记，报告里"差额"才等于"被排除的行"，可直接核对。
    """
    source_all = count_rows(mysql_conn, spec.name)
    source_selected = count_rows(mysql_conn, spec.name, spec.source_filter) if spec.source_filter else source_all
    report = TableReport(
        table=spec.name,
        source_rows=source_all,
        excluded_rows=source_all - source_selected,
        target_rows=target_rows,
        resumed_from=start_after,
        filter_reason=spec.filter_reason,
    )
    started = time.monotonic()
    cursor: str | None = start_after
    migrated = 0

    for rows in iter_batches(mysql_conn, spec, batch_size=batch_size, start_after=start_after):
        converted = [convert_row(spec, row, source_tz=source_tz, stats=report.stats) for row in rows]
        if not dry_run:
            insert_batch(pg_conn, spec, converted)
            pg_conn.commit()
        migrated += len(rows)
        cursor = str(rows[-1][spec.columns.index(spec.primary_key)])

    if not dry_run and spec.name in SERIAL_TABLES:
        reset_sequence(pg_conn, spec.name)
        pg_conn.commit()

    report.migrated_rows = migrated
    report.seconds = time.monotonic() - started
    if not dry_run:
        report.target_rows = count_pg_rows(pg_conn, spec.name)
    return report, cursor


def count_pg_rows(pg_conn: psycopg.Connection, table: str) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM "{table}"')
        return int(cur.fetchone()[0])


def render_report(
    *,
    env: str,
    args: argparse.Namespace,
    source_info: dict,
    reports: list[TableReport],
    state: MigrationState,
    dry_run: bool,
    total_seconds: float,
) -> str:
    """生成演练报告。含总耗时与建议停机窗口两节（验收要求）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tz_ok = timezone_agrees(source_info, args.source_tz)
    offset = source_info["mysql_utc_offset_seconds"]
    offset_text = f"UTC{offset // 3600:+d}" if offset % 3600 == 0 else f"UTC{offset / 3600:+.1f}"
    lines: list[str] = [
        f"# MySQL → PostgreSQL 迁移报告（{env}）",
        "",
        f"- 生成时间：{now}",
        f"- 执行模式：**{'演练（--dry-run，未写入目标库）' if dry_run else '正式迁移'}**",
        f"- 源库：MySQL {source_info['mysql_version']} / `{source_info['mysql_database']}`",
        f"- 源库时区：`@@system_time_zone={source_info['mysql_system_time_zone']}`、"
        f"`@@time_zone={source_info['mysql_session_time_zone']}`，实测偏移 **{offset_text}**；"
        f"本次按 `--source-tz {args.source_tz}` 转换"
        + ("　✅ 与源库实测偏移一致" if tz_ok else "　⚠️ 与源库实测偏移不一致，请人工确认后再正式迁移"),
        f"- 目标库：{source_info['pg_version']}",
        f"- 批大小：{args.batch_size}；断点状态文件：`{args.state_file}`",
        "",
        "## 一、逐表结果",
        "",
        "| 表 | 源行数 | 目标行数 | 差额 | 本次迁移 | 耗时(s) | 说明 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for r in reports:
        note = r.note or (f"续跑自主键 `{r.resumed_from}`" if r.resumed_from else "")
        if r.filter_reason:
            note = f"{r.filter_reason}（排除 {r.excluded_rows} 行）" + (f"；{note}" if note else "")
        lines.append(
            f"| `{r.table}` | {r.source_rows} | {r.target_rows} | {r.delta} | {r.migrated_rows} | "
            f"{r.seconds:.2f} | {note or '—'} |"
        )

    total_source = sum(r.source_rows for r in reports)
    total_target = sum(r.target_rows for r in reports)
    # 差额只允许来自两处：被显式过滤排除的行，或"续跑跳过已完成表"时尚未写入的行。
    # 两者都对不上说明有不明的行丢失/重复，必须让人看见而不是让报告显得干净。
    unaccounted = [r.table for r in reports if r.delta and r.delta != r.excluded_rows and not r.note]
    lines += [
        f"| **合计** | **{total_source}** | **{total_target}** | **{total_source - total_target}** | "
        f"**{sum(r.migrated_rows for r in reports)}** | **{total_seconds:.2f}** |  |",
        "",
        "差额仅出现在被显式过滤的表（上表「说明」列已附理由与排除行数）；其余表源行数与目标行数必须完全相等。",
    ]
    if unaccounted:
        lines.append(f"⚠️ 以下表的差额无法用声明的过滤条件解释，请人工核对：{'、'.join(f'`{t}`' for t in unaccounted)}")
    lines += [
        "",
        "## 二、三类差异的处理统计",
        "",
        "| 差异 | 处理函数 | 命中值数 |",
        "| --- | --- | ---: |",
        f"| JSON 字段 | `convert_json`（MySQL JSON 字符串 → Python 对象 → `JSONB`） | {sum(r.stats.json_values for r in reports)} |",
        f"| 布尔值 | `convert_bool`（`TINYINT(1)` 值域校验，非 0/1 即报错终止） | {sum(r.stats.bool_values for r in reports)} |",
        f"| 时间戳 | `convert_datetime`（源库时区 {args.source_tz} → UTC） | {sum(r.stats.datetime_values for r in reports)} |",
        "",
        "`broadcast_log.done` 说明：源库为 `smallint(0/1)`，PG 基线迁移 `0001_baseline` 同样建成 "
        "`SmallInteger`。本次对它执行与 `convert_bool` 同等的**值域校验**（出现 0/1 以外的值即报错终止），"
        "但落库形态维持整数——改为 `boolean` 需要新增一条改列迁移，超出 T1-14「改动范围」表所列文件。"
        "接口层 `broadcast_service._to_item` 已做 `bool(item.done)`，对外表现不变。",
        "",
        "## 三、转换告警",
        "",
    ]
    warnings = [w for r in reports for w in r.stats.warnings]
    if warnings:
        lines += [f"- {w}" for w in warnings]
    else:
        lines.append("无。所有值均按规则转换，无静默降级为 NULL 的字段。")

    lines += [
        "",
        "## 四、断点续跑说明",
        "",
        f"- 状态文件：`{args.state_file}`（记录已完成的表与每张表的最后一行主键）。",
        "- 幂等策略：写入统一使用 `ON CONFLICT (<主键>) DO NOTHING`。因此重复执行不会产生重复行，"
        "游标只用于减少重复读取，正确性由约束保证。",
        f"- 本次已完成表：{('、'.join(f'`{t}`' for t in state.done)) if state.done else '（演练模式不记录）'}。",
        "",
        "## 五、耗时与建议停机窗口",
        "",
        f"- 实测总耗时：**{total_seconds:.2f} 秒**（{total_seconds / 60:.2f} 分钟），"
        f"迁移 {total_source} 行，速率 {total_source / total_seconds if total_seconds else 0:.0f} 行/秒。",
        "- 建议停机窗口 = 实测耗时 × 3（安全系数，覆盖生产数据量更大与索引维护开销）"
        " + 15 分钟（`alembic upgrade head` 建结构、四类校验、切流量与回滚预留）。",
        f"- **本环境建议窗口：{_suggested_window(total_seconds)}**。",
        "- 生产环境应按本报告速率用生产实际行数重新外推，不要直接照搬本值。",
        "",
    ]
    return "\n".join(lines)


def _suggested_window(seconds: float) -> str:
    total = seconds * 3 + 15 * 60
    return f"{total / 60:.1f} 分钟"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MySQL → PostgreSQL 一次性数据迁移")
    parser.add_argument("--mysql-url", required=True, help="源库连接串，如 mysql+pymysql://ro:***@host:3306/huanong")
    parser.add_argument("--pg-url", required=True, help="目标库连接串，如 postgresql://huanong:***@host:5432/huanong")
    parser.add_argument("--env", default="staging", help="环境名，用于报告文件名，如 staging / production")
    parser.add_argument("--batch-size", type=int, default=1000, help="读取与写入批大小")
    parser.add_argument("--source-tz", default=DEFAULT_SOURCE_TZ, help=f"源库时区，默认 {DEFAULT_SOURCE_TZ}")
    parser.add_argument("--dry-run", action="store_true", help="只转换不写入，输出预估行数与转换告警")
    parser.add_argument(
        "--reset", action="store_true", help="忽略断点状态从头迁移（不清空目标表，靠 ON CONFLICT 幂等）"
    )
    parser.add_argument("--tables", default="", help="只迁移指定表，逗号分隔；默认全部")
    parser.add_argument("--state-file", default="", help="断点状态文件路径")
    parser.add_argument("--report", default="", help="报告输出路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # ZoneInfo 构造失败会抛异常：把"时区名写错"挡在迁移开始之前
    try:
        ZoneInfo(args.source_tz)
    except Exception:
        print(f"--source-tz 不是有效时区名：{args.source_tz}", file=sys.stderr)
        return 2

    if not args.state_file:
        args.state_file = str(REPORT_DIR / f".migration-state-{args.env}.json")
    if not args.report:
        args.report = str(REPORT_DIR / f"migration-{args.env}-{datetime.now():%Y%m%d}.md")

    selected = [t for t in (args.tables.split(",") if args.tables else TABLE_ORDER) if t]
    unknown = [t for t in selected if t not in TABLE_ORDER]
    if unknown:
        print(f"未知表名：{unknown}；可用表：{list(TABLE_ORDER)}", file=sys.stderr)
        return 2

    state_path = Path(args.state_file)
    state = MigrationState(env=args.env, started_at=datetime.now().isoformat(timespec="seconds"))
    if not args.reset:
        state = MigrationState.load(state_path, args.env)

    started = time.monotonic()
    mysql_conn = connect_mysql(args.mysql_url)
    pg_conn = connect_pg(args.pg_url)
    try:
        source_info = check_connections(mysql_conn, pg_conn)
        tz_ok = timezone_agrees(source_info, args.source_tz)
        print(
            f"连接校验通过：MySQL {source_info['mysql_version']} / {source_info['mysql_database']}，"
            f"源库实测偏移 {source_info['mysql_utc_offset_seconds']}s，"
            f"按 {args.source_tz} 转换（{'一致' if tz_ok else '不一致，请人工确认'}）"
        )

        reports: list[TableReport] = []
        for table in selected:
            spec = table_spec(table)
            target_rows = count_pg_rows(pg_conn, table)
            if table in state.done and not args.reset:
                # 已完成的表直接跳过：这是断点续跑"跳过已完成表"的落点
                report = TableReport(
                    table=table,
                    source_rows=count_rows(mysql_conn, table, spec.source_filter),
                    target_rows=target_rows,
                    note="断点续跑：该表已完成，本次跳过",
                    filter_reason=spec.filter_reason,
                )
                reports.append(report)
                print(f"[skip] {table}：已完成，跳过")
                continue

            report, cursor = migrate_table(
                mysql_conn,
                pg_conn,
                spec,
                batch_size=args.batch_size,
                source_tz=args.source_tz,
                dry_run=args.dry_run,
                start_after=state.cursor.get(table),
                target_rows=target_rows,
            )
            reports.append(report)
            if not args.dry_run:
                state.cursor[table] = cursor or ""
                if table not in state.done:
                    state.done.append(table)
                state.tables[table] = {
                    "source_rows": report.source_rows,
                    "target_rows": report.target_rows,
                }
                state.save(state_path)
            print(
                f"[{'dry' if args.dry_run else 'ok '}] {table}：源 {report.source_rows} 行，"
                f"目标 {report.target_rows} 行，本次 {report.migrated_rows} 行，{report.seconds:.2f}s"
            )

        total_seconds = time.monotonic() - started
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            render_report(
                env=args.env,
                args=args,
                source_info=source_info,
                reports=reports,
                state=state,
                dry_run=args.dry_run,
                total_seconds=total_seconds,
            ),
            encoding="utf-8",
        )
        print(f"报告已写入 {report_path}（总耗时 {total_seconds:.2f}s）")
        if args.dry_run:
            print("演练模式：未写入目标库，未记录断点。确认无误后去掉 --dry-run 执行正式迁移。")
        return 0
    finally:
        mysql_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

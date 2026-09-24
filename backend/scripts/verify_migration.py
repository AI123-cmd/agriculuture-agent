"""迁移后校验：行数、抽样字段、外键完整、时区正确（T1-14）。

用法：

    python scripts/verify_migration.py \
        --mysql-url "mysql+pymysql://ro:***@mysql:3306/huanong" \
        --pg-url    "postgresql://huanong:***@pg:5432/huanong" \
        --env staging

四类校验的通过阈值来自任务文件：

| 校验 | 方法 | 通过阈值 |
| --- | --- | --- |
| 行数一致 | 逐表双侧 `COUNT(*)` 比对 | 完全相等（被显式过滤的表记录差额与理由） |
| 抽样字段 | 每表随机 100 行，逐字段比对转换后值 | 100% 一致 |
| 外键完整 | 迁移后所有外键关系无孤儿行 | 0 条孤儿 |
| 时区正确 | 抽一条已知时间的记录断言 PG 与源时间差 8 小时 | 差值在 ±1 秒内 |

另含归属非空校验：`OwnedMixin` 要求 `user_id` 非空，源库若有缺归属的历史行必须
在迁移前修复或明确丢弃，否则 `NOT NULL` 约束根本加不上。

校验结果以退出码表达：全部通过返回 0，任一不通过返回 1，可直接接进发布流程。
"""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts.migrate.table_order import (  # noqa: E402
    FOREIGN_KEYS,
    TABLE_ORDER,
    spec as table_spec,
)
from scripts.migrate.type_converters import DEFAULT_SOURCE_TZ, convert_datetime, convert_json  # noqa: E402

# 每表抽样行数（任务文件要求 100）
SAMPLE_SIZE = 100
# 时区校验允许的误差
TZ_TOLERANCE_SECONDS = 1


class CheckResult:
    """一类校验的结果。`passed` 决定退出码，`detail` 写进报告。"""

    def __init__(self, name: str, passed: bool, detail: str, rows: list[str] | None = None) -> None:
        self.name = name
        self.passed = passed
        self.detail = detail
        self.rows = rows or []


def _mysql_module():
    """延迟导入 pymysql：只为让 `--help` 与导入期检查在没装 pymysql 时也能跑。

    pymysql 是开发依赖（一次性迁移工具用），不保证运行环境有。
    """
    import pymysql

    return pymysql


def _parse_pg_url(url: str) -> str:
    """psycopg 不认 `+driver` 后缀，剥掉。"""
    return url.replace("+asyncpg", "").replace("+psycopg", "")


def _parse_mysql_url(url: str) -> dict:
    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
        "database": parsed.path.lstrip("/") or None,
    }


def connect_mysql(url: str):
    pymysql = _mysql_module()
    cfg = _parse_mysql_url(url)
    return pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset="utf8mb4",
        autocommit=True,
    )


def check_row_counts(mysql_conn, pg_conn) -> CheckResult:
    """校验一：逐表行数双侧比对。

    源行数取**整表全量**，与目标行数直接相减：被显式过滤排除的行因此出现在差额里，
    `device` 这类表才能看出"源 2 行、目标 1 行、差额 1 行 = 未绑定设备"。若源侧也
    套用过滤条件，差额会恒为 0，这条校验就对"过滤是否真的生效"失去判别力。
    """
    rows: list[str] = []
    ok = True
    with pg_conn.cursor() as pcur:
        for table in TABLE_ORDER:
            spec = table_spec(table)
            with mysql_conn.cursor() as mcur:
                mcur.execute(f"SELECT COUNT(*) FROM `{table}`")
                source = int(mcur.fetchone()[0])
            pcur.execute(f'SELECT COUNT(*) FROM "{table}"')
            target = int(pcur.fetchone()[0])

            delta = source - target
            if delta == 0:
                rows.append(f"| `{table}` | {source} | {target} | 0 | ✅ | — |")
            elif spec.filter_reason:
                rows.append(f"| `{table}` | {source} | {target} | {delta} | ✅ 差额已声明 | {spec.filter_reason} |")
            else:
                ok = False
                rows.append(f"| `{table}` | {source} | {target} | {delta} | ❌ 未声明的差额 | — |")
    detail = f"{len(TABLE_ORDER)} 张表逐表比对，" + ("全部一致或差额已声明并附理由" if ok else "存在未声明的差额")
    return CheckResult(
        "行数一致",
        ok,
        detail,
        ["| 表 | 源行数 | 目标行数 | 差额 | 结果 | 理由 |", "| --- | ---: | ---: | ---: | --- | --- |", *rows],
    )


def check_owner_not_null(pg_conn) -> CheckResult:
    """归属非空：`user_id` 为空的历史行会让 T1-05 的 `OwnedMixin` 约束加不上。"""
    rows: list[str] = []
    ok = True
    targets = [t for t in TABLE_ORDER if table_spec(t).owner_required]
    with pg_conn.cursor() as cur:
        for table in targets:
            cur.execute(f"SELECT COUNT(*) FROM \"{table}\" WHERE user_id IS NULL OR user_id = ''")
            bad = int(cur.fetchone()[0])
            if bad:
                ok = False
            rows.append(f"| `{table}` | {bad} | {'❌' if bad else '✅'} |")
    return CheckResult(
        "归属非空",
        ok,
        f"{len(targets)} 张带归属列的表全部非空" if ok else "存在缺归属的历史行，需在迁移前修复或明确丢弃",
        ["| 表 | user_id 为空的行数 | 结果 |", "| --- | ---: | --- |", *rows],
    )


def check_foreign_keys(pg_conn) -> CheckResult:
    """校验三：外键完整性（无孤儿行）。"""
    rows: list[str] = []
    ok = True
    with pg_conn.cursor() as cur:
        for fk in FOREIGN_KEYS:
            cur.execute(
                f'SELECT COUNT(*) FROM "{fk.child_table}" c '
                f'LEFT JOIN "{fk.parent_table}" p ON p."{fk.parent_column}" = c."{fk.child_column}" '
                f'WHERE p."{fk.parent_column}" IS NULL'
            )
            orphans = int(cur.fetchone()[0])
            if orphans:
                ok = False
            rows.append(
                f"| `{fk.child_table}.{fk.child_column}` → `{fk.parent_table}.{fk.parent_column}` | "
                f"{orphans} | {'❌' if orphans else '✅'} |"
            )
    return CheckResult(
        "外键完整",
        ok,
        "全部外键关系无孤儿行" if ok else "存在孤儿行，说明源数据本身违反外键",
        ["| 外键 | 孤儿行数 | 结果 |", "| --- | ---: | --- |", *rows],
    )


def check_timezone(mysql_conn, pg_conn, source_tz: str) -> CheckResult:
    """校验四：时区正确。取一条已知时间的记录，断言两侧相差即为源库时区偏移。

    做法不写死"差 8 小时"：读出源库的 `datetime`（naive，本地时间），按 `source_tz`
    转成 UTC 后与 PG 侧的 `TIMESTAMPTZ` 直接比较。这样校验对源库时区本身也是正确的，
    而且能直接暴露"忘了做时区转换"——那种情况下差值恰好是 -8 小时。
    """
    with mysql_conn.cursor() as mcur:
        mcur.execute("SELECT id, create_time FROM `user` ORDER BY create_time LIMIT 1")
        row = mcur.fetchone()
    if row is None:
        return CheckResult("时区正确", True, "源库 `user` 无数据，跳过（不是失败）")

    user_id, source_time = row
    expected = convert_datetime(source_time, source_tz=source_tz)
    with pg_conn.cursor() as pcur:
        pcur.execute('SELECT create_time FROM "user" WHERE id = %s', (user_id,))
        got = pcur.fetchone()
    if got is None:
        return CheckResult("时区正确", False, f"目标库缺 `user.id={user_id}`，无法校验")
    actual = got[0]
    if actual.tzinfo is None:
        actual = actual.replace(tzinfo=UTC)

    delta = abs((actual - expected).total_seconds())
    passed = delta <= TZ_TOLERANCE_SECONDS
    example = actual.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        "| 项 | 值 |",
        "| --- | --- |",
        f"| 源库记录 | `user.id={user_id}`，`create_time={source_time}`（{source_tz} 本地时间） |",
        f"| 期望（源时间按 {source_tz} 转 UTC） | `{expected.strftime('%Y-%m-%d %H:%M:%S')}` |",
        f"| 实际（PG `create_time AT TIME ZONE 'UTC'`） | `{example}` |",
        f"| 差值 | {delta:.3f} 秒（容差 ±{TZ_TOLERANCE_SECONDS} 秒） |",
    ]
    return CheckResult(
        "时区正确",
        passed,
        f"示例值 `{example}`，差值 {delta:.3f} 秒" if passed else f"差值 {delta:.3f} 秒超出容差",
        rows,
    )


def check_sampled_fields(mysql_conn, pg_conn, source_tz: str) -> CheckResult:
    """校验二：每表随机抽 100 行，逐字段比对转换后的值。

    抽样在**源库侧**用 `ORDER BY RAND() LIMIT 100` 完成，不把整表拉回客户端：
    `ai_message` 是最大表，`fetchall()` 全表会把内存吃光，与本项"流式读"的要求相悖。
    """
    rows: list[str] = []
    ok = True
    mismatches: list[str] = []
    with pg_conn.cursor() as pcur:
        for table in TABLE_ORDER:
            spec = table_spec(table)
            columns = spec.columns
            select_cols = ", ".join(f"`{c}`" for c in columns)
            where = f" WHERE {spec.source_filter}" if spec.source_filter else ""
            with mysql_conn.cursor() as mcur:
                mcur.execute(f"SELECT COUNT(*) FROM `{table}`{where}")
                total = int(mcur.fetchone()[0])
                if not total:
                    rows.append(f"| `{table}` | 0 | 0 | ✅ 无数据 |")
                    continue
                mcur.execute(f"SELECT {select_cols} FROM `{table}`{where} ORDER BY RAND() LIMIT {SAMPLE_SIZE}")
                sample = mcur.fetchall()

            pk_index = columns.index(spec.primary_key)
            col_list = ", ".join(f'"{c}"' for c in columns)
            checked = 0
            table_ok = True
            for source_row in sample:
                pk = source_row[pk_index]
                pcur.execute(f'SELECT {col_list} FROM "{table}" WHERE "{spec.primary_key}" = %s', (pk,))
                target_row = pcur.fetchone()
                if target_row is None:
                    table_ok = False
                    mismatches.append(f"{table}.{spec.primary_key}={pk} 在目标库缺失")
                    continue
                for column, raw, converted_back in zip(columns, source_row, target_row, strict=True):
                    if not _field_matches(spec, column, raw, converted_back, source_tz):
                        table_ok = False
                        mismatches.append(f"{table}.{column}（主键 {pk}）：源 {raw!r} → 目标 {converted_back!r}")
                checked += 1
            if not table_ok:
                ok = False
            rows.append(f"| `{table}` | {total} | {checked} | {'✅' if table_ok else '❌'} |")

    detail = f"抽样 {SAMPLE_SIZE} 行/表，逐字段比对" + ("全部一致" if ok else f"，{len(mismatches)} 处不一致")
    if mismatches:
        rows += ["", "不一致明细（最多列 20 条）：", *[f"- {m}" for m in mismatches[:20]]]
    return CheckResult("抽样字段", ok, detail, rows)


def _field_matches(spec, column: str, raw: object, target: object, source_tz: str) -> bool:
    """单个字段的比对。按列规格把目标值还原成源侧表述后比较。"""
    if column in spec.json_columns:
        expected = convert_json(raw if isinstance(raw, (bytes, str)) else raw)
        return target == expected
    if column in spec.datetime_columns:
        expected_dt = convert_datetime(raw, source_tz=source_tz)
        if expected_dt is None:
            return target is None
        if target is None:
            return False
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        return abs((target - expected_dt).total_seconds()) <= TZ_TOLERANCE_SECONDS
    if column in spec.bool_validated_columns:
        return target is not None and int(target) == int(raw)
    # `decimal` 与 `float` 的相等比较在 PG 侧会因 Numeric 精度产生假差异，统一转字符串
    if raw is None:
        return target is None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return target is not None and float(target) == float(raw)
    return str(target) == str(raw)


def render_report(env: str, results: list[CheckResult], source_tz: str) -> str:
    """把四类校验结果渲染成可归档的 markdown。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    overall = all(r.passed for r in results)
    lines = [
        f"# 迁移校验报告（{env}）",
        "",
        f"- 生成时间：{now}",
        f"- 源库时区：`{source_tz}`",
        f"- 总体结论：**{'全部通过 ✅' if overall else '存在未通过项 ❌'}**",
        "",
    ]
    for idx, result in enumerate(results, start=1):
        lines += [
            f"## {idx}. {result.name}　{'✅' if result.passed else '❌'}",
            "",
            result.detail,
            "",
            *result.rows,
            "",
        ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="迁移后四类校验")
    parser.add_argument("--mysql-url", required=True, help="源库连接串")
    parser.add_argument("--pg-url", required=True, help="目标库连接串")
    parser.add_argument("--env", default="staging", help="环境名，用于报告文件名")
    parser.add_argument("--source-tz", default=DEFAULT_SOURCE_TZ, help=f"源库时区，默认 {DEFAULT_SOURCE_TZ}")
    parser.add_argument("--report", default="", help="校验报告输出路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report_path = Path(
        args.report or BACKEND_ROOT / "reports" / f"migration-verify-{args.env}-{datetime.now():%Y%m%d}.md"
    )

    mysql_conn = connect_mysql(args.mysql_url)
    pg_conn = psycopg.connect(_parse_pg_url(args.pg_url))
    try:
        results = [
            check_row_counts(mysql_conn, pg_conn),
            check_owner_not_null(pg_conn),
            check_foreign_keys(pg_conn),
            check_timezone(mysql_conn, pg_conn, args.source_tz),
            check_sampled_fields(mysql_conn, pg_conn, args.source_tz),
        ]
    finally:
        mysql_conn.close()
        pg_conn.close()

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(args.env, results, args.source_tz), encoding="utf-8")

    for result in results:
        print(f"[{'PASS' if result.passed else 'FAIL'}] {result.name}：{result.detail}")
    print(f"校验报告已写入 {report_path}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

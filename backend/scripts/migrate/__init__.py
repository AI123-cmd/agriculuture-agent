"""MySQL → PostgreSQL 一次性迁移工具集（T1-14）。

三个模块：`type_converters` 放三类差异的纯转换函数，`table_order` 放表顺序与列规格，
主脚本 `scripts/migrate_mysql_to_pg.py` 与校验脚本 `scripts/verify_migration.py`
在 `scripts/` 下（按任务文件的改动范围表）。
"""

"""T1-01 依赖与运行环境基线：断言关键包可导入、版本满足下限、同步 MySQL 与 SQLite 驱动已移除。"""

import importlib
import re
from importlib.metadata import version
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# 运行依赖必须可导入，且不低于任务文件规定的下限
REQUIRED_PACKAGES: dict[str, tuple[str, ...]] = {
    "sqlalchemy": ("2", "0", "30"),
    "asyncpg": ("0", "29", "0"),
    "psycopg": ("3", "1", "18"),
    "alembic": ("1", "13", "0"),
    "pgvector": ("0", "3", "0"),
    "redis": ("5", "0", "0"),
    "arq": ("0", "26", "0"),
    "tenacity": ("8", "2", "0"),
    "structlog": ("24", "1", "0"),
    "jwt": ("2", "8", "0"),
}

CHECKPOINT_PACKAGES: tuple[str, ...] = (
    "langgraph",
    "langgraph.checkpoint.postgres",
)

# 本项删除的 SQLite 检查点驱动：运行依赖里彻底不再有，也不该能导入
REMOVED_PACKAGES: tuple[str, ...] = ("aiosqlite", "langgraph.checkpoint.sqlite")

# 只在开发依赖里存在的包：一次性运维工具（T1-14 的 MySQL 迁移）用，
# 不得进入运行依赖。它们"可导入"是允许的（开发者本机装了），
# 但 requirements.txt 不许出现，app/ 也不许导入——否则等于把
# 已淘汰的同步 MySQL 驱动又拉回了运行时。
DEV_ONLY_PACKAGES: tuple[str, ...] = ("pymysql",)


def _version_tuple(raw: str) -> tuple[int, ...]:
    """把 0.29.0 / 3.1.18 / 0.45b0 之类的版本串截到数字部分。"""
    numbers = re.findall(r"\d+", raw.split("+")[0])
    return tuple(int(n) for n in numbers)


@pytest.mark.parametrize(("module_name", "minimum"), REQUIRED_PACKAGES.items())
def test_required_package_importable_with_minimum_version(module_name: str, minimum: tuple[str, ...]) -> None:
    """关键包可导入，且版本不低于下限。"""
    importlib.import_module(module_name)
    installed = _version_tuple(version(module_name.replace("jwt", "PyJWT")))
    expected = tuple(int(p) for p in minimum)
    assert installed >= expected, f"{module_name} 版本 {installed} 低于要求的 {expected}"


@pytest.mark.parametrize("module_name", CHECKPOINT_PACKAGES)
def test_checkpoint_packages_importable(module_name: str) -> None:
    """PostgreSQL 检查点链路可用。"""
    importlib.import_module(module_name)


@pytest.mark.parametrize("module_name", REMOVED_PACKAGES)
def test_removed_packages_not_importable(module_name: str) -> None:
    """SQLite 检查点驱动必须已从运行依赖移除。"""
    with pytest.raises(ImportError):
        importlib.import_module(module_name)


@pytest.mark.parametrize("package_name", DEV_ONLY_PACKAGES)
def test_dev_only_packages_stay_out_of_runtime(package_name: str) -> None:
    """一次性迁移工具用的包只许待在开发依赖：运行依赖不声明、app/ 不导入。

    T1-01 的"运行环境已切到 PostgreSQL 全异步栈"结论必须保持成立；T1-14 的
    MySQL 迁移脚本是一次性运维工具，它引入的同步驱动不该被误读为运行期依赖。
    """
    assert package_name not in (BACKEND_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert package_name in (BACKEND_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")

    app_source = "\n".join(p.read_text(encoding="utf-8") for p in (BACKEND_ROOT / "app").rglob("*.py"))
    assert f"import {package_name}" not in app_source
    assert f"from {package_name}" not in app_source


def test_requirements_has_no_mysql_or_sqlite_checkpoint() -> None:
    """requirements.txt 不再引用 pymysql 与 langgraph-checkpoint-sqlite。"""
    content = (BACKEND_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "pymysql" not in content
    assert "langgraph-checkpoint-sqlite" not in content
    assert "postgresql+asyncpg" in (BACKEND_ROOT / ".env.example").read_text(encoding="utf-8")


def test_settings_env_defaults_to_development() -> None:
    """Settings().ENV 默认 development。"""
    from app.core.config import Settings

    assert Settings().ENV == "development"

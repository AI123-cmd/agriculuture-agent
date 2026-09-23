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

# 本项删除的同步 MySQL 驱动与 SQLite 检查点驱动
REMOVED_PACKAGES: tuple[str, ...] = ("pymysql", "aiosqlite", "langgraph.checkpoint.sqlite")


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
    """同步 MySQL 驱动与 SQLite 检查点驱动必须已从运行依赖移除。"""
    with pytest.raises(ImportError):
        importlib.import_module(module_name)


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

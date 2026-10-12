"""T6-08：`app/` 下不得有裸 `print(` 与裸标准库 `logging.*` 调用。

为什么用测试而不是靠自觉：全局约定第五节的完成定义要求"没有留下临时物，`print` 调试语句
在提交前清理干净"，而日志必须经 `app.observability` 的统一入口才能保证脱敏。一条静态断言
让这两件事在 CI 里持续成立。

豁免范围：

- `app/observability/logging.py`：它正是标准库 logging 的封装处（脱敏 formatter 挂在这里），
  必然出现 `logging.` 调用与标准库 logger 的构造。
- `alembic/`：迁移脚本，不在 `app/` 下，天然不被扫描。

`logger = logging.getLogger(__name__)` 这类**构造**不被禁止（很多模块仍用它记录），
被禁的是绕过统一入口的**直接调用** `logging.info(...)` 与文档明令清理的 `print(...)`。
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

# 直接调用标准库 logging 模块级函数的写法（绕过 structlog 入口）。
_LOGGING_LEVEL_FUNCS = frozenset({"info", "warning", "error", "debug", "exception", "critical", "log"})

# 允许出现标准库 logging 的封装文件。
_EXEMPT = {"observability/logging.py"}


def _iter_sources() -> list[Path]:
    return [p for p in APP_ROOT.rglob("*.py") if p.suffix == ".py"]


def _relative(path: Path) -> str:
    return path.relative_to(APP_ROOT).as_posix()


def test_no_print_calls_in_app() -> None:
    """`app/` 下无 `print(...)` 调用。"""
    offenders: list[str] = []
    for path in _iter_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                offenders.append(f"{_relative(path)}:{node.lineno}")
    assert offenders == [], f"发现裸 print 调用：{offenders}"


def test_no_direct_stdlib_logging_calls_in_app() -> None:
    """`app/` 下无 `logging.info(...)` 这类直接调用（`logging.getLogger` 不算）。"""
    offenders: list[str] = []
    for path in _iter_sources():
        if _relative(path) in _EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _LOGGING_LEVEL_FUNCS
                and isinstance(func.value, ast.Name)
                and func.value.id == "logging"
            ):
                offenders.append(f"{_relative(path)}:{node.lineno} logging.{func.attr}")
    assert offenders == [], f"发现绕过统一入口的标准库 logging 调用：{offenders}"


def test_exempt_file_actually_imports_stdlib_logging() -> None:
    """豁免清单里的文件确实存在且用到标准库 logging（防止豁免名写错而静默失效）。"""
    exempt_path = APP_ROOT / "observability" / "logging.py"
    assert exempt_path.exists()
    assert "import logging" in exempt_path.read_text(encoding="utf-8")

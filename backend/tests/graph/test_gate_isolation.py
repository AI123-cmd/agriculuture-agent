"""T2-45：闸门与对话/模型的隔离（AST 静态断言）。

技术方案 8.1 要求闸门"不接收会话消息、不调用大模型"，这是**物理隔离**：不是"约定不要
读"，而是代码里根本拿不到那些东西。本模块用 `ast` 扫描 `gate_graph.py` 的源码，把这条
约束钉在语法层：

1. `build_gate_context`（唯一读 state 的地方）与 `gate_node` 的函数体内不得出现
   `messages`、`reply` 标识符——多读一个字段就多一个被对话表述影响的入口；
2. 全模块不得导入 `gateway`、`llm` 相关模块——闸门无模型句柄，判定才可复现。

用静态扫描而非运行时断言，是因为"某条路径恰好没读到"不代表隔离成立；只有源码里不存在
这些名字，才能保证任何分支都读不到。
"""

import ast
from pathlib import Path

from app.agent.graph import gate_graph

_SOURCE = Path(gate_graph.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)

_FORBIDDEN_NAMES = {"messages", "reply"}
_FORBIDDEN_IMPORT_ROOTS = {"gateway", "llm"}


def _function_defs(tree: ast.AST) -> dict[str, ast.AST]:
    return {node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _used_identifiers(node: ast.AST) -> set[str]:
    """函数体内出现的标识符（`ast.Name`/`ast.Attribute`），不含字符串常量。

    刻意排除字符串常量：函数的 docstring 会以散文形式提到 `messages`/`reply` 来说明
    "刻意不读它们"，那不是实际引用。真正的引用只有标识符与 `state["reply"]` 这类下标。
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def _subscript_keys(node: ast.AST) -> set[str]:
    keys: set[str] = set()
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Subscript)
            and isinstance(child.slice, ast.Constant)
            and isinstance(child.slice.value, str)
        ):
            keys.add(child.slice.value)
    return keys


def test_gate_context_builder_does_not_read_conversation_fields() -> None:
    func = _function_defs(_TREE)["build_gate_context"]
    used = _used_identifiers(func) | _subscript_keys(func)
    assert used.isdisjoint(_FORBIDDEN_NAMES), f"build_gate_context 读到了会话字段: {used & _FORBIDDEN_NAMES}"


def test_gate_node_does_not_read_conversation_fields() -> None:
    func = _function_defs(_TREE)["gate_node"]
    used = _used_identifiers(func) | _subscript_keys(func)
    assert used.isdisjoint(_FORBIDDEN_NAMES), f"gate_node 读到了会话字段: {used & _FORBIDDEN_NAMES}"


def test_module_does_not_import_model_gateway() -> None:
    imported: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
    for module in imported:
        root = module.split(".")[0]
        assert root not in _FORBIDDEN_IMPORT_ROOTS, f"闸门模块不得导入 {module}"


def test_gate_graph_source_has_no_reply_assignment() -> None:
    """节点不得写 `reply`：面向农户的对话消息由主图与转人工节点产出，闸门不产生消息。"""
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            assert node.slice.value not in _FORBIDDEN_NAMES, f"gate_graph.py 引用了 {node.slice.value!r} 下标"

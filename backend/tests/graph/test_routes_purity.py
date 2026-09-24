"""T2-24：把四条路由约束里可机器化的两条钉死在测试上。

约束一（无 IO）与约束二（分支全覆盖）靠人眼审查会随版本漂移，因此这里用 AST 与
返回值集合做机器化核对：

1. **无 IO**：解析 `routes.py`，逐函数检查函数体内不出现 `await`、异步语法与标识符
   `db`、`gateway`、`httpx`；
2. **分支全覆盖**：`ast` 收集每个路由函数 `return` 的字符串常量，得到"可能返回值集合"，
   再与 `ROUTE_MAPS` 里配对的 path_map 键集合逐一比对，必须完全相等；每个函数体末尾必须
   是 `return` 或 `raise`，不允许隐式落到 `None`（那等于把未覆盖情形当成一个不存在的分支名）；
3. **模块依赖面**：文件顶部只导入 `collections.abc` 与 `app.agent.state`，不导入节点、服务
   或数据库对象——这样单测能在纯内存下跑。

纯静态用例，不依赖 PostgreSQL，也不触发 `app/agent/__init__.py` 的导入。
"""

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

from app.agent.graph.routes import ROUTE_MAPS

_ROUTES_PATH = Path(__file__).resolve().parents[2] / "app" / "agent" / "graph" / "routes.py"

# 出现即违反约束一：等待与网络/数据库客户端都让"同一条路径两次重放走向不同分支"。
_FORBIDDEN_NAMES = frozenset({"db", "gateway", "httpx"})
# 允许导入的模块前缀：状态与枚举之外的东西一律不许进路由模块。
_ALLOWED_IMPORT_PREFIXES = ("collections", "app.agent.state", "__future__")
_ROUTE_IDS = [route.__name__ for route, _ in ROUTE_MAPS]


def _source() -> str:
    return _ROUTES_PATH.read_text(encoding="utf-8")


def _parse_routes() -> ast.Module:
    return ast.parse(_source(), filename=str(_ROUTES_PATH))


def _route_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    """取出所有顶层 `route_*` 函数定义。"""
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("route_")
    }


def _returned_names(func: ast.FunctionDef) -> set[str]:
    """收集函数所有 `return <字符串常量>` 的取值。

    三元表达式（如 `return "confirm_object" if cond else "enter_process"`）的两个分支都是
    `ast.Constant`，因此遍历整棵子树而不是只看 `IfExp` 的直接子节点。
    """
    func_name: str = func.name
    assert func_name
    return {
        inner.value
        for node in ast.walk(func)
        if isinstance(node, ast.Return) and node.value is not None
        for inner in ast.walk(node.value)
        if isinstance(inner, ast.Constant) and isinstance(inner.value, str)
    }


def _raises_unrouted(func: ast.FunctionDef) -> bool:
    return any(
        isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "UnroutedStateError"
        for node in ast.walk(func)
    )


def test_routes_module_exists() -> None:
    assert _ROUTES_PATH.is_file(), f"路由模块缺失: {_ROUTES_PATH}"


def test_route_functions_are_discovered() -> None:
    """`ROUTE_MAPS` 里的九个函数都应能在源码里找到同名的顶层同步函数定义。"""
    defined = set(_route_functions(_parse_routes()))
    expected = set(_ROUTE_IDS)
    assert expected <= defined, f"源码里缺少定义: {sorted(expected - defined)}"


def test_all_routes_are_synchronous() -> None:
    """条件边刻意保持同步：LangGraph 允许 async 条件边，但本模块要求无 IO 无等待。"""
    tree = _parse_routes()
    async_routes = [
        node.name
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("route_")
    ]
    assert not async_routes, f"以下边被写成了 async: {async_routes}"


@pytest.mark.parametrize("func_name", _ROUTE_IDS)
def test_route_body_has_no_io(func_name: str) -> None:
    """约束一：函数体内不出现 `await`、异步语法、函数内导入与 `db`/`gateway`/`httpx`。"""
    func = _route_functions(_parse_routes())[func_name]
    violations: list[str] = []

    for node in ast.walk(func):
        if isinstance(node, ast.Await):
            violations.append(f"第 {node.lineno} 行出现 await")
        elif isinstance(node, (ast.AsyncFor, ast.AsyncWith, ast.AsyncFunctionDef)):
            violations.append(f"第 {node.lineno} 行出现异步语法 {type(node).__name__}")
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            violations.append(f"第 {node.lineno} 行访问了禁用标识符 {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
            violations.append(f"第 {node.lineno} 行访问了禁用属性 {node.attr}")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            violations.append(f"第 {node.lineno} 行出现函数内导入")

    assert not violations, f"{func_name} 违反「无 IO」约束: {violations}"


@pytest.mark.parametrize(
    ("route", "path_map"),
    ROUTE_MAPS,
    ids=_ROUTE_IDS,
)
def test_returned_names_match_path_map_keys(route: Callable[..., str], path_map: dict[str, str]) -> None:
    """约束二：可能返回值集合 == path_map 键集合。

    两边同时改错（新增分支时函数与 path_map 一起漏改）本用例发现不了，但那种改动会在
    `add_conditional_edges` 编译期因"分支名未注册"而失败，不会流到线上。
    """
    func = _route_functions(_parse_routes())[route.__name__]
    returned = _returned_names(func)
    assert returned == set(path_map), (
        f"{route.__name__} 的返回值 {sorted(returned)} 与 path_map 键 {sorted(path_map)} 不一致"
    )


@pytest.mark.parametrize(
    ("route", "path_map"),
    ROUTE_MAPS,
    ids=_ROUTE_IDS,
)
def test_path_map_targets_are_non_empty(route: Callable[..., str], path_map: dict[str, str]) -> None:
    """path_map 的值必须是节点名或子图出口，不允许空串之类的占位。"""
    assert path_map, f"{route.__name__} 缺少 path_map"
    assert all(target for target in path_map.values()), f"{route.__name__} 的 path_map 含空目标"


@pytest.mark.parametrize(("route", "path_map"), ROUTE_MAPS, ids=_ROUTE_IDS)
def test_route_body_ends_with_return_or_raise(route: Callable[..., str], path_map: dict[str, str]) -> None:
    """函数体末尾必须是 `return` 或 `raise`。

    隐式落到 `None` 等于把"未覆盖的组合"当成一个名为 None 的分支交回 LangGraph，
    运行时报错信息指向图层而看不出是路由漏判，正是约束二要避免的形态。
    """
    func = _route_functions(_parse_routes())[route.__name__]
    last = func.body[-1]
    assert isinstance(last, (ast.Return, ast.Raise)), (
        f"{route.__name__} 末尾是 {type(last).__name__}，存在隐式返回 None 的可能"
    )


@pytest.mark.parametrize(
    "func_name",
    ["route_after_confirm_object", "route_after_judge", "route_after_gate"],
)
def test_conditional_routes_raise_on_uncovered_state(func_name: str) -> None:
    """这三条边的条件无法穷尽（缺 `process_id`、置信等级未知、decision 未知），必须抛错。

    其余六条边用 if/else 已穷尽全部分支，不需要 `raise`；但凡是"条件不穷尽"的边，一律
    用 `UnroutedStateError` 兜住，不许 `return "default"`。
    """
    func = _route_functions(_parse_routes())[func_name]
    assert _raises_unrouted(func), f"{func_name} 没有 UnroutedStateError 分支"


def test_routes_import_only_state_and_collections() -> None:
    """导入面收窄到状态与枚举：导入本模块不应拉起任何外部依赖。"""
    tree = _parse_routes()
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module if node.level == 0 else "." * node.level + node.module)

    unexpected = [name for name in imported if not name.startswith(_ALLOWED_IMPORT_PREFIXES)]
    assert not unexpected, f"routes.py 导入了未允许的模块: {unexpected}"


def test_module_defines_only_the_expected_class() -> None:
    """除 `UnroutedStateError` 外不应再有类：新增类往往意味着某条边开始持有状态或依赖。"""
    classes = {node.name for node in _parse_routes().body if isinstance(node, ast.ClassDef)}
    assert classes <= {"UnroutedStateError"}, f"routes.py 出现预期外的类: {sorted(classes)}"


def test_no_import_of_agents_services_or_orm() -> None:
    """防止未来有人为了方便，在路由里导入智能体、服务或 ORM。"""
    source = _source()
    for forbidden in ("app.agent.agents", "app.services", "app.models", "app.gate", "sqlalchemy"):
        assert forbidden not in source, f"routes.py 出现禁止的依赖: {forbidden}"

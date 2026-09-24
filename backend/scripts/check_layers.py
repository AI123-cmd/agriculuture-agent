"""分层依赖检查：四条硬规则，违反即构建失败。

规则来自技术方案 15.2 与 00-全局约定第三节：L1 闸门不得依赖智能体/服务层、
L2 智能体不得直接导入 ORM、L3 模型网关不得依赖业务模块、L4 任何模块不得直接
`db.query(Model)`。

它是 T1-06 整改结果的长期守卫：没有这道门禁，42 处 `db.query` 会在两次迭代内
回到原样。脚本不解析 `# noqa`——行内豁免会让例外散落在各处且无人复查，
确有例外必须在 `check_layers_allowlist.toml` 登记并写明失效任务编号。
"""

import ast
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST_NAME = "check_layers_allowlist.toml"

# 白名单条目上限。例外超过这个数说明规则正在被架空，该回头改代码而不是继续加条目。
_DEFAULT_MAX_ALLOWLIST = 5
# L4 的合法入口（默认值，可被 pyproject.toml 的 [tool.check-layers] 覆盖）
_DEFAULT_DB_QUERY_ALLOWED = ("app/repositories/", "app/db/")
# 任务编号形如 T2-25。用结构化匹配而非"数一数有几个 T"：后者会把含两个任务号的
# 理由（"T2-25 与 T2-30 一起删除"）误判为不合规。
_TASK_ID = re.compile(r"\bT\d+-\d+\b")


@dataclass(frozen=True)
class LayerRule:
    """一条 import 方向禁令。"""

    name: str
    module_prefix: str  # 被检查的模块（如 app.gate）
    forbidden_prefixes: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class AllowEntry:
    """白名单条目：哪条规则、哪个路径前缀、何时失效。"""

    rule: str
    path: str  # 相对 backend/ 的路径前缀
    reason: str


LAYER_RULES = (
    LayerRule(
        "L1",
        "app.gate",
        ("app.agent", "app.services"),
        "闸门必须独立可测、可单独部署，这是生成与校验物理隔离的落地方式",
    ),
    LayerRule(
        "L2",
        "app.agent",
        ("app.models",),
        "智能体要能被单测（喂假数据），耦合 ORM 就无法脱离数据库测试",
    ),
    LayerRule(
        "L3",
        "app.llm",
        ("app.agent", "app.gate", "app.services"),
        "网关是通用组件，反向依赖会让它无法复用",
    ),
)

KNOWN_RULES = frozenset({rule.name for rule in LAYER_RULES} | {"L4"})


def allowlist_file() -> Path:
    """白名单路径。做成函数而非模块常量：测试会把 `ROOT` 换成临时目录。"""
    return ROOT / "scripts" / ALLOWLIST_NAME


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


def walk_py_files(prefix: str) -> list[Path]:
    """列出模块前缀下全部 `.py`。目录不存在返回空列表而非报错。

    第一步尚无 `app/gate/`、`app/llm/`，脚本必须能跑通，且要让"目录不存在"
    与"目录存在且合规"在输出上可区分——所以 `main` 会打印每条规则的扫描文件数，
    目录不存在时显示 0。
    """
    base = ROOT / prefix.replace(".", "/")
    return sorted(p for p in base.rglob("*.py") if p.is_file()) if base.exists() else []


def load_allowlist() -> list[AllowEntry]:
    if not allowlist_file().exists():
        return []
    data = tomllib.loads(allowlist_file().read_text(encoding="utf-8"))
    return [AllowEntry(e["rule"], e["path"], e["reason"]) for e in data.get("allow", [])]


def load_config() -> dict:
    """读 `pyproject.toml` 的 `[tool.check-layers]`，缺省时用内置默认值。

    把"哪些目录可以写 `db.query`"与"白名单上限"放进项目配置，是为了让调规则
    不必改脚本：规则是项目约定，脚本只是执行者。
    """
    pyproject = ROOT / "pyproject.toml"
    if not pyproject.exists():
        return {}
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data.get("tool", {}).get("check-layers", {})


def is_allowed(allow: list[AllowEntry], rule: str, rel: str) -> bool:
    return any(e.rule == rule and rel.startswith(e.path) for e in allow)


def _parse(path: Path, bad: list[str]) -> ast.Module | None:
    """解析文件；语法错误记为违规而不是抛栈，保证脚本可独立运行。"""
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        bad.append(f"[syntax] {_rel(path)}:{e.lineno}: 无法解析（{e.msg}）")
        return None


def parse_imports(path: Path, bad: list[str]) -> list[str]:
    """用 AST 取 import 目标。

    技术方案给的骨架是 `"db.query(" in source` 这类子串匹配，它会把注释与文档
    字符串里的示例判成违规（逼执行者删注释），又漏掉 `from X import Y`。
    AST 的意义不在防绕过（静态分析永远能绕过），而在"只报真实的代码表达式"，
    使门禁结论可被信任。

    只取 `level == 0` 的绝对导入：相对导入（`from .x import y`）的归属由所在
    包决定，模块前缀本身已经限定了范围。
    """
    tree = _parse(path, bad)
    if tree is None:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append(node.module)
    return out


def check_import_direction(allow: list[AllowEntry]) -> list[str]:
    """L1 / L2 / L3：import 方向禁令。"""
    bad: list[str] = []
    for rule in LAYER_RULES:
        for path in walk_py_files(rule.module_prefix):
            rel = _rel(path)
            if is_allowed(allow, rule.name, rel):
                continue
            for imp in parse_imports(path, bad):
                if any(imp == f or imp.startswith(f + ".") for f in rule.forbidden_prefixes):
                    bad.append(f"[{rule.name}] {rel}: 非法依赖 {imp} —— {rule.reason}")
    return bad


def check_db_query(allow: list[AllowEntry], config: dict) -> list[str]:
    """L4：只认 `ast.Call` + `ast.Attribute`，不认注释与字符串。

    `isinstance(node.func.value, ast.Name)` 把判定限定为"直接名为 `db` 的对象"，
    会漏掉 `self.db.query(...)`——这是刻意的：本项目规范里 `AsyncSession` 参数名
    固定为 `db`（00-全局约定第四节）。若放宽到"任意对象上有 query 方法"，
    会把 `request.query_params` 这类无关调用报成违规，门禁随即失去可信度。
    """
    bad: list[str] = []
    allowed_prefixes = tuple(config.get("db_query_allowed", _DEFAULT_DB_QUERY_ALLOWED))
    for path in walk_py_files("app"):
        rel = _rel(path)
        if any(rel.startswith(p) for p in allowed_prefixes) or is_allowed(allow, "L4", rel):
            continue
        tree = _parse(path, bad)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "query"
                and isinstance(node.func.value, ast.Name)
            ):
                bad.append(f"[L4] {rel}:{node.lineno}: 禁止直接 {node.func.value.id}.query()，请走 app/repositories/")
    return bad


def check_allowlist_hygiene(allow: list[AllowEntry], config: dict) -> list[str]:
    """白名单卫生：每条必须写明失效任务编号，规则名必须存在，条目数有上限。

    没有这三条的约束，白名单会从"临时例外"长成"永久豁免清单"。
    """
    bad: list[str] = []
    max_entries = int(config.get("max_allowlist_entries", _DEFAULT_MAX_ALLOWLIST))
    for entry in allow:
        if not _TASK_ID.search(entry.reason):
            bad.append(f"[allowlist] {entry.path} 的理由必须写明失效任务编号（如 T2-25）：{entry.reason!r}")
        if entry.rule not in KNOWN_RULES:
            bad.append(
                f"[allowlist] {entry.path} 的 rule={entry.rule!r} 不是已知规则"
                f"（{'/'.join(sorted(KNOWN_RULES))}）；规则名写错会让豁免静默失效"
            )
    if len(allow) > max_entries:
        bad.append(f"[allowlist] 条目数 {len(allow)} 超过上限 {max_entries}：例外过多说明规则已被架空")
    return bad


def scan_report() -> list[str]:
    """每条规则实际扫到的文件数，用来区分"目录不存在"与"目录存在且合规"。"""
    return [
        f"{rule.name} {rule.module_prefix}: {len(walk_py_files(rule.module_prefix))} 个文件" for rule in LAYER_RULES
    ]


def check() -> list[str]:
    allow = load_allowlist()
    config = load_config()
    return check_import_direction(allow) + check_db_query(allow, config) + check_allowlist_hygiene(allow, config)


def main() -> int:
    for line in scan_report():
        print(line)
    print(f"L4 app: {len(walk_py_files('app'))} 个文件")

    violations = check()
    if not violations:
        print("分层依赖检查通过")
        return 0
    print(f"分层依赖检查未通过，共 {len(violations)} 项：", file=sys.stderr)
    for line in violations:
        print(f"  {line}", file=sys.stderr)
    print(
        "不得使用 # noqa 跳过，确有例外请在 check_layers_allowlist.toml 登记并写明理由。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""分层检查的防绕过守卫（T6-11，技术方案 15.2）。

15.2 的原话是"这个脚本接入 CI，与 `ruff` / `mypy` 并列，**不允许 `# noqa` 跳过**"。
`check_layers.py` 本身用 AST 判定，行内注释天然影响不了它——但"加个 `# noqa` 试试"这个
动作一旦被默许，就会有人据此认为违规已被豁免，把真正该改的代码留在原地。本脚本把这条
约束变成可执行的断言，三条：

1. **`app/` 内的分层违规行不得出现 `# noqa`**。只报"含有违规代码的行"（跨层 import 或
   `db.query()`），不报 `except Exception:  # noqa: BLE001` 这类与分层无关的豁免——否则
   门禁会因几十处无关命中而失去可信度，最后被人整体忽略。
2. **CI 配置不得出现跳过分层检查的写法**。`continue-on-error: true` 比直接删掉检查更隐蔽：
   检查照跑、只是不阻断。唯一例外是评测回归作业在非主分支上的告警语义，且例外以
   **精确表达式**登记，主分支上仍必须阻断。
3. **白名单条目必须写明失效任务编号**（复用 `check_layers` 的卫生检查），使"临时例外"
   在评审时被看见，而不是长成永久豁免。

`# type: ignore` 不在禁令范围内：类型错误与分层依赖是两件事，混在一条禁令里会逼执行者
为过检查去删有效的类型豁免。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import yaml

# 允许从任意工作目录直接运行（CI 用 `python backend/scripts/check_no_noqa.py`）：
# `scripts` 是命名空间包，需要 backend/ 在 sys.path 上才能 `from scripts import check_layers`。
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from scripts import check_layers  # noqa: E402  路径注入必须在导入之前

ROOT = _BACKEND_ROOT
WORKFLOWS_DIR = ROOT.parent / ".github" / "workflows"

# 违规代码行若带这些 `noqa` 代码，说明作者以为能豁免分层规则。
_NOQA_TOKEN = "# noqa"

# `continue-on-error` 的唯一允许写法：评测回归作业在非主分支上降级为告警。
# 主分支（main/master）上该表达式为 false，门禁仍然阻断——这是"允许告警"与
# "允许绕过"的分界线，写死在这里而不是按文件名放行，避免有人把检查整体挪进
# eval-regression.yml 就绕过了。
_ALLOWED_CONTINUE_ON_ERROR = {
    "eval-regression.yml": (
        "${{ !(github.ref == 'refs/heads/main' || github.ref == 'refs/heads/master') }}"
    ),
}

def _has_noqa(line: str) -> bool:
    """该行是否带 `# noqa`。

    只扫"确实违规的行"，因此不需要按代码名单过滤——`except Exception:  # noqa: BLE001`
    这类与分层无关的豁免根本不在违规行集合里，不会被误报。
    """
    return _NOQA_TOKEN in line


def _violating_lines(path: Path, rel: str) -> dict[int, str]:
    """该文件里"确实违反分层规则"的行号 → 说明。

    规则与 `check_layers` 同源（同一份 `LAYER_RULES` 与 `db_query_allowed` 配置），
    本函数只是补上**行号**，因为防绕过必须精确定位到"哪一行加了 noqa"。
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    hits: dict[int, str] = {}

    for rule in check_layers.LAYER_RULES:
        prefix = rule.module_prefix.replace(".", "/") + "/"
        if not rel.startswith(prefix):
            continue
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                targets = [node.module]
            for target in targets:
                if any(target == f or target.startswith(f + ".") for f in rule.forbidden_prefixes):
                    hits[node.lineno] = f"[{rule.name}] 非法依赖 {target}"

    config = check_layers.load_config()
    allowed = tuple(config.get("db_query_allowed", ("app/repositories/", "app/db/")))
    if not rel.startswith(allowed):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "query"
                and isinstance(node.func.value, ast.Name)
            ):
                hits[node.lineno] = f"[L4] 直接 {node.func.value.id}.query()"
    return hits


def check_app_noqa() -> list[str]:
    """第一条：违规代码行不得用 `# noqa` 掩盖。"""
    bad: list[str] = []
    for path in check_layers.walk_py_files("app"):
        rel = check_layers._rel(path)
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, reason in sorted(_violating_lines(path, rel).items()):
            line = lines[lineno - 1] if lineno <= len(lines) else ""
            if not _has_noqa(line):
                continue
            bad.append(
                f"[noqa] {rel}:{lineno}: {reason}，且该行带 `# noqa` —— 分层检查不解析行内豁免，"
                f"请改代码或在 check_layers_allowlist.toml 登记"
            )
    return bad


def _iter_yaml(node: object, path: str = "root"):
    """递归遍历 YAML，产出 `(路径, 字典)` 供逐层检查。"""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _iter_yaml(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _iter_yaml(item, f"{path}[{index}]")


def check_ci_no_bypass() -> list[str]:
    """第二条：CI 配置不得出现跳过分层检查的写法。"""
    bad: list[str] = []
    if not WORKFLOWS_DIR.exists():
        return [f"[ci] 找不到 CI 配置目录：{WORKFLOWS_DIR}"]

    for workflow in sorted(WORKFLOWS_DIR.glob("*.y*ml")):
        data = yaml.safe_load(workflow.read_text(encoding="utf-8")) or {}
        allowed_expr = _ALLOWED_CONTINUE_ON_ERROR.get(workflow.name)
        for loc, node in _iter_yaml(data):
            if "continue-on-error" in node:
                value = node["continue-on-error"]
                # 只认登记过的表达式。`continue-on-error: true`（无论写在哪个文件）一律拦下：
                # 它让检查"跑了但不阻断"，比删掉检查更隐蔽，正是本脚本要拦的写法。
                if value != allowed_expr:
                    bad.append(
                        f"[ci] {workflow.name} {loc}: continue-on-error={value!r}，"
                        f"只允许登记过的告警表达式（见 _ALLOWED_CONTINUE_ON_ERROR）"
                    )
            condition = node.get("if")
            if condition is False or condition == "false":
                name = node.get("name", loc)
                bad.append(f"[ci] {workflow.name} {loc}: 步骤 {name!r} 的 if 恒为假，等于删掉该检查")
    return bad


def check_allowlist_hygiene() -> list[str]:
    """第三条：白名单条目必须写明失效任务编号（复用 check_layers 的口径）。"""
    allow = check_layers.load_allowlist()
    return check_layers.check_allowlist_hygiene(allow, check_layers.load_config())


def check() -> list[str]:
    return check_app_noqa() + check_ci_no_bypass() + check_allowlist_hygiene()


def main() -> int:
    violations = check()
    if not violations:
        print("分层检查防绕过守卫通过（无 noqa 掩盖、CI 无跳过条件、白名单卫生）")
        return 0
    print(f"分层检查防绕过守卫未通过，共 {len(violations)} 项：", file=sys.stderr)
    for line in violations:
        print(f"  {line}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

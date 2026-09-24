"""T1-12 验收：分层依赖检查脚本。

分三段：

1. **四类正例**——每种规则各构造一处真实违规，断言被检出且信息里带规则名、
   文件、行号与理由。门禁的可信度取决于"它确实报得出来"。
2. **四类反例**——注释、字符串、白名单目录、不在禁令内的模块。假阳性会让
   执行者被迫删注释或加豁免，门禁随即被架空；这四条钉住"不误报"。
3. **白名单卫生与真实仓库**——无失效任务编号、规则名写错、条目超限都要报错；
   最后在真实仓库上跑一次，确认脚本首次运行零违规。

所有用例都往 `tmp_path` 里造迷你仓库，再 `monkeypatch` 掉 `check_layers.ROOT`：
直接在真实仓库里塞违规文件会污染工作区，也会让用例互相干扰。
"""

import textwrap
from pathlib import Path

import pytest

from scripts import check_layers


def _write(root: Path, rel: str, source: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


@pytest.fixture()
def fake_root(tmp_path: Path, monkeypatch) -> Path:
    """把脚本的工作根换到临时目录。"""
    monkeypatch.setattr(check_layers, "ROOT", tmp_path)
    return tmp_path


def _violations() -> list[str]:
    return check_layers.check()


# --------------------------------------------------------------- 一、四类正例


def test_gate_importing_services_is_l1_violation(fake_root: Path) -> None:
    """L1：闸门引用服务层——闸门将无法独立部署，生成与校验的隔离被打破。"""
    _write(fake_root, "app/gate/rules/x.py", "from app.services import farmer_service\n")

    violations = _violations()

    assert len(violations) == 1
    assert "[L1]" in violations[0]
    assert "app/gate/rules/x.py" in violations[0]
    assert "app.services" in violations[0]
    assert "物理隔离" in violations[0], "错误信息必须带原文理由，便于定位"


def test_agent_importing_models_is_l2_violation(fake_root: Path) -> None:
    """L2：智能体直接导入 ORM——无法脱离数据库单测。"""
    _write(fake_root, "app/agent/nodes/x.py", "from app.models import User\n")

    violations = _violations()

    assert len(violations) == 1
    assert "[L2]" in violations[0]
    assert "app/agent/nodes/x.py" in violations[0]
    assert "app.models" in violations[0], "报出的必须是点分模块名，便于直接定位 import 语句"
    assert "假数据" in violations[0]


def test_llm_importing_gate_is_l3_violation(fake_root: Path) -> None:
    """L3：网关反向依赖业务模块——通用组件失去复用性。"""
    _write(fake_root, "app/llm/gateway.py", "from app.gate import engine\n")

    violations = _violations()

    assert len(violations) == 1
    assert "[L3]" in violations[0]
    assert "app.gate" in violations[0]
    assert "通用组件" in violations[0]


def test_db_query_outside_repositories_is_l4_violation(fake_root: Path) -> None:
    """L4：业务代码直接 `db.query()`——绕过归属校验的入口。"""
    _write(
        fake_root,
        "app/services/x.py",
        """
        async def f(db):
            return db.query(User).all()
        """,
    )

    violations = _violations()

    assert len(violations) == 1
    assert "[L4]" in violations[0]
    assert "app/services/x.py:3" in violations[0], "必须报出行号"
    assert "请走 app/repositories/" in violations[0]


def test_relative_import_inside_forbidden_package_is_not_flagged(fake_root: Path) -> None:
    """相对导入（`from .x import y`）不算跨层：包路径本身已限定范围。"""
    _write(fake_root, "app/gate/rules/x.py", "from .base import Rule\n")

    assert _violations() == []


def test_submodule_import_of_forbidden_package_is_flagged(fake_root: Path) -> None:
    """`from app.models.user import User` 也算 L2：只看顶层前缀，不要求完全相等。"""
    _write(fake_root, "app/agent/x.py", "from app.models.user import User\n")

    assert any("[L2]" in v for v in _violations())


# --------------------------------------------------------------- 二、四类反例


def test_comment_mentioning_db_query_is_not_flagged(fake_root: Path) -> None:
    """注释里的 `db.query(User)` 不是代码：AST 不误判（正则式实现会报）。"""
    _write(
        fake_root,
        "app/services/x.py",
        """
        # 改前这里是 db.query(User)，已迁到仓储
        x = 1
        """,
    )

    assert _violations() == []


def test_string_mentioning_db_query_is_not_flagged(fake_root: Path) -> None:
    """文档字符串里的示例同样不报，否则执行者被迫删掉解释性文字。"""
    _write(
        fake_root,
        "app/services/x.py",
        '''
        """禁止 db.query(User) 这种写法。"""
        MSG = "db.query(User)"
        ''',
    )

    assert _violations() == []


def test_db_query_inside_repositories_is_allowed(fake_root: Path) -> None:
    """白名单目录内允许：仓储层正是 L4 想引导到的唯一入口。"""
    _write(fake_root, "app/repositories/x.py", "def f(db):\n    return db.query(User).all()\n")

    assert _violations() == []


def test_other_module_importing_models_is_not_flagged(fake_root: Path) -> None:
    """`app.process_config` 导入 ORM 不在禁令内：只有 agent/gate/llm 受方向约束。"""
    _write(fake_root, "app/process_config/loader.py", "from app.models import KnowledgeItem\n")

    assert _violations() == []


def test_query_on_other_object_is_not_flagged(fake_root: Path) -> None:
    """`request.query_params` 不是 L4 的目标：放宽匹配会让门禁噪声化。"""
    _write(fake_root, "app/services/x.py", "def f(request):\n    return request.query_params.get('a')\n")

    assert _violations() == []


# --------------------------------------------------------------- 三、白名单卫生


def _write_allowlist(root: Path, body: str) -> None:
    _write(root, "scripts/check_layers_allowlist.toml", body)


def test_allowlist_entry_without_task_id_is_rejected(fake_root: Path) -> None:
    """理由里没有失效任务编号 → 例外会永久存在，必须报错。"""
    _write(fake_root, "app/agent/x.py", "from app.models import User\n")
    _write_allowlist(
        fake_root,
        """
        [[allow]]
        rule = "L2"
        path = "app/agent/x.py"
        reason = "临时放行，以后再改"
        """,
    )

    violations = _violations()

    assert any("[allowlist]" in v and "失效任务编号" in v for v in violations)
    assert not any("[L2]" in v for v in violations), "条目本身有效，只是理由不合规"


def test_allowlist_entry_with_task_id_suppresses_violation(fake_root: Path) -> None:
    """写了失效任务编号则豁免生效——证明上一条报的是理由而非条目本身。"""
    _write(fake_root, "app/agent/x.py", "from app.models import User\n")
    _write_allowlist(
        fake_root,
        """
        [[allow]]
        rule = "L2"
        path = "app/agent/x.py"
        reason = "T2-25 主图重排后删除本条"
        """,
    )

    assert _violations() == []


def test_allowlist_with_unknown_rule_name_is_rejected(fake_root: Path) -> None:
    """规则名写错会让豁免静默失效（永远匹配不上），必须显式报错。"""
    _write_allowlist(
        fake_root,
        """
        [[allow]]
        rule = "L9"
        path = "app/agent/x.py"
        reason = "T2-25 重排后删除"
        """,
    )

    assert any("不是已知规则" in v for v in _violations())


def test_allowlist_exceeding_max_entries_is_rejected(fake_root: Path) -> None:
    """条目数超上限说明规则被架空，报错而不是默默接受。"""
    entries = "\n".join(
        f'[[allow]]\nrule = "L2"\npath = "app/agent/f{i}.py"\nreason = "T2-25 待删除"\n'
        for i in range(check_layers._DEFAULT_MAX_ALLOWLIST + 1)
    )
    _write_allowlist(fake_root, entries)

    assert any("超过上限" in v for v in _violations())


def test_missing_allowlist_file_is_tolerated(fake_root: Path) -> None:
    """白名单文件不存在时按"无例外"处理，不是错误。"""
    _write(fake_root, "app/agent/x.py", "import os\n")

    assert _violations() == []


def test_unparsable_file_is_reported_not_raised(fake_root: Path) -> None:
    """语法错误的文件记为违规：脚本要能独立跑，不能因一个坏文件抛栈。"""
    _write(fake_root, "app/services/broken.py", "def f(:\n")

    violations = _violations()

    assert any("[syntax]" in v for v in violations)


# --------------------------------------------------------------- 四、扫描报告与真实仓库


def test_scan_report_lists_every_rule_with_file_count(fake_root: Path) -> None:
    """逐条打印扫描文件数：`L1 app.gate: 0 个文件` 与"扫了但合规"必须可区分。"""
    _write(fake_root, "app/agent/x.py", "import os\n")

    report = check_layers.scan_report()

    assert len(report) == len(check_layers.LAYER_RULES)
    assert "L1 app.gate: 0 个文件" in report
    assert "L2 app.agent: 1 个文件" in report


def test_real_repository_passes_with_zero_violations() -> None:
    """在真实仓库上零违规：这是 T1-06 整改到位的可执行证据。

    用 `check_layers.check()` 而非 `main()`：后者只返回退出码，断言拿不到明细，
    失败时无法定位是哪条规则。
    """
    violations = check_layers.check()

    assert violations == [], "\n".join(violations)

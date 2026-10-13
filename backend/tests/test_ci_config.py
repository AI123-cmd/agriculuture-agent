"""T6-11 验收：CI 配置不可被悄悄削弱。

流水线的价值全部来自"不可绕过"。手工维护的 YAML 里，删掉一步、或用
`continue-on-error: true` 把一步变成"跑了但不阻断"，都不会让任何测试变红——
直到发布出事。本模块把这条约束变成断言：

1. **八项检查步骤全部存在**（按步骤名精确匹配）。删掉任一项即失败，并指出缺的是哪一项。
2. **没有任何步骤带 `continue-on-error`**，唯一例外是评测回归作业在主分支之外的告警语义，
   且例外以**精确表达式**写在断言里。把检查整体挪进 `eval-regression.yml` 就会命中
   "该文件不允许带该表达式"的检查。
3. **本地脚本与 CI 第 1 至 7 项逐条一致**：文本比对两边的命令序列，避免"本地过 CI 挂"
   只因命令不同。这是人工肉眼核对容易漏的部分。
4. **评测门禁脚本存在且能正确裁定**：`compare_eval_reports.py` 在报告达标时返回 0、
   在下降超阈值时返回 1（用临时报告构造，不依赖真实评测）。

YAML 用 `safe_load` 解析而非正则匹配：正则匹配到的可能是被注释掉的步骤。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CI_YML = WORKFLOWS / "ci.yml"
EVAL_YML = WORKFLOWS / "eval-regression.yml"
CI_SH = BACKEND_ROOT / "scripts" / "ci.sh"
COMPARE = BACKEND_ROOT / "scripts" / "compare_eval_reports.py"
CHECK_NO_NOQA = BACKEND_ROOT / "scripts" / "check_no_noqa.py"

# 八项检查的步骤名（顺序即执行顺序）。名字写死在这里是有意的：改名字就要同步改本清单，
# 使"这一步还在不在"在评审里被看见，而不是靠 diff 里少几行。
EXPECTED_STEPS = [
    "1 ruff check",
    "2 ruff format --check",
    "3 mypy",
    "4 分层依赖检查（不允许 noqa 跳过）",
    "5 迁移可升降",
    "6 闸门规则单测",
    "7 图路径测试",
    "8 离线评测集回归",
]

# 主检查作业允许的非检查步骤（准备性步骤，不属于八项）。
_SETUP_STEPS = {"安装依赖", "建测试库", "9 其余测试（契约与集成）", "check-wxss-tokens"}

# 唯一登记的 continue-on-error 表达式（评测回归、非主分支告警）。
_ALLOWED_CONTINUE_ON_ERROR_EXPR = "${{ !(github.ref == 'refs/heads/main' || github.ref == 'refs/heads/master') }}"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _iter_nodes(node: object):
    """递归遍历 YAML，产出全部字典节点（用于找 continue-on-error）。"""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_nodes(item)


def _checks_job() -> dict:
    return _load(CI_YML)["jobs"]["checks"]


def test_ci_has_all_eight_steps_in_order() -> None:
    """八项检查按序存在：删掉任一项即失败并指出缺项。"""
    names = [step.get("name") for step in _checks_job()["steps"]]
    ordered = [name for name in names if name in EXPECTED_STEPS]

    assert ordered == EXPECTED_STEPS, f"检查项缺失或顺序不对：{ordered}"


def test_ci_job_has_no_unexpected_steps() -> None:
    """除八项与准备性步骤外不应有别的步骤：多出来的步骤要显式登记。"""
    names = {step.get("name") for step in _checks_job()["steps"]}
    # `uses` 步骤没有 name，取 `uses` 作为标识
    uses = [step.get("uses") for step in _checks_job()["steps"] if "uses" in step]
    unnamed = {u for u in uses}
    unexpected = names - set(EXPECTED_STEPS) - _SETUP_STEPS - unnamed
    unexpected.discard(None)
    assert not unexpected, f"出现未登记的 CI 步骤：{sorted(unexpected)}"


def test_main_checks_never_continue_on_error() -> None:
    """主检查作业的任何步骤都不得带 `continue-on-error`。

    它比删掉检查更隐蔽：步骤照跑、绿灯照亮，检查结论却不阻断发布。
    """
    offenders = [node for node in _iter_nodes(_checks_job()) if "continue-on-error" in node]

    assert offenders == [], f"主检查作业出现 continue-on-error：{offenders}"


def test_eval_workflow_continue_on_error_is_the_registered_expression() -> None:
    """评测作业只允许登记过的告警表达式，且必须同时存在于独立的评测配置文件里。"""
    assert EVAL_YML.exists(), "缺少独立评测回归作业 .github/workflows/eval-regression.yml"

    found: list[object] = []
    for node in _iter_nodes(_load(EVAL_YML)):
        if "continue-on-error" in node:
            found.append(node["continue-on-error"])

    assert found, "评测回归的门禁步骤应带登记过的告警表达式（非主分支降级）"
    assert all(value == _ALLOWED_CONTINUE_ON_ERROR_EXPR for value in found), (
        f"评测作业的 continue-on-error 表达式未登记：{found}"
    )


def test_ci_yml_does_not_define_its_own_eval_gate() -> None:
    """评测门禁只允许存在于 `eval-regression.yml`。

    若把它挪进 ci.yml，就能绕开上面那条"表达式必须登记"的断言——本用例封住这条路。
    """
    text = CI_YML.read_text(encoding="utf-8")

    assert "continue-on-error" not in text, "ci.yml 不应出现 continue-on-error"


def test_guard_flags_continue_on_error_true(tmp_path: Path, monkeypatch) -> None:
    """防绕过守卫确实拦得住 `continue-on-error: true`（用临时 workflow 目录构造）。

    这条用例防的是"守卫本身失效"：`check_no_noqa.py` 若把 `true` 当成合法值放过，
    流水线上任何一步都能被悄悄降级为"跑了但不阻断"，而所有测试仍全绿。
    """
    from scripts import check_no_noqa

    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "ci.yml").write_text(
        "jobs:\n  checks:\n    steps:\n      - name: 4 分层依赖检查\n"
        "        continue-on-error: true\n        run: python backend/scripts/check_layers.py\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_no_noqa, "WORKFLOWS_DIR", workflows)

    violations = check_no_noqa.check_ci_no_bypass()

    assert violations, "守卫未拦下 continue-on-error: true"
    assert "continue-on-error" in violations[0]


def test_compare_script_blocks_on_absolute_threshold(tmp_path: Path) -> None:
    """绝对门禁独立生效：整体一致率低于阈值即阻断，即使相对基线还有上升。

    T6-11 验收原文："把总一致率人为降到 `HARNESS_PASS_RATE_THRESHOLD` 以下（但相对基线未
    下降），`eval-gate` 仍失败，证明绝对门禁与相对门禁同时生效。"
    """
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "overall": {"total": 100, "passed": 80, "consistency": 0.80},
                "processes": {"rice": {"total": 100, "passed": 80, "consistency": 0.80}},
            }
        ),
        encoding="utf-8",
    )
    current = tmp_path / "current.json"
    # 相对基线整体上升（0.80 → 0.85），但仍在阈值 0.90 之下
    current.write_text(
        json.dumps(
            {
                "overall": {"total": 100, "passed": 85, "consistency": 0.85},
                "processes": {"rice": {"total": 100, "passed": 85, "consistency": 0.85}},
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(COMPARE),
            "--current",
            str(current),
            "--baseline",
            str(baseline),
            "--threshold",
            "0.90",
            "--max-drop-pp",
            "1.0",
        ],
        capture_output=True,
        text=True,
        cwd=BACKEND_ROOT,
    )

    assert result.returncode == 1, f"整体一致率低于阈值应阻断：{result.stdout}"
    assert "低于阈值" in result.stdout


def test_compare_script_accumulates_from_baseline_not_last_run(tmp_path: Path) -> None:
    """相对门禁以**基线**为准而非"上一次运行"：连续两次各降 0.9pp，第二次被阻断。

    若拿上一次运行做比较，两次各降 0.9 个百分点都会通过，累积成明显退化而无人察觉。
    这里用同一份基线跑两次，第二次的累计降幅（1.8pp）超过阈值即被拦下。
    """
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "overall": {"total": 1000, "passed": 1000, "consistency": 1.0},
                "processes": {"rice": {"total": 1000, "passed": 1000, "consistency": 1.0}},
            }
        ),
        encoding="utf-8",
    )

    def run(passed: int) -> tuple[int, str]:
        current = tmp_path / f"current_{passed}.json"
        consistency = passed / 1000
        current.write_text(
            json.dumps(
                {
                    "overall": {"total": 1000, "passed": passed, "consistency": consistency},
                    "processes": {"rice": {"total": 1000, "passed": passed, "consistency": consistency}},
                }
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                sys.executable,
                str(COMPARE),
                "--current",
                str(current),
                "--baseline",
                str(baseline),
                "--threshold",
                "0.90",
                "--max-drop-pp",
                "1.0",
            ],
            capture_output=True,
            text=True,
            cwd=BACKEND_ROOT,
        )
        return result.returncode, result.stdout

    first, first_out = run(991)  # 降 0.9pp：未超阈值
    second, second_out = run(982)  # 累计降 1.8pp：超阈值

    assert first == 0, f"单次下降 0.9pp 不应阻断：{first_out}"
    assert second == 1, f"累计下降 1.8pp 应阻断：{second_out}"
    assert "rice" in second_out, "阻断信息应指出受影响的流程名"


def test_guard_flags_noqa_on_layering_violation(tmp_path: Path, monkeypatch) -> None:
    """防绕过守卫拦得住"给分层违规行加 `# noqa`"。

    与 `test_check_layers.py` 的分工：那个模块证明 `check_layers.py` 不解析 `# noqa`
    （违规仍被报出），本用例证明有人这么做时会**额外**被指名，而不是留下一行"看起来已豁免"
    的代码。裸 `db.query` 的行不在 L4 白名单目录下，因此必然命中。
    """
    from scripts import check_layers, check_no_noqa

    monkeypatch.setattr(check_layers, "ROOT", tmp_path)
    path = tmp_path / "app" / "services" / "x.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "async def f(db):\n    return db.query(User).all()  # noqa: E501\n",
        encoding="utf-8",
    )

    violations = check_no_noqa.check_app_noqa()

    assert violations, "守卫未拦下带 noqa 的分层违规行"
    assert "app/services/x.py:2" in violations[0]
    assert "[L4]" in violations[0]


def test_guard_ignores_unrelated_noqa(tmp_path: Path, monkeypatch) -> None:
    """与分层无关的 `# noqa`（如 `except Exception:  # noqa: BLE001`）不报。

    这是守卫可信度的前提：`app/` 下现有四十处 BLE001 豁免一旦被误报，门禁会被人整体忽略。
    """
    from scripts import check_layers, check_no_noqa

    monkeypatch.setattr(check_layers, "ROOT", tmp_path)
    path = tmp_path / "app" / "services" / "y.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "async def f(db):\n    try:\n        return 1\n    except Exception:  # noqa: BLE001\n        return 0\n",
        encoding="utf-8",
    )

    assert check_no_noqa.check_app_noqa() == []


def test_check_no_noqa_script_exists() -> None:
    """第 4 项依赖的防绕过脚本必须存在。"""
    assert CHECK_NO_NOQA.exists(), "缺少 scripts/check_no_noqa.py"


def _canonical(command: str) -> str:
    """把一条命令归一化成可比形式。

    两边的工作目录不同（CI 在仓库根、脚本在 `backend/`），解释器前缀也不同
    （CI 直接 `ruff`、本地 `"$PY" -m ruff`）。归一只做这两件事的剥离，**不改命令本身**：
    改动命令仍会被比对出来，否则这条断言就失去意义。
    """
    text = command.strip()
    for prefix in ('"$PY"', "python3", "python", ".venv/Scripts/python.exe", ".venv/bin/python"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    text = re.sub(r"^-[mM]\s+", "", text)
    text = text.replace("backend/", "")
    return re.sub(r"\s+", " ", text).strip()


def test_local_script_matches_ci_first_seven_steps() -> None:
    """`scripts/ci.sh` 的命令序列与 CI 第 1 至 7 项逐条一致（文本比对，非肉眼）。

    这是"本地过 CI 挂"只可能由环境差异导致的前提。比对的是命令本身，不是步骤名——
    名字可以措辞不同，命令必须相同。
    """
    ci_commands: list[str] = []
    for step in _checks_job()["steps"]:
        if step.get("name") not in EXPECTED_STEPS[:7]:
            continue
        block = step["run"]
        for raw in re.split(r"\n|&&", block):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line = re.sub(r"^cd\s+\S+\s*", "", line)
            if line:
                ci_commands.append(_canonical(line))

    sh_commands = [
        _canonical(match.group(0))
        for match in re.finditer(r'^"\$PY".*$', CI_SH.read_text(encoding="utf-8"), flags=re.MULTILINE)
    ]

    assert ci_commands, "未能从 ci.yml 提取到命令，断言本身失效"
    missing = [cmd for cmd in ci_commands if cmd not in sh_commands]
    assert not missing, f"ci.sh 缺少 CI 中的命令：{missing}"


def test_compare_script_blocks_on_threshold_drop(tmp_path: Path) -> None:
    """`compare_eval_reports.py`：一致率达标放行（0），下降超阈值阻断（1）。

    用临时报告构造两种情形，不依赖真实评测结果——门禁脚本本身的行为必须可独立验证。
    """

    def report(overall: float, rice: float) -> dict:
        return {
            "knowledge_version": "test",
            "model_mode": "mock",
            "config_version": None,
            "overall": {"total": 10, "passed": int(overall * 10), "consistency": overall},
            "processes": {"rice": {"total": 10, "passed": int(rice * 10), "consistency": rice}},
        }

    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(report(1.0, 1.0)), encoding="utf-8")

    passing = tmp_path / "pass.json"
    passing.write_text(json.dumps(report(1.0, 1.0)), encoding="utf-8")

    blocked = tmp_path / "blocked.json"
    # rice 从 1.0 掉到 0.8（20pp > 1pp），绝对一致率仍在阈值之上 → 只有相对门禁能拦住它
    blocked.write_text(json.dumps(report(0.98, 0.8)), encoding="utf-8")

    def run(current: Path) -> int:
        result = subprocess.run(
            [
                sys.executable,
                str(COMPARE),
                "--current",
                str(current),
                "--baseline",
                str(baseline),
                "--threshold",
                "0.90",
                "--max-drop-pp",
                "1.0",
            ],
            capture_output=True,
            text=True,
            cwd=BACKEND_ROOT,
        )
        return result.returncode

    assert run(passing) == 0, "一致率达标应放行"
    assert run(blocked) == 1, "相对基线显著下降应阻断"


def test_compare_script_passes_without_baseline(tmp_path: Path) -> None:
    """基线缺失时放行（首次评测）：不能因"没有历史"把流水线拦住。"""
    current = tmp_path / "current.json"
    current.write_text(
        json.dumps(
            {
                "overall": {"total": 10, "passed": 10, "consistency": 1.0},
                "processes": {},
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(COMPARE),
            "--current",
            str(current),
            "--baseline",
            str(tmp_path / "missing-baseline.json"),
        ],
        capture_output=True,
        text=True,
        cwd=BACKEND_ROOT,
    )

    assert result.returncode == 0, f"首次评测应放行：{result.stdout}\n{result.stderr}"
    assert "首次评测" in result.stdout


@pytest.mark.parametrize("marker", ["eval", "graph", "gate", "contracts"])
def test_markers_registered(marker: str) -> None:
    """四个 marker 全部注册（未注册会产生 `PytestUnknownMarkWarning`）。"""
    pyproject = (BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert f'"{marker}:' in pyproject, f"pyproject.toml 未注册 marker：{marker}"


def test_release_checklist_maps_every_item_to_a_check() -> None:
    """发布清单每条人工核对项都要指向一项自动化检查（或显式标注"无"并给理由）。"""
    checklist = REPO_ROOT / "docs" / "release-checklist.md"
    assert checklist.exists(), "缺少 docs/release-checklist.md"

    text = checklist.read_text(encoding="utf-8")

    assert "对应的自动化检查" in text, "清单必须有'对应的自动化检查'列"
    # 至少覆盖文档点名的六类人工核对项
    for keyword in ["迁移", "评测", "noqa", "红线", "提示词", "临时物"]:
        assert keyword in text, f"清单缺少人工核对项：{keyword}"


def test_pull_request_template_exists() -> None:
    """PR 模板存在且含迁移/评测/红线三类勾选项。"""
    template = REPO_ROOT / ".github" / "pull_request_template.md"
    assert template.exists(), "缺少 .github/pull_request_template.md"

    text = template.read_text(encoding="utf-8")

    for keyword in ["迁移", "评测", "红线"]:
        assert keyword in text, f"PR 模板缺少核对项：{keyword}"

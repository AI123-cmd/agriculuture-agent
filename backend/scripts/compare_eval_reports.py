"""评测一致率门禁（T6-11，技术方案 14.1 的 CI 落地）。

只做三件事：读本次报告与基线报告、调用 T6-01 的 `compare_consistency`、按裁定设退出码
（放行 0、阻断 1）。判定逻辑**不在本脚本重复实现**——重复一份就会与评测集自身跑出来的
报告口径分叉，出现"测试说通过、门禁说下降"这种最难查的偏差。

用法（工作目录任意，路径由本文件位置推导）：

    python scripts/compare_eval_reports.py --current tests/eval/reports/2026-10-12.json
    python scripts/compare_eval_reports.py --current <本次> --update-baseline

门禁两条（与 T6-01、T6-02 一致）：

- **绝对门禁**：整体一致率低于阈值即阻断。阈值取 `--threshold`，缺省读环境变量
  `HARNESS_PASS_RATE_THRESHOLD`，再缺省用 `report.DEFAULT_PASS_RATE`（0.90）。
- **相对门禁**：与基线相比，任一流程一致率下降超过 `--max-drop-pp` 个百分点即阻断。
  阈值缺省读 `weights.toml` 的 `thresholds.consistency_drop_pp`（1.0）。

**基线缺失时放行并提示**：首次评测没有可比基线，不能因"没有历史"把流水线拦住
（`compare_consistency` 的既定语义），但提示必须显式。

`--update-baseline` 把本次报告固化成交付基线。它只被主分支的收尾作业调用（见
`.github/workflows/eval-regression.yml`），使"基线何时被更新"在 git 历史里有据可查。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tomllib
from pathlib import Path

# 允许从仓库任意目录运行：`tests.eval.report` 需要 backend/ 在 sys.path 上。
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from tests.eval.report import (  # noqa: E402  路径注入必须在导入之前
    DEFAULT_PASS_RATE,
    compare_consistency,
    load_report,
)

DEFAULT_BASELINE = _BACKEND_ROOT / "tests" / "eval" / "reports" / "baseline.json"
_WEIGHTS_PATH = _BACKEND_ROOT / "tests" / "eval" / "cases" / "weights.toml"


def _threshold(explicit: float | None) -> float:
    """阈值优先级：命令行 > 环境变量 > `report.DEFAULT_PASS_RATE`。"""
    if explicit is not None:
        return explicit
    raw = os.environ.get("HARNESS_PASS_RATE_THRESHOLD")
    if raw:
        return float(raw)
    return DEFAULT_PASS_RATE


def _drop_pp(explicit: float | None) -> float:
    """相对门禁阈值（百分点）：命令行 > `weights.toml` 的 `thresholds.consistency_drop_pp`。

    刻意不调用 `tests.eval.runner.load_drop_pp`：那个模块为了跑评测会连带导入
    `app.process_config`、`app.agent` 等整条链路，而本条门禁只读 JSON 报告，不该为了
    一个阈值就背上一整套应用依赖——独立运行时很容易撞上包的循环导入。
    """
    if explicit is not None:
        return explicit
    if not _WEIGHTS_PATH.exists():
        return 1.0
    data = tomllib.loads(_WEIGHTS_PATH.read_text(encoding="utf-8"))
    return float((data.get("thresholds") or {}).get("consistency_drop_pp", 1.0))


def _version_notice(current: dict, baseline: dict) -> str | None:
    """评测集自身版本变化时给出提示但不阻断（T6-02）。

    换了知识库或流程配置版本，一致率变化可能来自数据而非代码回归：直接阻断会让每次
    知识库更新都卡在"看起来是退化"，因此这里只提示，交由人工判断。
    """
    diffs = [
        f"{key}: {baseline.get(key)!r} → {current.get(key)!r}"
        for key in ("knowledge_version", "config_version")
        if baseline.get(key) != current.get(key)
    ]
    if not diffs:
        return None
    return "评测集版本已变化（不阻断，请人工判断）：" + "；".join(diffs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线评测一致率门禁")
    parser.add_argument("--current", required=True, type=Path, help="本次评测报告路径")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE, help="基线报告路径")
    parser.add_argument("--threshold", type=float, default=None, help="绝对一致率阈值（默认 0.90）")
    parser.add_argument(
        "--max-drop-pp",
        type=float,
        default=None,
        help="相对基线允许的最大下降（百分点，默认读 weights.toml 的 consistency_drop_pp）",
    )
    parser.add_argument("--update-baseline", action="store_true", help="把本次报告固化成交付基线")
    args = parser.parse_args(argv)

    if not args.current.exists():
        print(f"本次评测报告不存在：{args.current}", file=sys.stderr)
        return 1

    if args.update_baseline:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.current, args.baseline)
        print(f"基线已更新：{args.baseline} ← {args.current}")
        return 0

    current = load_report(args.current)
    if current is None:
        print(f"本次评测报告无法解析：{args.current}", file=sys.stderr)
        return 1

    baseline = load_report(args.baseline)
    if baseline is None:
        print(f"基线不存在（{args.baseline}），本次为首次评测，放行。")

    if baseline is not None:
        notice = _version_notice(current, baseline)
        if notice:
            print(notice)

    decision, reason = compare_consistency(
        current,
        baseline,
        pass_rate_threshold=_threshold(args.threshold),
        drop_pp_threshold=_drop_pp(args.max_drop_pp),
    )
    print(f"一致率门禁：{decision} —— {reason}")
    return 0 if decision == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

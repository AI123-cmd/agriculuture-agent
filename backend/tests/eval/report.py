"""评测报告与一致率门禁（T6-01，技术方案 14.1）。

一致率是评测的**唯一对外指标**：`consistency = 通过用例数 / 总用例数`，按流程分别计算。
这里只提供计算与比较，跑题与断言在 `test_offline_eval.py`；持久化格式是 JSON，落在
`reports/`（已在 `.gitignore` 里排除内容，只留 `.gitkeep`）。

门禁两条，语义不同：

- **绝对门禁**：一致率低于 `Settings.HARNESS_PASS_RATE_THRESHOLD`（默认 0.90）即阻断。
  它防的是"整体质量滑到地板以下"。
- **相对门禁**：与上一次历史报告相比下降超过 `weights.toml` 的 `consistency_drop_pp`
  （默认 1.0 个百分点）即阻断。它防的是"绝对线还过得去、但本次改动把某条流程搞坏了"，
  按流程分集比较才能定位到具体是哪一条变差。

**无历史报告时返回"放行"**：首次评测没有可比基线，不能因"没有历史"就把流水线拦住，
但必须显式提示这是首次评测。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

# 默认阈值与权重：T6-02 的 `weights.toml` 会覆盖；此处是"文件缺失也能跑"的兜底。
DEFAULT_PASS_RATE = 0.90
DEFAULT_DROP_PP = 1.0

REPORTS_DIR = Path(__file__).resolve().parent / "reports"

Decision = Literal["pass", "blocked"]


class ConsistencyReport(dict):
    """评测报告：`{process_id: {total, passed, consistency}}` 加上元信息。

    用普通 dict 子类而不是 Pydantic 模型：报告是产物、不是契约，字段会随 14.3 的指标
    演进；用 dict 让新旧报告能共存读取，读取方按 `.get()` 取即可。
    """


def consistency_of(passed: int, total: int) -> float:
    """一致率。`total=0` 时返回 1.0：没有用例不构成失败，也不能除零。"""
    if total <= 0:
        return 1.0
    return passed / total


def build_report(
    per_process: dict[str, tuple[int, int]],
    *,
    knowledge_version: str,
    model_mode: str,
    config_version: int | None = None,
) -> dict:
    """组装报告。`per_process` 的键是流程标识，值 `(passed, total)`。

    `knowledge_version` 与 `model_mode` 必须随报告记录：一次一致率下降若不注明"换没换
    知识库 / 跑的是 mock 还是真模型"，事后无法判断下降是代码回归还是知识库更新导致的。
    """
    processes: dict[str, dict] = {}
    overall_passed = overall_total = 0
    for process_id, (passed, total) in sorted(per_process.items()):
        processes[process_id] = {
            "total": total,
            "passed": passed,
            "consistency": consistency_of(passed, total),
        }
        overall_passed += passed
        overall_total += total
    return {
        "knowledge_version": knowledge_version,
        "model_mode": model_mode,
        "config_version": config_version,
        "overall": {
            "total": overall_total,
            "passed": overall_passed,
            "consistency": consistency_of(overall_passed, overall_total),
        },
        "processes": processes,
    }


def compare_consistency(
    current: dict,
    previous: dict | None,
    *,
    pass_rate_threshold: float = DEFAULT_PASS_RATE,
    drop_pp_threshold: float = DEFAULT_DROP_PP,
) -> tuple[Decision, str]:
    """比较本次与历史报告，返回 `(裁定, 说明)`。

    裁定顺序：先绝对门禁（低于阈值直接阻断），再相对门禁（按流程比较下降幅度）。
    无历史报告时返回 `("pass", 提示首次评测)`。
    """
    overall = current.get("overall") or {}
    rate = float(overall.get("consistency", 0.0))
    if rate < pass_rate_threshold:
        return "blocked", f"整体一致率 {rate:.4f} 低于阈值 {pass_rate_threshold:.2f}"

    if previous is None:
        return "pass", f"首次评测，无历史基线（整体一致率 {rate:.4f}）"

    drop_pp = drop_pp_threshold / 100.0
    prev_processes = previous.get("processes") or {}
    regressions: list[str] = []
    for process_id, row in (current.get("processes") or {}).items():
        prev_row = prev_processes.get(process_id)
        if prev_row is None:
            continue
        delta = float(row.get("consistency", 0.0)) - float(prev_row.get("consistency", 0.0))
        if delta < -drop_pp:
            regressions.append(f"{process_id} 下降 {-delta * 100:.2f}pp")
    if regressions:
        return "blocked", "按流程一致率下降超过阈值：" + "；".join(regressions)
    return "pass", f"一致率 {rate:.4f} 达标且无流程显著下降"


def load_report(path: Path) -> dict | None:
    """读一份历史报告；文件不存在返回 `None`（首次评测）。"""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_report(report: dict, path: Path) -> None:
    """写一份报告。目录不存在时创建（`reports/` 只有 `.gitkeep`）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


__all__ = [
    "DEFAULT_DROP_PP",
    "DEFAULT_PASS_RATE",
    "REPORTS_DIR",
    "build_report",
    "compare_consistency",
    "consistency_of",
    "load_report",
    "save_report",
]

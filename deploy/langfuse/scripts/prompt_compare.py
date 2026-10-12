#!/usr/bin/env python3
"""提示词版本对比脚本（T6-07，技术方案 15.5）。

读 Langfuse 的 generation 记录，按 `prompt_name` + `prompt_version` 聚合，输出每个版本的
质量与成本对比。用途是三件事：新提示词能否全量、是否需要回退（T6-09 的触发条件）、
哪个环节值得继续优化。

## 为什么是这六个指标

```python
GROUPS  = ("prompt_name", "prompt_version", "llm.model")
METRICS = ("latency_p50_ms", "latency_p95_ms", "tokens_p50",
           "contract_violation_rate", "gate_block_rate", "quality_label_bad_rate")
```

- 前三个是**成本与延迟**：改提示词常伴随 token 增长（例如加了更多示例），延迟随之上升。
  用中位数与 P95 而不是均值：均值会被偶发长尾带偏，而"改提示词后变慢"恰恰体现在长尾。
- 后三个是**质量**：
  - `contract_violation_rate`（解析失败占比）是提示词变更最灵敏的负面信号——提示词让模型
    更容易输出结构化格式之外的文本时，这个值立刻上升，而它**无需人工标注**即可计算。
  - `gate_block_rate` 与 `quality_label_bad_rate`（来自 T6-09 的人工抽检标注）才是真正的
    质量口径。前者是"处方被安全闸门拦下的比例"，后者是"人工抽检判为差的比例"。

## 版本对比与版本化是一件事的两面

没有版本号，对比无从谈起；没有对比，版本化只是存档。注意一个边界：业务侧的
`prompt_snapshot` 记录**生成当时**的提示词版本（15.5、T3-01），那是业务侧的可复现性证据；
本脚本读 Langfuse 是研发侧的优化依据。两者都写 `prompt_version` 且**必须同源**（都取
T2-60 的提示词注册表），否则会出现"业务说用的是 v7、观测数据显示 v5"这类无法对账的情况。

## 用法

```bash
# 1) 从 Langfuse 拉取（需界面创建的密钥）
python deploy/langfuse/scripts/prompt_compare.py \
  --host http://langfuse-web:3000 \
  --public-key pk-lf-... --secret-key sk-lf-... --days 7

# 2) 用本地导出的 JSON（离线复算，不发请求）
python deploy/langfuse/scripts/prompt_compare.py --input generations.json

# 3) 纯离线自检（内置样例，验证聚合与分组逻辑）
python deploy/langfuse/scripts/prompt_compare.py --self-check
```

依赖：Python 3.11+，标准库之外仅需 `httpx`（拉取模式才用到；`--input`/`--self-check`
不需要）。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GROUPS = ("prompt_name", "prompt_version", "llm.model")
METRICS = (
    "latency_p50_ms",
    "latency_p95_ms",
    "tokens_p50",
    "contract_violation_rate",
    "gate_block_rate",
    "quality_label_bad_rate",
)

# 属性键。与 `app/observability/attributes.py` 常量一一对应：脚本与代码共用同一份键名，
# 拼错不会报错只会静默丢数据，故这里显式列出而不用裸字符串散落各处。
KEY_PROMPT_NAME = "prompt.name"
KEY_PROMPT_VERSION = "prompt.version"
KEY_MODEL = "llm.model"
KEY_LATENCY_MS = "llm.latency_ms"
KEY_TOKENS = "llm.tokens"
# 解析失败标记：Langfuse 里契约违约表现为 generation 的 level=ERROR 且状态信息含
# `parse_error`；本脚本按 `status_message`/`level` 两个来源取，兼容不同导出形态。
KEY_CONTRACT_VIOLATION = "contract_violation"
KEY_GATE_DECISION = "gate.decision"
KEY_QUALITY_LABEL = "quality.label"


@dataclass
class Aggregate:
    """一个分组内的累计量。比率类指标在输出时由分子/分母相除得到。"""

    latencies: list[float] = field(default_factory=list)
    tokens: list[float] = field(default_factory=list)
    total: int = 0
    contract_violations: int = 0
    gate_blocked: int = 0
    quality_bad: int = 0
    quality_labeled: int = 0

    def add(self, latencies: float | None, tokens: float | None, row: dict[str, Any]) -> None:
        self.total += 1
        if isinstance(latencies, (int, float)):
            self.latencies.append(float(latencies))
        if isinstance(tokens, (int, float)):
            self.tokens.append(float(tokens))
        if _is_contract_violation(row):
            self.contract_violations += 1
        if _gate_decision(row) == "block":
            self.gate_blocked += 1
        label = _quality_label(row)
        if label is not None:
            self.quality_labeled += 1
            if label == "bad":
                self.quality_bad += 1

    def metrics(self) -> dict[str, float | int | None]:
        """算六个指标。分母为 0 时比率给 `None` 而不是 0——"没有样本"与"比例为零"不是一回事，
        混同会让"某版本从没被抽检"看起来像"该版本质量完美"。"""
        return {
            "samples": self.total,
            "latency_p50_ms": _percentile(self.latencies, 50),
            "latency_p95_ms": _percentile(self.latencies, 95),
            "tokens_p50": _percentile(self.tokens, 50),
            "contract_violation_rate": _ratio(self.contract_violations, self.total),
            "gate_block_rate": _ratio(self.gate_blocked, self.total),
            "quality_label_bad_rate": _ratio(self.quality_bad, self.quality_labeled),
        }


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _percentile(values: list[float], pct: float) -> float | None:
    """最近秩百分位。样本少时比插值法更贴近"实际最差的那次有多慢"。

    取整数下标而非线性插值：观测样本量小（一个提示词版本几天可能只有几十条），插值会
    造出一个从未真实发生过的数值，反而不便于和排障时看到的具体 trace 对应。
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), round(pct / 100 * len(ordered))))
    return round(ordered[rank - 1], 2)


def _attr(row: dict[str, Any], key: str, *fallbacks: str) -> Any:
    """取属性值。兼容三种导出形态：扁平 `{key: value}`、`{attributes: {...}}`、
    Langfuse API 的 `{observations: [...]}` 明细行。"""
    for container in (row, row.get("attributes") or {}):
        if not isinstance(container, dict):
            continue
        for name in (key, *fallbacks):
            if name in container and container[name] is not None:
                return container[name]
    return None


def _prompt_name(row: dict[str, Any]) -> str:
    value = _attr(row, KEY_PROMPT_NAME, "promptName", "name")
    return str(value) if value else "unknown"


def _prompt_version(row: dict[str, Any]) -> str:
    value = _attr(row, KEY_PROMPT_VERSION, "promptVersion", "version")
    return str(value) if value is not None else "unknown"


def _model(row: dict[str, Any]) -> str:
    value = _attr(row, KEY_MODEL, "model")
    return str(value) if value else "unknown"


def _latency(row: dict[str, Any]) -> float | None:
    value = _attr(row, KEY_LATENCY_MS, "latencyMs", "latency")
    return float(value) if isinstance(value, (int, float)) else None


def _tokens(row: dict[str, Any]) -> float | None:
    total = _attr(row, KEY_TOKENS, "totalTokens")
    if isinstance(total, (int, float)):
        return float(total)
    prompt = _attr(row, "usage.prompt_tokens", "promptTokens", "prompt_tokens")
    completion = _attr(row, "usage.completion_tokens", "completionTokens", "completion_tokens")
    if isinstance(prompt, (int, float)) and isinstance(completion, (int, float)):
        return float(prompt) + float(completion)
    return None


def _is_contract_violation(row: dict[str, Any]) -> bool:
    if bool(_attr(row, KEY_CONTRACT_VIOLATION, "contractViolation")):
        return True
    message = str(_attr(row, "status_message", "statusMessage") or "")
    return "parse_error" in message or "合同违约" in message or "契约违约" in message


def _gate_decision(row: dict[str, Any]) -> str:
    value = _attr(row, KEY_GATE_DECISION, "gateDecision")
    return str(value) if value else ""


def _quality_label(row: dict[str, Any]) -> str | None:
    """人工抽检标注（T6-09）。`quality.label` 为 `good`/`bad`；未标注返回 `None`。"""
    value = _attr(row, KEY_QUALITY_LABEL, "qualityLabel", "quality_label")
    if value in ("good", "bad"):
        return str(value)
    return None


def aggregate(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str], Aggregate]:
    """按 `GROUPS` 三元组聚合。键的三个分量分别是提示词名、版本、模型。"""
    buckets: dict[tuple[str, str, str], Aggregate] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = (_prompt_name(row), _prompt_version(row), _model(row))
        buckets.setdefault(key, Aggregate()).add(_latency(row), _tokens(row), row)
    return buckets


def format_report(buckets: dict[tuple[str, str, str], Aggregate]) -> str:
    """按版本分组输出六指标。同一 `prompt_name` 的多个版本相邻，便于直接比较。"""
    lines = ["按 prompt_name / prompt_version / llm.model 分组：", ""]
    header = ("prompt_name", "version", "model", *METRICS, "samples")
    lines.append(" | ".join(header))
    lines.append("-" * 96)
    for (name, version, model) in sorted(buckets):
        data = buckets[(name, version, model)].metrics()
        cells = [
            name,
            version,
            model,
            *[_fmt(data[metric]) for metric in METRICS],
            str(data["samples"]),
        ]
        lines.append(" | ".join(cells))
    if not buckets:
        lines.append("（无数据）")
    return "\n".join(lines)


def _fmt(value: float | int | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and 0 <= value <= 1:
        return f"{value:.2%}"
    return str(value)


def fetch_generations(host: str, public_key: str, secret_key: str, days: int) -> list[dict[str, Any]]:
    """从 Langfuse 公共 API 拉取 generation 明细。

    延迟导入 `httpx`：离线模式（`--input` / `--self-check`）不需要它，也不该因为没有网络
    依赖就跑不起来。
    """
    import base64
    import datetime as _dt

    import httpx

    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    since = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows: list[dict[str, Any]] = []
    page = 1
    with httpx.Client(base_url=host.rstrip("/"), timeout=30.0) as client:
        while True:
            resp = client.get(
                "/api/public/observations",
                params={"type": "GENERATION", "fromTimestamp": since, "page": page, "limit": 100},
                headers={"Authorization": f"Basic {token}"},
            )
            resp.raise_for_status()
            payload = resp.json()
            batch = payload.get("data") or []
            rows.extend(batch)
            if len(batch) < 100:
                break
            page += 1
    return rows


_SELF_CHECK_ROWS: list[dict[str, Any]] = [
    # v1：契约违约 1/4，无闸门拦截，未被抽检
    {"prompt.name": "interviewer", "prompt.version": 1, "llm.model": "m-a", "llm.latency_ms": 100, "llm.tokens": 50},
    {"prompt.name": "interviewer", "prompt.version": 1, "llm.model": "m-a", "llm.latency_ms": 200, "llm.tokens": 60},
    {"prompt.name": "interviewer", "prompt.version": 1, "llm.model": "m-a", "llm.latency_ms": 300, "llm.tokens": 70},
    {
        "prompt.name": "interviewer",
        "prompt.version": 1,
        "llm.model": "m-a",
        "llm.latency_ms": 400,
        "llm.tokens": 80,
        "status_message": "parse_error",
    },
    # v2：无违约，闸门拦截 1/2，抽检 2 条中 1 条差
    {
        "prompt.name": "interviewer",
        "prompt.version": 2,
        "llm.model": "m-a",
        "llm.latency_ms": 120,
        "llm.tokens": 55,
        "gate.decision": "block",
        "quality.label": "bad",
    },
    {
        "prompt.name": "interviewer",
        "prompt.version": 2,
        "llm.model": "m-a",
        "llm.latency_ms": 130,
        "llm.tokens": 58,
        "quality.label": "good",
    },
]


def _self_check() -> int:
    """离线自检：内置样例必须产出两个版本分组，且六指标齐全。"""
    buckets = aggregate(_SELF_CHECK_ROWS)
    report = format_report(buckets)
    print(report)
    versions = {key[1] for key in buckets}
    if versions != {"1", "2"}:
        print(f"[self-check] 期望两个版本分组，实际 {sorted(versions)}", file=sys.stderr)
        return 1
    v1 = buckets[("interviewer", "1", "m-a")].metrics()
    v2 = buckets[("interviewer", "2", "m-a")].metrics()
    if v1["contract_violation_rate"] != 0.25 or v1["quality_label_bad_rate"] is not None:
        print(f"[self-check] v1 指标不符：{v1}", file=sys.stderr)
        return 1
    if v2["contract_violation_rate"] != 0.0 or v2["gate_block_rate"] != 0.5:
        print(f"[self-check] v2 指标不符：{v2}", file=sys.stderr)
        return 1
    if v2["quality_label_bad_rate"] != 0.5:
        print(f"[self-check] v2 抽检不良率不符：{v2}", file=sys.stderr)
        return 1
    print("[self-check] 通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Langfuse 提示词版本对比（T6-07）")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="本地 generation JSON（数组）")
    source.add_argument("--self-check", action="store_true", help="用内置样例离线自检")
    source.add_argument("--host", help="从 Langfuse 拉取，需同时给 --public-key/--secret-key")
    parser.add_argument("--public-key", default="")
    parser.add_argument("--secret-key", default="")
    parser.add_argument("--days", type=int, default=7, help="拉取最近若干天（默认 7）")
    args = parser.parse_args(argv)

    if args.self_check:
        return _self_check()

    if args.input is not None:
        rows = json.loads(args.input.read_text(encoding="utf-8"))
    else:
        if not args.public_key or not args.secret_key:
            parser.error("--host 模式必须提供 --public-key 与 --secret-key")
        rows = fetch_generations(args.host, args.public_key, args.secret_key, args.days)

    print(format_report(aggregate(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

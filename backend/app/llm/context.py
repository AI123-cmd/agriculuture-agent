"""上下文裁剪（T2-55）。

方案 10.4 明确"只保留最近 N 轮对话（建议 6 轮），更早的压缩为已确认信息摘要"。这在
技术上既控 token，也避免长上下文导致的指令遵循退化——对"每轮只问两个问题"这类硬约束，
长上下文会显著降低遵循度。裁剪放在网关而不是各智能体，保证所有调用一致受控，也避免
某个智能体忘记裁剪。

摘要由纯函数生成（拼接早期消息的角色与截断内容），**不用模型**：裁剪本身不应触发又一次
模型调用，否则"省 token"的机制自己先花了 token。
"""

# 单条早期消息进摘要时的截断长度。摘要只需要"说过什么"的线索，不需要原话
# （原话属于敏感内容，进摘要反而扩大了它出现的范围）。
_SUMMARY_ITEM_CHARS = 60


def _summarize(head: list[dict]) -> str:
    """把被裁掉的早期消息拼成一段摘要文本。纯函数，无 I/O。"""
    parts = []
    for msg in head:
        role = str(msg.get("role") or "unknown")
        content = str(msg.get("content") or "")
        clipped = content[:_SUMMARY_ITEM_CHARS]
        if len(content) > _SUMMARY_ITEM_CHARS:
            clipped += "…"
        parts.append(f"{role}: {clipped}")
    return "以下是本次对话中较早的内容摘要（供参考，勿逐字复述）：" + "；".join(parts)


def trim_messages(messages: list[dict], max_rounds: int = 6) -> list[dict]:
    """只保留最近 N 轮对话，更早的压缩为已确认信息摘要。

    `system` 消息不参与裁剪，且摘要作为新的 `system` 消息插在 `system` 之后、`tail`
    之前：`system` 承载角色定位与硬规则，裁掉它会让模型的遵循度断崖式下降；而摘要
    紧跟其后、位于对话之前，符合"先背景后对话"的常规读法。

    纯函数：不修改入参，同一输入返回相等结果。
    """
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    if len(rest) <= max_rounds:
        return list(messages)
    head = rest[: len(rest) - max_rounds]
    tail = rest[len(rest) - max_rounds :]
    summary = {"role": "system", "content": _summarize(head)}
    return [*system, summary, *tail]


__all__ = ["trim_messages"]

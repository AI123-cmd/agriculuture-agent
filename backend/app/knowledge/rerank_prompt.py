"""轻量模型重排的提示词构造（T2-51）。

规则实现（`HeuristicReranker`）不依赖模型，是默认路径；本模块为"用轻量模型做相关性判定"
这条可选路径提供提示词。任务类型为 `TaskType.RETRIEVE`（证据核查档位）。

候选以编号清单下发、模型只回编号排序，而不是让模型复述正文：正文可能很长且含需要脱敏的
字段，让模型回编号既省 token 又避免把正文抄错。编号与候选一一对应，解析后按编号取回候选。
"""

from app.knowledge.fusion import KnowledgeHit

# 单条候选正文进提示词的长度上限。重排只需判断主题相关性，正文摘要足够，长正文会把
# 上下文预算挤占掉。
_EXCERPT_LEN = 200


def build_rerank_prompt(query: str, candidates: list[KnowledgeHit]) -> list[dict]:
    """构造相关性重排的提示词：下发编号候选，要求模型按相关度回编号序列。"""
    lines = [
        "你是农业知识检索的相关性排序器。",
        "按候选与查询的相关程度从高到低排序。",
        "只输出编号序列，用逗号分隔，不要解释。",
        "",
        f"查询：{query}",
        "",
        "候选：",
    ]
    for index, hit in enumerate(candidates, start=1):
        excerpt = hit.row.content[:_EXCERPT_LEN]
        lines.append(f"{index}. 标题：{hit.row.title}｜正文：{excerpt}")
    return [{"role": "user", "content": "\n".join(lines)}]


def parse_rerank_order(text: str, count: int) -> list[int]:
    """把模型回的编号序列解析成 0 基下标列表。

    越界编号丢弃（模型偶发幻觉），重复编号只保留首次，缺失的编号补在末尾——补全而不是报错，
    因为重排只是精排，一半候选没被模型点名不应让整轮检索失败。输出长度恒为 `count`。
    """
    seen: list[int] = []
    for token in text.replace("，", ",").split(","):
        stripped = token.strip()
        if not stripped.isdigit():
            continue
        idx = int(stripped)
        if 0 <= idx - 1 < count and (idx - 1) not in seen:
            seen.append(idx - 1)
    missing = [i for i in range(count) if i not in seen]
    return [*seen, *missing]


__all__ = ["build_rerank_prompt", "parse_rerank_order"]

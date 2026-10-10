"""提示文案长度约束（T4-05）：在生成侧收敛，前端不做截断。

技术方案 16.5 第四步明确：提示文案的可见字数上限必须在快照生成时控制，留到前端截断
会在适老化验收时反复返工。本模块只放"字数"这一件事的常量与纯函数，与 T3-07 的
`writer.truncate_at_sentence` 分工如下：

- `truncate_at_sentence` 作用于**整条**文案，对照流程配置的 `output_template.max_chars`，
  用于控制模型输出的总长；
- `fit_item_text` 作用于**逐段**字段（callout / fact / impact / action），是落库前的
  最终收敛，粒度是段。

两者叠加的执行顺序是：模型输出 → 裁到 `max_chars` → 拆段 → `fit_item_text` 逐段收敛 → 落库。
四级配置的优先级写死为：流程配置 `output_template.max_item_chars`（可覆盖）> 本模块
`MAX_ITEM_CHARS`（默认）> 逐段常量。

100 这个数字来自版面而非习惯：正文字号 36rpx、内容宽约 690rpx 时每行约 19 字，100 字约
6 行，乘 1.5 行距得 324rpx，加上级别标签与勾选热区后单条提示不超过 400rpx。逐段常量
20 + 32 + 24 + 24 = 100，保证"逐段不超即总长不超"。
"""

# 单条提示可见总长（含标点），32rpx 字号下约 6 行。
MAX_ITEM_CHARS: int = 100
# 呼告短，字段本身为 `String(30)`。
MAX_CALLOUT_CHARS: int = 20
# 事实段上限。
MAX_FACT_CHARS: int = 32
# 影响段上限。
MAX_IMPACT_CHARS: int = 24
# 行动段上限。
MAX_ACTION_CHARS: int = 24

# 句子边界标点：收敛时向前找最近的句末标点，避免返回"建议傍晚灌水降…"这类残句。
# 公开给 `snapshot.py` 复用：它据此判断一次收敛是否"退到句末"（安全）还是"硬切"（有损）。
SENTENCE_END = "。！？；"


def fit_item_text(text: str, limit: int) -> str:
    """超长则退到最近句末标点；无句末标点才硬切。绝不返回半句。

    未超长时原样返回（逐字节相等），证明本函数不改写合格文案。硬切只在整段没有任何
    句末标点时才发生——此时已无法"退到上一句"，只能截到上限。
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    idx = max((head.rfind(ch) for ch in SENTENCE_END), default=-1)
    return head[: idx + 1] if idx >= 0 else head


__all__ = [
    "MAX_ACTION_CHARS",
    "MAX_CALLOUT_CHARS",
    "MAX_FACT_CHARS",
    "MAX_IMPACT_CHARS",
    "MAX_ITEM_CHARS",
    "SENTENCE_END",
    "fit_item_text",
]

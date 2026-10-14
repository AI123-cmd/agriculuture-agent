"""追问候选池筛选器（T2-17）。

把流程必问清单压成最多六个候选方向，交给问诊智能体生成问题文本。它是"跳过已知项"与
"不漏必问项"两个要求的实现者，也是「凡是能被规则判定的，不交给模型」最典型的落地：
这里**没有模型参与**，三步筛选全是集合运算与排序，输入相同则输出必然相同——这是问诊
可重放的前提（同一份 checkpointer 状态重放时不能给出不同的候选池）。

三步严格串行且顺序不可调换：先做集合减法（去掉不需要问的），再做排序（决定剩下的谁
优先）。若先排序再剔除，排序时还得为注定被剔除的项计算相关性，属于无效计算。

`excluded` 记录每个被剔除的槽位及原因，是排查"为什么没问某个应该问的槽位"的主要依据。
没有它，筛选器一旦有 bug，表现只是"某类问题很少被问"，很难定位到具体是哪一步过滤掉的。
"""

from typing import Literal, TypeAlias, cast

import jieba
from pydantic import BaseModel, ConfigDict

from app.agent.contracts import ObservationResult
from app.agent.state import ConsultationState, ProcessId, SlotGroup
from app.process_config.models import ProcessConfig, SlotSpec

# 可由档案或环境直接读取的来源。这些来源的值一旦确实存在，对应槽位就不必再问农户。
AUTO_SOURCES = {"profile", "sensor", "weather"}

CandidateReason: TypeAlias = Literal["must_ask", "offline_enhance", "relevant_followup"]

# 相关性排序的权重。集中声明是为了让"为什么这个槽位排在前面"能一眼对上表。
_SCORE_REQUIRED = 100  # 必问清单是流程的底线
_SCORE_OFFLINE = 40  # 缺环境数据时这些问题的信息增量最大
_SCORE_CONFUSABLE = 20  # 每命中一个易混淆项词
_SCORE_SYMPTOM = 10  # 每命中一个农户已述症状词
_SCORE_PEST_GROUP = 5  # 农户提问以病虫情为主，轻微加权

# 检索改写用的停用词。分词只用于相关性粗排，不值得为它维护一份大词典。
_STOPWORDS = frozenset({"什么", "怎么", "为什么", "可以", "有没有", "没有", "还", "这个", "那个", "情况"})


def _is_content_token(token: str) -> bool:
    """单字与停用词不进相关性计算。

    单字（尤其"有""看""的"）几乎能命中任何文本，会让相关性排序退化成噪声；它对区分
    鉴别槽位与非鉴别槽位毫无帮助。
    """
    stripped = token.strip()
    return len(stripped) >= 2 and stripped not in _STOPWORDS and not stripped.isdigit()


def _tokenize(text: str) -> list[str]:
    """分词并过滤无信息量的词。jieba 的分词结果在进程内是确定的，满足可重放要求。"""
    return [token for token in jieba.lcut(text) if _is_content_token(token)]


def _dedupe(words: list[str]) -> list[str]:
    """保序去重。不排序：保序让高优先来源（用户回答）排在前面。"""
    seen: set[str] = set()
    out: list[str] = []
    for word in words:
        if word and word not in seen:
            seen.add(word)
            out.append(word)
    return out


class CandidateSlot(BaseModel):
    """候选池中的一项。含排序分与入选理由，便于线上排查。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_key: str
    display_label: str
    group: str
    required: bool
    options: list[str] | None = None
    # 作答形态提示：`photo` 表示该槽位靠农户拍照作答，模型据此把问题问成"能不能拍张
    # 照片说明情况"。由槽位配置的 `source_hint` 决定，不由模型自由发挥。
    answer_type: str = "text"
    relevant_score: int = 0
    reason: CandidateReason = "must_ask"


class CandidatePool(BaseModel):
    """筛选结果。`keys` 是给问诊智能体校验用的白名单。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    process_id: ProcessId
    candidates: tuple[CandidateSlot, ...]
    excluded: dict[str, str] = {}  # slot_key → 剔除原因，供排查
    top_k: int = 6

    @property
    def keys(self) -> set[str]:
        return {c.slot_key for c in self.candidates}


def select_candidate_slots(
    config: ProcessConfig,
    state: ConsultationState,
    *,
    observations: ObservationResult | None = None,
    top_k: int = 6,
) -> CandidatePool:
    """三步筛选：剔除已填、剔除可自动读、按相关性排序取 top-k。

    本函数是纯函数且只读：它对 `_ask_counters` 等处以下划线前缀的临时键也只看不写，
    写入方在 T2-16 的问诊智能体。因此它可以被反复调用而不产生副作用，测试也能直接
    断言输出。

    三步筛选后剩余项少于两个时照常返回，不补齐也不报错：接近收敛时本来就可能只剩
    一个未问项，此时"补齐到两个"只会问出已知答案。
    """
    excluded: dict[str, str] = {}
    step1 = _drop_filled(config, state, excluded)
    step2 = _drop_auto_readable(step1, state, observations, excluded)
    step3 = _rank_by_relevance(step2, state, config)
    return CandidatePool(
        process_id=config.process_id,
        candidates=tuple(step3[:top_k]),
        excluded=excluded,
        top_k=top_k,
    )


def _drop_filled(config: ProcessConfig, state: ConsultationState, excluded: dict[str, str]) -> list[SlotSpec]:
    """剔除 `status` 为 filled 或 unknown 的槽位。

    `unknown` 必须剔除是"不反复逼问"的直接实现：农户答不出就是合法答案，系统据此降低
    确定性表述，而不是换个措辞再问一遍（产品说明书 4.2.2）。非必问槽位也参与筛选，
    只是排序时权重低。
    """
    out: list[SlotSpec] = []
    slots = state.get("slots") or {}
    for spec in config.required_slots:
        slot = slots.get(spec.key)
        if slot is None or slot.status == "missing":
            out.append(spec)
        elif slot.status == "filled":
            excluded[spec.key] = "已确认"
        else:
            excluded[spec.key] = "农户已表示不清楚"
    return out


def _drop_auto_readable(
    specs: list[SlotSpec],
    state: ConsultationState,
    observations: ObservationResult | None,
    excluded: dict[str, str],
) -> list[SlotSpec]:
    """剔除可由档案/环境直接读取的项，以及追问超限的槽位。

    剔除条件是"该槽位的 `source_hint` 含自动来源"**且**"该来源确实已有值"，两个条件
    都要满足：只看 `source_hint` 就剔除（配置里配了 `profile` 但农户其实没填）会让这个
    槽位永远不会被问到，形成信息盲区。

    追问超限的剔除本质属于"不该再问"，与状态过滤同类，故放在这一步而不是单开第四步。
    """
    auto_filled = _collect_auto_facts(state, observations)
    # 这两个是 T2-16 写入的临时键（下划线前缀，不入状态契约），故显式标注类型：
    # `ConsultationState` 是 `total=False` 的 TypedDict，未声明的键在 mypy 眼里是 `object`。
    ask_counters: dict[str, int] = cast(dict, state.get("_ask_counters") or {})
    max_rounds: int = cast(int, state.get("_max_slot_rounds", 6))

    out: list[SlotSpec] = []
    for spec in specs:
        if any(src in AUTO_SOURCES for src in spec.source_hint) and spec.key in auto_filled:
            excluded[spec.key] = "档案或环境已有"
            continue
        if ask_counters.get(spec.key, 0) > max_rounds:
            excluded[spec.key] = "追问次数超限，转 unknown"
            continue
        out.append(spec)
    return out


def _collect_auto_facts(state: ConsultationState, observations: ObservationResult | None) -> set[str]:
    """汇总当前确实可自动获取的标识。

    三个来源：档案里 `slot_values` 非空的值、环境读数中 `quality == "ok"` 的 `metric`、
    以及 `observed_facts` 中非空的键。`observations` 参数优先，缺省时回落到 state 里的
    现场采集产出（`ObservationResult` 序列化后的 dict）。
    """
    facts: set[str] = set()

    profile = state.get("profile") or {}
    slot_values = profile.get("slot_values") or {}
    if isinstance(slot_values, dict):
        facts.update(key for key, value in slot_values.items() if value)

    source: dict = (
        observations.model_dump() if observations is not None else cast(dict, state.get("observations") or {})
    )
    for reading in source.get("readings") or []:
        metric = reading.get("metric")
        if metric and reading.get("quality") == "ok":
            facts.add(metric)
    for key, value in (source.get("observed_facts") or {}).items():
        if value:
            facts.add(key)
    return facts


def _rank_by_relevance(specs: list[SlotSpec], state: ConsultationState, config: ProcessConfig) -> list[CandidateSlot]:
    """相关性排序：必问清单优先，易混淆项相关优先，其余按 `ask_order`。

    排序是**多级权重加分**而不是单一关键词匹配：权重含义见模块顶部的 `_SCORE_*` 常量。
    排序键为 `(score, -ask_order)`，分数相同则 `ask_order` 小者优先（取负值是因为 `sort`
    升序而 `ask_order` 越小越应先问）。`sort` 是稳定排序，同分同 `ask_order` 的项其相对
    顺序由 `config.required_slots` 的列表顺序决定，因此同一配置与状态得到完全确定的
    候选池。
    """
    confusable_terms = _confusable_terms(config)
    symptom_terms = _symptom_terms(state)
    offline_keys = _offline_keys(config, state)

    scored: list[tuple[int, int, CandidateSlot]] = []
    for spec in specs:
        score = 0
        reason: CandidateReason = "relevant_followup"
        if spec.required:
            score, reason = score + _SCORE_REQUIRED, "must_ask"
        if spec.key in offline_keys:
            score, reason = score + _SCORE_OFFLINE, "offline_enhance"
        score += _SCORE_CONFUSABLE * _term_hits(spec, confusable_terms)
        score += _SCORE_SYMPTOM * _term_hits(spec, symptom_terms)
        if spec.group is SlotGroup.PEST:
            score += _SCORE_PEST_GROUP
        scored.append(
            (
                score,
                -spec.ask_order,
                CandidateSlot(
                    slot_key=spec.key,
                    display_label=spec.display_label,
                    group=spec.group.value,
                    required=spec.required,
                    options=spec.options,
                    answer_type=_answer_type_of(spec),
                    relevant_score=score,
                    reason=reason,
                ),
            )
        )

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in scored]


def _answer_type_of(spec: SlotSpec) -> str:
    """候选槽位的作答形态。由 `source_hint` 与是否有选项决定，不由模型自由发挥。

    `source_hint` 含 `image` 的槽位靠拍照作答（"能不能拍张照片说明情况"），提示模型把
    问题问成拍照请求；有选项的槽位走单选，其余走自由文本。
    """
    if "image" in spec.source_hint:
        return "photo"
    return "single" if spec.options else "text"


def _confusable_terms(config: ProcessConfig) -> set[str]:
    """易混淆项的词集：候选名与决定性特征的分词结果。

    两者都要取。候选名（"青枯病"）让鉴别类槽位加权，决定性特征（"挤压切口渗出乳白色
    菌脓"）让"怎么鉴别"的操作性槽位也加权——后者往往才是模型真正需要问的那一问。
    """
    words: list[str] = []
    for pair in config.confusable_items:
        words.extend(pair.candidates)
        for features in pair.decisive_features.values():
            words.extend(features)
    return set(_tokenize(" ".join(words)))


def _symptom_terms(state: ConsultationState) -> set[str]:
    """农户已述症状的词集：从已填槽位的值里抽取。"""
    words: list[str] = []
    for slot in (state.get("slots") or {}).values():
        if slot.status == "filled" and slot.value:
            words.append(slot.value)
    return set(_tokenize(" ".join(words)))


def _term_hits(spec: SlotSpec, terms: set[str]) -> int:
    """槽位文本命中的词数。纯字符串包含判断。

    不做语义相似度计算：那需要模型或向量，与本模块"模型不参与"的定位冲突，也会让
    "同一状态得到同一候选池"这条可重放性质失效。
    """
    if not terms:
        return 0
    text = " ".join([spec.label, spec.display_label, *(spec.options or [])])
    return sum(1 for term in terms if term in text)


def _offline_keys(config: ProcessConfig, state: ConsultationState) -> set[str]:
    """无环境数据时的增强追问目标槽位。`env_source` 不是 `none` 时不生效。

    传感器数据陈旧（`sensor_stale`）与完全没有环境数据（`none`）分开处理：前者设备是
    装着的，该去排查设备，而不是改问"地里干湿如何"这类替代问题（T2-15 的
    `offline_questions_needed` 用同一判据）。
    """
    observations = state.get("observations") or {}
    if observations.get("env_source") != "none":
        return set()
    return {oq.slot_key for oq in config.offline_questions}


__all__ = [
    "AUTO_SOURCES",
    "CandidatePool",
    "CandidateSlot",
    "select_candidate_slots",
]

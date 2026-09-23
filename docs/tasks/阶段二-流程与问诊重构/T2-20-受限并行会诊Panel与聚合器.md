# T2-20 受限并行会诊 Panel 与聚合器

## 这一项要做什么

在 `app/agent/agents/panel.py` 中实现受限并行会诊：对易混淆项鉴别任务以 fork-join 结构并行发起三路视角，再由纯规则聚合器裁定。启用范围限茶、辣椒、杂草三条流程，这是技术方案 6.4 明确的范围限定。

聚合器是"不投票"的地方。三个视角各说一个病名然后取多数票，在只有三个视角时会退化为主观权重。真正的鉴别依据是流程配置里写明的决定性特征，这类特征是排除性的、可验证的，必须优先于统计式的意见聚合。

## 前置依赖

- 依赖 T2-19：必须已提供 `DiagnoserAgent`，聚合结果交给它产出最终判定
- 依赖 T2-13：必须已提供 `BaseAgent`、`AgentContext`（面板本身作为批量智能体使用基类的重试降级）
- 依赖 T2-11：必须已提供 `DiagnosisResult`，面板只产出意见，不产出最终结论
- 依赖 T2-02：必须已提供 `ProcessConfig` 的 `confusable_items` 与 `panel_enabled`
- 依赖 T2-01：必须已提供 `PanelResolution`
- 外部环境：三条流程的 `decisive_features` 需农技确认（见全局约定第八节）

## 参考章节

- 技术方案 第6章 6.4 受限并行会诊（fork-join 结构、启用范围、聚合口径）
- 技术方案 第6章 6.4 聚合器的裁定规则（`aggregate_panel` 的完整代码）
- 技术方案 第6章 6.4 为什么"不投票"（决定性特征优先于统计式聚合）
- 技术方案 第6章 6.4 `force_uncertain` 的意义（不强行择一，给出观察加求助）
- 技术方案 第6章 6.4 成本控制（三条流程才启用，仅鉴别节点触发一次）
- 技术方案 第5章 5.1 智能体总表（⑨并行会诊可选，失败转单链判定）
- 技术方案 第9章 9.1 `ConfusablePair`（一份配置驱动三处行为）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/agent/agents/panel.py` | `PanelOpinion`、`PanelAggregate`、`aggregate_panel` 与三视角模型 |
| 新增 | `app/agent/agents/prompts/panel.py` | 三个视角的提示词 |
| 新增 | `tests/agent/test_panel.py` | 硬判据、排除项、未收敛三段裁定的用例 |
| 修改 | `app/agent/agents/__init__.py` | 导出面板接口 |

## 实现要点

### 数据模型

```python
class PanelOpinion(BaseModel):
    """单路视角的意见。"""
    model_config = ConfigDict(extra="forbid", frozen=True)

    perspective: Literal["symptom", "environment", "exclusion"]
    candidate: str | None                     # 该视角倾向的候选，排除视角可为 None
    support: list[str]                        # 支持理由
    excluded_candidates: list[str] = []       # 该视角明确排除的候选
    decisive_feature_hit: str | None = None   # 命中的决定性特征原文
    confidence: Literal["likely", "preliminary", "uncertain"] = "preliminary"


class PanelAggregate(BaseModel):
    """聚合结果。"""
    model_config = ConfigDict(extra="forbid", frozen=True)

    winner: PanelOpinion | None = None
    candidates: list[str] = []
    resolution: Literal["decisive_feature", "elimination", "unresolved"]
    force_uncertain: bool = False
    excluded: list[str] = []
    rationale: str = ""
```

`decisive_feature_hit` 存命中特征的**原文**而不是布尔值。这样聚合器判定的"是否命中决定性特征"可以被审计：排查时能看到模型到底引用了哪句话，从而判断是判据配置有问题还是模型理解有偏差。

### 三路视角的语义

| 视角 | 关注点 | 输出倾向 |
| --- | --- | --- |
| symptom 症状学 | 病斑形态、受害部位、发展速度 | 倾向某个候选 |
| environment 环境与农事 | 天气、用药史、栽培管理 | 倾向某个候选 |
| exclusion 排除项 | 生理性、药害、缺素等非侵染因素 | 主要输出排除项 |

排除视角是唯一被允许 `candidate=None` 的视角。技术方案 6.4 的流程图里把视角 C 标注为"排除项（生理性/药害/缺素）"，它的职责就是排除，不强求给候选。

### aggregate_panel 完整实现

```python
def aggregate_panel(opinions: list[PanelOpinion], config: ProcessConfig) -> PanelAggregate:
    """并行意见聚合：不投票，按流程配置的判据裁定。

    三段逻辑依次尝试，命中即返回：
    1. 硬判据优先——唯一视角命中流程配置中的决定性特征时，取该视角
    2. 排除项优先——排除视角明确排除的候选直接出局，不论其他视角支持度
    3. 仍不唯一——标记分歧，强制 confidence_level = uncertain
    """
    if not opinions:
        return PanelAggregate(
            resolution="unresolved",
            force_uncertain=True,
            rationale="无有效视角意见",
        )

    # 1. 硬判据优先：若任一视角给出流程配置中的"决定性特征"，
    #    且该特征与另一视角冲突，则取有决定性特征的一方
    decisive = [o for o in opinions if o.decisive_feature_hit]
    decisive_candidates = {o.candidate for o in decisive if o.candidate}
    if len(decisive) == 1:
        return PanelAggregate(
            winner=decisive[0],
            candidates=[decisive[0].candidate] if decisive[0].candidate else [],
            resolution="decisive_feature",
            excluded=_collect_excluded(opinions),
            rationale=f"命中决定性特征：{decisive[0].decisive_feature_hit}",
        )
    if len(decisive_candidates) == 1:
        winner = next(o for o in decisive if o.candidate in decisive_candidates)
        return PanelAggregate(
            winner=winner,
            candidates=[winner.candidate] if winner.candidate else [],
            resolution="decisive_feature",
            excluded=_collect_excluded(opinions),
            rationale=f"多视角一致命中决定性特征：{winner.decisive_feature_hit}",
        )

    # 2. 排除项优先：排除项视角（视角C）若能明确排除某候选，
    #    该候选直接出局，不论其他视角支持度
    excluded = reduce_exclusions(opinions, config)
    remaining = [c for c in all_candidates(opinions) if c not in excluded]

    # 3. 仍不唯一 → 标记为分歧，交判定智能体在结论中显式说明不确定性
    if len(remaining) != 1:
        return PanelAggregate(
            candidates=remaining,
            resolution="unresolved",
            force_uncertain=True,
            excluded=excluded,
            rationale=(
                "并行会诊未收敛"
                if remaining
                else "全部候选被排除，需重新收集证据"
            ),
        )
    return PanelAggregate(
        winner=_opinion_for(opinions, remaining[0]),
        candidates=remaining,
        resolution="elimination",
        excluded=excluded,
        rationale=f"经排除后唯一候选：{remaining[0]}",
    )
```

### 三个辅助函数

```python
def reduce_exclusions(opinions: list[PanelOpinion], config: ProcessConfig) -> list[str]:
    """汇总被排除的候选。仅接受有配置判据支撑的排除。"""
    configured = {c for pair in config.confusable_items for c in pair.candidates}
    excluded: list[str] = []
    for opinion in opinions:
        for cand in opinion.excluded_candidates:
            if cand in configured and cand not in excluded:
                excluded.append(cand)
    return excluded


def all_candidates(opinions: list[PanelOpinion]) -> list[str]:
    """收集所有视角提到的候选，保序去重。"""
    seen: list[str] = []
    for opinion in opinions:
        if opinion.candidate and opinion.candidate not in seen:
            seen.append(opinion.candidate)
    return seen


def _opinion_for(opinions: list[PanelOpinion], candidate: str) -> PanelOpinion:
    return next(o for o in opinions if o.candidate == candidate)


def _collect_excluded(opinions: list[PanelOpinion]) -> list[str]:
    out: list[str] = []
    for opinion in opinions:
        for cand in opinion.excluded_candidates:
            if cand not in out:
                out.append(cand)
    return out
```

`reduce_exclusions` 只接受"配置里有判据支撑的排除"。若某视角排除了一个不在任何 `confusable_items` 候选集里的名称，这个排除被忽略。原因是排除的效力来自配置中的决定性特征，没有配置支撑的排除只是模型的随口判断，不能作为"直接出局"的依据。这防止模型排除掉正确候选从而把结论引向错误方向。

`all_candidates` 保序去重而不是用集合，因为最终返回的 `candidates` 顺序会进入展示与审计，需要可复现。

### 为什么"不投票"

在农业鉴别诊断中，三个视角各说一个病名然后取"多数票"，在只有三个视角时会退化为主观权重。真正的鉴别依据是流程配置中写明的**决定性特征**（如辣椒的青枯病判据是"挤压切口渗出乳白色菌液"），这类特征是排除性的、可验证的，必须优先于统计式的意见聚合。

投票的失效场景很具体：辣椒流程的青枯病与枯萎病，三个视角可以是"症状学支持青枯病、环境视角支持枯萎病、排除视角未给候选"。三票中只有两票有候选，此时"多数"没有意义；而如果症状视角命中了"挤压切口渗出乳白色菌脓"这条决定性特征，结论就是确定的，不需要再看另外两票。

反过来，若没有任何视角命中决定性特征，当前证据就不足以支撑一个确定的鉴别结论。此时投票得出的"多数"只是模型的主观倾向分布，用它选出的结论会以"确定"的语气输出，而实际上把握很低。这正是 `force_uncertain` 要避免的情况。

### force_uncertain 的意义

```python
# force_uncertain 的语义：
# 当并行会诊无法收敛时，系统不做强行择一，而是把结论定性为"不确定"
# 并给出观察 + 求助建议。这直接实现产品说明书 4.2.3 功能点 4 的要求——
# 当把握不足时，正确做法是给出观察 + 复查的动作，
# 而不是给一个模糊的答案让农户自己猜。
```

`force_uncertain=True` 由 T2-19 的判定智能体读取 `state["panel_force_uncertain"]` 后强制覆写 `confidence_level`。面板本身不产出 `DiagnosisResult`（那是判定智能体的职责），只产出意见与裁定结果。

### 面板运行器

```python
class PanelRunner:
    """三路并行会诊。仅对启用 panel_enabled 的流程且仅鉴别节点调用一次。"""

    def __init__(self, gateway: ModelGateway, timeout: float = 20.0) -> None:
        self._gateway = gateway
        self._timeout = timeout

    async def collect(
        self,
        config: ProcessConfig,
        state: ConsultationState,
        ctx: AgentContext,
    ) -> list[PanelOpinion]:
        if not config.panel_enabled:
            return []
        tasks = [
            asyncio.create_task(self._one(p, config, state, ctx))
            for p in ("symptom", "environment", "exclusion")
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        opinions = [r for r in results if isinstance(r, PanelOpinion)]
        if len(opinions) < 2:
            log.warning("panel quorum not reached, fallback to single chain",
                        extra={"trace_id": ctx.trace_id, "got": len(opinions)})
        return opinions
```

技术方案 6.4 的失败行为是"转单链判定"。因此不足两路意见时不报错，返回已得到的意见，由调用方判定：`len(opinions) < 2` 时直接走单链（跳过面板裁定）。这个判断由 T2-26 的节点工厂做，面板只上报情况。

### 成本控制

技术方案 6.4 的三条限制要逐条落实：
- **只有三条流程启用**：`config.panel_enabled` 为 False 时 `collect` 直接返回空
- **仅鉴别节点触发一次**：调用点写在节点工厂里，不在判定节点内部循环调用。`state` 中要打一个 `panel_ran` 标记，防止同一会话内重复触发
- **聚合器为纯规则无模型消耗**：`aggregate_panel` 不接收网关参数，这一点从签名可以看出

```python
    def already_ran(self, state: ConsultationState) -> bool:
        return bool(state.get("panel_ran"))
```

`already_ran` 的检查放在调用方而不是 `collect` 内部，因为"是否可以重跑"属于编排决策，面板只提供状态查询。

### 提示词按视角差异化

```python
PERSPECTIVE_FOCUS: dict[str, str] = {
    "symptom": "只从症状学角度分析：病斑形态、受害部位、发展速度。不要考虑天气与用药。",
    "environment": "只从环境与农事角度分析：近期天气、用药史、栽培管理。不要考虑病斑形态。",
    "exclusion": "只从排除角度分析：能否排除生理性、药害、缺素等非侵染因素。给不出候选是正常的。",
}
```

三个视角的提示词必须**互不覆盖**，否则三路会给出几乎相同的意见，并行就失去意义。技术方案 6.4 的流程图里明确标注了每路的关注点，这里的提示词与之一一对应。

每个视角都必须输出结构化 JSON（`PanelOpinion`），并且要求"若认为某候选可排除，在 `excluded_candidates` 中列出"——这是第二段裁定逻辑的数据来源。

## 验收标准

- `pytest tests/agent/test_panel.py` 全部通过，含三段裁定各自的用例：唯一视角命中决定性特征时 `resolution="decisive_feature"`、某候选被排除视角排除后仅剩一个候选时 `resolution="elimination"`、无判据且多候选时 `resolution="unresolved"` 且 `force_uncertain=True`
- 断言 `aggregate_panel` 在 `opinions=[]` 时返回 `force_uncertain=True` 且不抛异常
- 断言 `reduce_exclusions` 忽略不在任何 `confusable_items` 候选集中的排除项（构造一个"感冒"排除项验证其被忽略）
- 断言 `all_candidates` 保序去重：两个视角都给"青枯病"时结果只含一次
- 断言 `aggregate_panel` 的函数签名不含 `gateway`、`session`、`db` 任一参数（证明纯规则无模型消耗）
- 断言 `panel_enabled=False` 时 `PanelRunner.collect` 返回空列表且不发起任何模型调用
- 断言三路中两路失败时 `collect` 返回一路意见且记录 warning，不抛异常
- 断言同一输入连续调用 `aggregate_panel` 100 次，`PanelAggregate` 各字段完全一致
- 断言辣椒流程的配置下，`candidates` 为 `["青枯病", "枯萎病"]` 时，只有 `decisive_features` 中的名称能进入 `excluded`
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0

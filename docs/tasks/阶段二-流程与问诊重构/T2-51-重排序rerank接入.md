# T2-51 重排序 rerank 接入

## 这一项要做什么

在 `app/knowledge/rerank.py` 中实现混合检索第四步的重排序：对 RRF 融合后的候选按时序与本条查询做相关性精排，输出截断到 `top_k` 的最终证据序列。技术在方案 9.3 把这一步定义为"cross-encoder 或轻量模型"，本项要给出两条可切换的实现路径与一致的降级行为。

重排序质量决定证据质量，证据质量决定判定与处方的正确性。方案 4.4 也因此把证据核查的模型档位定为"中等模型加重排序模型"，而不是用最便宜的小模型。

## 前置依赖

- 依赖 T2-49：必须已提供的 `search` 第四步调用点与融合后的候选结构
- 依赖 T2-47：必须已提供知识条目字段，重排序输入含条目的正文与关键元数据
- 依赖 T2-55、T2-57：必须已提供模型网关与任务类型路由（重排序走 `TaskType.RETRIEVE` 的档位）
- 外部环境：重排序模型服务（cross-encoder）的端点配置；未配置或不可用时回落到轻量实现
- 与 T2-40 的关系：重排后的证据顺序影响 `R-REGION` 的地区判定取样，重排需保证高相关条目在前

## 参考章节

- 技术方案 第9章 9.3 混合检索实现的第 4 步（重排序，cross-encoder 或轻量模型）
- 技术方案 第4章 4.4 模型分层策略（证据核查用中等模型加重排序模型）
- 技术方案 第17章 17.1 技术风险（知识库检索召回不准导致处方偏离，处置含 cross-encoder 重排）
- 技术方案 第10章 10.4 成本控制（上下文裁剪与单次预算）
- 附录 C.2 技术术语（cross-encoder 的约定含义）
- 全局约定 第六节（凡是能被规则判定的，不交给模型）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/knowledge/rerank.py` | `Reranker` 协议、`CrossEncoderReranker`、`HeuristicReranker` |
| 新增 | `app/knowledge/rerank_prompt.py` | 轻量模型重排的提示词构造 |
| 新增 | `tests/knowledge/test_rerank.py` | 两路实现、降级、截断、幂等的用例 |
| 修改 | `app/knowledge/repository.py` | 把 `_rerank` 接到本项实现，默认走 `HeuristicReranker` |
| 修改 | `app/core/config.py` | 新增 `RERANK_API_URL`、`RERANK_API_KEY`、`RERANK_ENABLED` |

## 实现要点

### 统一接口与两条实现

```python
class Reranker(Protocol):
    """重排序接口。候选为 RRF 融合后的有序列表。"""
    async def rerank(self, query: str, candidates: list[KnowledgeHit], top_k: int) -> list[KnowledgeHit]: ...

class CrossEncoderReranker:
    """cross-encoder 精排：把查询与候选拼接后逐条打分。"""
    def __init__(self, url: str, api_key: str, timeout: float = 10.0) -> None: ...
    async def rerank(self, query: str, candidates: list[KnowledgeHit], top_k: int) -> list[KnowledgeHit]:
        if not candidates:
            return []
        pairs = [(query, f"{c.row.title}\n{c.row.content[:512]}") for c in candidates]
        scores = await self._score(pairs)
        return _truncate(_reorder(candidates, scores), top_k)

class HeuristicReranker:
    """轻量精排：规则加权，零模型可用。默认实现。"""
    async def rerank(self, query: str, candidates: list[KnowledgeHit], top_k: int) -> list[KnowledgeHit]:
        return _truncate(sorted(candidates, key=lambda c: self._score(c, query), reverse=True), top_k)
    def _score(self, hit: KnowledgeHit, query: str) -> float:
        """规则打分：RRF 分 + 标题命中 + 有效期 + 状态优先。"""
        return (hit.score
                + 0.30 * _title_overlap(hit.row.title, query)
                + 0.10 * _is_effective_now(hit.row)
                + 0.05 * _region_affinity(hit.row.region_scope))
```

`HeuristicReranker` 是默认实现，`CrossEncoderReranker` 通过 `RERANK_ENABLED` 启用。默认走规则实现的原因是重排序处在一个高频路径（每次证据核查都调用），而 cross-encoder 需要一次远程调用，会把证据核查的延迟抬高一个量级。先用规则实现跑通链路，再按评测结果决定是否启用交叉编码器，符合方案"先跑通安全链路再优化"的取舍。

规则打分的权重有明确语义：`hit.score` 是 RRF 融合分（占比最大，保留融合结果的主体权重），标题命中加 0.30（标题比正文更能代表条目主题），有效期加 0.10（生效期内的条目优先），地区亲和加 0.05（条目 `region_scope` 与查询地区同前缀时加分）。权重不设成可配置项，避免"调参调出无法复现的排序"。

### 候选上限与截断

```python
RERANK_CANDIDATE_LIMIT = 30

def _truncate(hits: list[KnowledgeHit], top_k: int) -> list[KnowledgeHit]:
    """截断到 top_k，并重排名次前缀。"""
    return [hit.with_rank(i) for i, hit in enumerate(hits[:top_k], start=1)]
```

`search` 传给重排序的候选上限是 30（来自方案 9.3 的 `fused[:30]`），最终输出截断到 `query.top_k`。候选上限存在的意义是控制 cross-encoder 的打分条数：逐条打分是 O(n) 次远程调用或一次批量调用，条数过多会让重排成为瓶颈。30 是"两路各 20 条、去重后通常 25 到 35 条"的经验值，能在覆盖融合前排的同时不溢出。

`_truncate` 重排名次而不是保留原始 RRF 名次，因为对外暴露的证据顺序决定下游取样（如处方只取 `pesticide` 类前 6 条），重排后的名次才是最终语义。

### cross-encoder 的失败降级

```python
class FallbackReranker:
    """主重排不可用时回落到轻量实现。"""
    def __init__(self, primary: Reranker, fallback: Reranker) -> None: ...
    async def rerank(self, query: str, candidates: list[KnowledgeHit], top_k: int) -> list[KnowledgeHit]:
        try:
            return await asyncio.wait_for(self._primary.rerank(query, candidates, top_k),
                timeout=RERANK_TIMEOUT_SECONDS)
        except Exception as exc:                    # noqa: BLE001
            log.warning("rerank fallback", extra={"reason": str(exc), "count": len(candidates)})
            metrics.incr("rerank_fallback_total")
            return await self._fallback.rerank(query, candidates, top_k)
```

重排失败必须降级而不能抛错：证据核查智能体的契约是"返回证据集"，重排只是精排，拿融合结果直接返回仍然是可用结果。降级路径把主实现换成轻量实现，而不是返回空集——空集会让判定智能体误以为知识库没内容，从而输出"不确定"结论，而实际上候选是好的，只是精排服务挂了。这两种情况的区分对线上排查很重要，因此降级时上报 `rerank_fallback_total` 指标。

超时用 `asyncio.wait_for` 包住而不是依赖 HTTP 客户端超时，因为有些客户端超时不覆盖连接建立阶段。

### 幂等与确定性

重排序必须满足两条确定性要求：同一份候选、同一个查询，两次调用返回相同顺序；候选的输入顺序不影响输出顺序（即排序是全局的，不是稳定的相对排序）。

第一条通过"规则打分无随机性、cross-encoder 用 `temperature=0`"保证。第二条通过 `sorted` 的 key 只依赖 `hit` 与 `query`、不依赖输入下标保证。若用 `stable sort` 且 key 依赖输入顺序，两路召回的先后会改变重排结果，导致同一查询在不同时刻返回不同证据，评测无法复现。

当打分并列时，用 `knowledge_id` 做最终 tie-break，保证全局确定性。

### 与任务类型路由的关系

重排序若走轻量模型（用模型做相关性判定），其 `task_type` 为 `TaskType.RETRIEVE`，档位由 T2-57 决定（中等模型）。此处不直接调模型网关而是注入 `Reranker` 实例，由装配层决定具体实现。这样 rerank 模块不依赖网关的具体形态，也便于单测（注入假实现）。

但规则实现完全不依赖模型。技术方案 4.4 与全局约定都强调"凡是能被规则判定的，不交给模型"：标题命中、有效期、地区亲和都是规则可判定的信号，先用规则实现保证这条链路在 `USE_MOCK_LLM=true` 与模型全线不可用时仍可用。

## 验收标准

- `pytest tests/knowledge/test_rerank.py` 全部通过，至少含八条：`HeuristicReranker` 对标题含查询词的候选给出更高分、有效期外的候选分数低于有效期内、`top_k=3` 时只返回 3 条且名次为 1、2、3、空候选返回空列表、`FallbackReranker` 在主实现抛错时返回落实现结果且上报指标、主实现超时时走降级、并列分数以 `knowledge_id` 稳定定序、同一输入两次调用返回完全相同顺序
- 断言 `RERANK_CANDIDATE_LIMIT` 为 30，且 `KnowledgeRepository._rerank` 接收的候选数不超过 30
- 断言 `HeuristicReranker` 全流程不发起任何网络请求（`respx` 断言零请求），在 `USE_MOCK_LLM=true` 与其他模型配置缺失时均可用
- 断言 `_truncate` 在输入少于 `top_k` 时返回全部且名次连续
- 断言 `FallbackReranker` 降级时 `metrics.incr` 的指标名为 `rerank_fallback_total`
- 断言 `CrossEncoderReranker` 在 `RERANK_ENABLED=false` 时不被实例化（装配层断言）
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0

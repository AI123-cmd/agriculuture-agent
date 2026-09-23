# T2-42 流程专属红线规则 R-REDLINE

## 这一项要做什么

在 `app/gate/rules/process_redline.py` 中实现 `R-REDLINE`：命中当前流程 `redlines` 配置中任一禁用项即拦截。它是"红线由配置承载而非代码分支"这条设计（技术方案 8.4）在运行时的执行者——禁令措辞随流程与季节变化，写进 YAML 才能改一次配置就生效，写进代码就要发版。规则读 `LoadedRedlines.blocking_labels()` 做一次文本扫描，覆盖禁用品种名与禁用短语两类。等级 `block`。

## 前置依赖

- 依赖 T2-34：必须已提供 `Rule` 基类、`register`、`GateContext`、`GatePayload`、`GateViolation`、`GateRuleId`。
- 依赖 T2-06：必须已提供 `LoadedRedlines`、`Redline.labels()`（`forbidden` 与 `forbidden_phrases` 的并集）与 `blocking_labels()`；本项直接消费这两个方法，不自行解析配置。
- 依赖 T2-41：必须已提供 `GatePayload.rendered_text`，本项与 `R-PROVENANCE`、`R-CAUTION` 读同一份展示文本。
- 依赖 T2-04：七份 YAML 的红线内容（如 `tea.yaml` 的 `TEA-02` 含 `forbidden` 与 `forbidden_phrases`）是农技内容，需人工确认（全局约定第八节）。
- 外部条件：禁用清单与禁用短语的措辞为农技与产品共同确认范围（全局约定第八节）。

## 参考章节

- 技术方案 第8章 8.2 规则清单（`R-REDLINE` 一行：命中当前流程 `redlines` 配置的任一禁用项）
- 技术方案 第8章 8.4 流程红线配置化（`tea.yaml` 的 `TEA-02`：`data.forbidden` 与 `data` 的 `reference`；"红线的差异很大，必须由配置承载而非代码分支"）
- 技术方案 第9章 9.1 流程配置模型（`ProcessConfig.redlines` 的位置与 `TerminologySpec`）
- 技术方案 附录 A 的 `tea.yaml` 形态（`forbidden_pesticides`、`registered_only`、`forbidden_phrases` 三项）
- 产品说明书 第5章 5.3 茶流程禁用输出（不得触碰禁限用品种，含乙酰甲胺磷、丁硫克百威、乐果、氰戊菊酯等）；第4章 4.2.3 功能点 5（流程专属红线按流程取值）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/gate/rules/process_redline.py` | `ProcessRedlineRule` |
| 修改 | `app/gate/rules/__init__.py` | 导入本模块触发登记 |
| 新增 | `tests/gate/rules/test_process_redline.py` | 禁用品种、禁用短语、无红线三条用例组 |
| 修改 | `tests/gate/fixtures/fake_knowledge.py` | 增加构造 `LoadedRedlines` 的辅助函数 |

## 实现要点

### 规则实现

```python
@register
class ProcessRedlineRule(Rule):
    """R-REDLINE：命中当前流程红线配置的任一禁用项即拦截。

    禁用清单来自流程配置而非代码：植保口径与法规清单会变，
    写进 YAML 才能改配置即生效，写进代码就要发版（技术方案 8.4）。
    """

    rule_id = GateRuleId.REDLINE.value
    severity: GateSeverity = "block"
    description = "不得触碰本流程的禁用输出清单"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        violations: list[GateViolation] = []
        labels = ctx.redlines.blocking_labels()
        if not labels:
            return violations
        text = ctx.payload.rendered_text
        for label in labels:
            if not label:
                continue
            hit = self._locate(text, label, ctx)
            if hit is None:
                continue
            field, snippet = hit
            rule_ref = self._rule_ref(ctx, label)
            violations.append(self.violation(
                field=field,
                message=(f"命中本流程禁用项「{label}」（{rule_ref}）：{snippet}"),
            ))
        return violations

    @staticmethod
    def _locate(text: str, label: str, ctx: GateContext) -> tuple[str, str] | None:
        """在展示文本、动作文本与术语改写结果中定位禁用项。返回 (字段, 片段)。"""
        if label in text:
            return "gate_payload.rendered_text", _snippet(text, label)
        for i, action in enumerate(ctx.prescription.actions):
            joined = " ".join([action.action, action.timing, action.method,
                               *action.cautions, *action.stop_conditions])
            if label in joined:
                return f"actions[{i}]", _snippet(joined, label)
        for term, rewritten in ctx.prescription.detail_terms.items():
            if label in rewritten:
                return f"detail_terms[{term}]", _snippet(rewritten, label)
        return None

    @staticmethod
    def _rule_ref(ctx: GateContext, label: str) -> str:
        """反查该禁用项所属的红线 id，便于审计追溯到具体配置条目。"""
        for redline in ctx.redlines.redlines:
            if label in redline.labels():
                return f"{redline.id} / {redline.desc}"
        return "未归属到具体红线"


def _snippet(text: str, label: str, radius: int = 12) -> str:
    """截取命中位置的上下文，供审计表记录（不含会话原文，仅展示文本）。"""
    pos = text.find(label)
    if pos < 0:
        return label
    start = max(0, pos - radius)
    end = min(len(text), pos + len(label) + radius)
    return f"...{text[start:end]}..."
```

### 三个扫描位置与一次反查

**三个位置**是必要的：展示文本（`rendered_text`）是模型改写后的成品，动作文本是结构化的原文，`detail_terms` 是术语到口语化的改写映射。禁用项可能出现在任一位置——例如模型把"乐果"写进了 `actions[0].method` 却没出现在拼接后的展示文本里（展示层可能做了截断），或模型在 `detail_terms` 里把某个术语改写成了含禁用品种名的说法。只扫展示文本会漏掉这两类。

**一次反查**（`_rule_ref`）是 8.5 审计用途的要求：审计表要能回答"哪条红线被触发"。`blocking_labels()` 返回的是措辞并集，丢掉了归属信息，因此需要反查。反查的代价是一次线性遍历，红线数量在个位数，代价可忽略。

### 禁用品种与禁用短语的差别处理

`Redline.labels()` 把 `forbidden`（禁用品种名）与 `forbidden_phrases`（禁用短语）合并。两者在本规则中处理方式相同，但**监控与处置不同**：

| 类型 | 示例（茶流程） | 性质 | 处置 |
| --- | --- | --- | --- |
| 禁用品种（`forbidden`） | 乙酰甲胺磷、丁硫克百威、乐果、氰戊菊酯 | 硬约束，法规与植保口径 | 拦截并复核知识条目（8.5 第一条） |
| 禁用短语（`forbidden_phrases`） | "可放心随意加大用量"、"无需安全间隔期" | 表述约束，防止误导 | 拦截并调提示词（模型话术问题） |

两者都在 `block` 等级下，但复盘时的处理路径完全不同。实现上通过 `_rule_ref` 返回的红线 id 前缀区分（如 `TEA-02` 是品种，短语类通常单列一条红线 id）。若配置未把两类分成两条红线，建议在 T2-04 侧拆分，本规则不替配置做判断。

### 与文字匹配相关的两条防漏判措施

第一，**归一化匹配**。`label in text` 是子串匹配，会漏掉全半角差异与空格插入。本项在比较前对两侧做 `unicodedata.normalize("NFKC", ...)` 与去空白归一，复用 T2-39 `app/gate/matching.py` 的 `identity_key` 中同名步骤（抽成 `normalize_text`）。不做同义词扩展：禁用清单的措辞由农技确认，同义词应直接加进配置，而不是在代码里猜。

第二，**空标签跳过**。配置里若出现空字符串（YAML 书写错误），`label in text` 恒为真，会造成全量拦截。`if not label: continue` 是必须的一行防护。

### 与 T2-43 的分工

`R-REDLINE-LOAD`（T2-43）判断"红线配置是否成功加载"，`R-REDLINE`（本项）判断"处方的文本是否命中已加载的红线"。前者在前者失败时，引擎会短路，本项根本不会被执行——这正是"红线缺失即不可用"的实现：配置不可用时，不是"用空清单跑一遍本规则然后放行"，而是整体 block。

### 拦截频率监控方式

`gate_audit` 中按 `rule_id = 'R-REDLINE'` 聚合，并利用 `_rule_ref` 的信息做归因。

- **按红线 id 分组的命中数**：这是 8.5 第一条复盘用途的核心数据。每个 block 都触发一条知识条目复核工单，按红线 id 分组后可以直接判断"是知识库缺条目"（某条禁用品种频繁被推荐，说明检索侧没有把它排除）还是"模型越界"。
- **禁用品种 vs 禁用短语的比例**：前者指向检索侧与知识库，后者指向提示词。两者的修复责任人与周期都不同，必须分开看。
- **`_rule_ref` 返回"未归属到具体红线"的比例**：这条必须为 0。非 0 说明 `ReloadedRedlines` 的标签归属与扫描用的并集不同步，属实现缺陷，应立即修复。
- **季节性观察**：茶树禁限用清单与用药高峰相关，春季命中数应随用药量上升。若某条禁用品种在禁采期内命中数不降反升，说明该品种的登记信息有变更，需农技复核。
- **与 `R-WHITELIST` 的交叉**：`R-REDLINE` 命中而 `R-WHITELIST` 未命中，说明该禁用品种在知识库中仍是 `approved` 状态，属知识库治理问题（应将条目置为 `suspended`），这是最有价值的一类告警。

## 验收标准

- `pytest tests/gate/rules/test_process_redline.py` 全部通过，含正例与反例各不少于 2 条：
  - 正例 1：`redlines` 含 `forbidden=["乐果"]`，文本不含"乐果"，返回空列表。
  - 正例 2：`redlines.blocking_labels()` 为空（红线只有 degrade 级），返回空列表。
  - 反例 1：`redlines` 含 `forbidden=["乐果"]`，`rendered_text` 含"可选用乐果防治"，返回 1 条 block 违规，`field == "gate_payload.rendered_text"`，message 含"乐果"与红线 id。
  - 反例 2：`redlines` 含 `forbidden_phrases=["无需安全间隔期"]`，`actions[0].cautions` 含该短语但 `rendered_text` 不含，返回 1 条违规，`field == "actions[0]"`。
  - 反例 3：`detail_terms` 的某个改写值含禁用品种名，返回 1 条违规，`field` 以 `detail_terms[` 开头。
- 断言 message 中通过 `_rule_ref` 带出了命中的红线 id（如 `TEA-02`），并断言在某条红线不计入 `blocking_labels` 时不会误归属。
- 断言全角与半角混写（`"乐 果"` 与 `"乐果"`）经归一后仍能命中；构造空字符串标签，断言不产生违规。
- 断言 `rule_id == "R-REDLINE"`、`severity == "block"`，且 `TEA-02` 的配置在 `LoadedRedlines` 中确实进入 `blocking_labels()`。
- 断言 `R-REDLINE` 在 `ctx.redlines.loaded=False` 时不会被引擎执行（引擎短路，`checked_rules` 不含 `R-REDLINE`）。
- 该模块内无 `app.agent` / `app.services` 导入（L1）。
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0。

# T2-41 数字溯源规则 R-PROVENANCE

## 这一项要做什么

在 `app/gate/rules/provenance.py` 中实现 `R-PROVENANCE`：处方展示文本中出现的每一个数值都必须能在 `facts_provenance` 中登记。这是把产品说明书 6.4 第 4 条"不出现无数据支撑的数字"从一句原则变成自动检查的关键规则，也是"模板出事实、模型只改写"的最后一道防线——模型可以改写措辞，但不能引入任何未经登记的数字。技术方案 8.3.3 给出了本规则的完整实现，本项**原样保留该实现**（含 `UNIT_PATTERN` 正则与 `STRUCTURAL` 豁免集合），并落实方案对豁免清单"必须极短"的约束。等级 `block`。

## 前置依赖

- 依赖 T2-34：必须已提供 `Rule` 基类、`register`、`GateContext`、`GatePayload`、`GateViolation`、`GateRuleId`。
- 依赖 T2-11：必须已提供 `Prescription.facts_provenance` 的结构（`{"字段路径": "来源标识"}`，来源标识形如 `knowledge:K-1023` 或 `knowledge:K-1023:field:dose`）。
- 依赖 T2-21：必须已提供 `build_provenance`，处方层为每个数值登记来源；本规则是反向扫描的比对基准。
- 依赖 T2-44：`R-NO-DATA` 与本规则同为"数字管控"，二者共用本模块的正则与豁免集合，分界见下文。
- 外部条件：无。

## 参考章节

- 技术方案 第8章 8.3.3 数字溯源扫描（本规则的完整实现原文、`UNIT_PATTERN` 正则、`STRUCTURAL` 集合与"豁免清单必须极短"的实现要点）
- 技术方案 第8章 8.2 规则清单（`R-PROVENANCE` 一行：处方文本中的每个数字必须在 `facts_provenance` 中登记）
- 技术方案 第5章 5.3.5 处方契约（`facts_provenance` 的字段结构与两条来源标识形态）
- 全局约定 第四节 事实必带溯源（任何数值型字段必须配套 provenance 字段；没有 provenance 的数字不允许进入处方与提示）
- 产品说明书 6.4 第 4 条（不出现无数据支撑的数字）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/gate/rules/provenance.py` | `ProvenanceRule`（方案 8.3.3 原文 + 注册装饰器 + 常量外提） |
| 修改 | `app/gate/rules/__init__.py` | 导入本模块触发登记 |
| 新增 | `tests/gate/rules/test_provenance.py` | 命中/豁免/中文数字三类用例；含 STRUCTURAL 大小断言 |
| 新增 | `tests/gate/test_structural_exemptions.py` | CI 断言豁免集合不超过 5 |

## 实现要点

### 方案 8.3.3 原文（逐字保留）

```python
# gate/rules/provenance.py
import re

class ProvenanceRule:
    """R-PROVENANCE：处方文本中的每个数字必须在 facts_provenance 中登记。

    实现方式：对输出的展示文本做数字抽取（含中文数字与单位），
    逐个核对是否在 provenance 表中有对应键。未登记即拦截。
    """
    rule_id = "R-PROVENANCE"
    severity = "block"

    # 单位白名单：这些词后面的数字属于必须溯源的"事实性数字"
    UNIT_PATTERN = re.compile(
        r"(\d+(?:\.\d+)?)\s*(亩|克|毫升|千克|公斤|升|%|℃|天|小时|次|头|株|丛|片)"
    )
    # 允许出现的非事实性数字（序号、步骤编号）
    STRUCTURAL = {"1", "2", "3", "4", "5"}

    def check(self, ctx: GateContext) -> list[GateViolation]:
        violations = []
        text = ctx.gate_payload.rendered_text
        registered_values = set(ctx.prescription.facts_provenance.keys())

        for match in self.UNIT_PATTERN.finditer(text):
            number, unit = match.group(1), match.group(2)
            token = f"{number}{unit}"
            if token in registered_values or number in self.STRUCTURAL:
                continue
            violations.append(GateViolation(
                rule_id=self.rule_id,
                severity="block",
                message=f"文本中出现的数值「{token}」无事实来源登记",
                field="facts_provenance",
            ))
        return violations
```

原文保留，不删不改。以下三处是在原文之上的补全。

第一，**加 `@register` 与类继承**（`class ProvenanceRule(Rule):`），并把 `UNIT_PATTERN` 与 `STRUCTURAL` 外提到模块级常量 `UNIT_PATTERN` 与 `STRUCTURAL_EXEMPTIONS`，类属性引用它们。外提的原因：T2-44 的 `R-NO-DATA` 需要复用同一份正则与豁免集合，若各写一份，两个集合会漂移，出现"这条规则豁免了、那条没豁免"的不一致。

第二，**实现 `rendered_text` 的一致口径**。原文用 `ctx.gate_payload.rendered_text`，本项在 `GatePayload` 中定义该字段为"处方卡最终展示文本"，取值顺序为：`detail_terms` 替换后的动作文本 → 拼接 `cautions` → 拼接 `stop_conditions`。`R-CAUTION` 与 `R-REDLINE` 读同一个字段，三条规则看到的是同一份文本，避免"扫描 A 文本、放行 B 文本"的漏洞。

第三，**登记值的键形态**。原文用 `set(ctx.prescription.facts_provenance.keys())`，把 provenance 的**键**（字段路径）当作已登记值集合，与文本中抽取出的 `token`（如 `30克`）比对。这里存在一个必须写明的推断：`facts_provenance` 的键是**字段路径**（`pesticide_items[0].dosage_max`），而文本中的 token 是**数值加单位**（`30克`），两者形态不同，直接比对必然全部不匹配。因此本项在 `GateContext` 上增加一个派生方法 `registered_numeric_tokens()`，由处方层在生成时同步写入一个"值 → 字段路径"的映射，闸门读该映射而非读键。原文的 `registered_values` 一行保留原样，但改为 `set(ctx.registered_numeric_tokens())`，语义（"文本中的数值是否已登记"）不变，比对对象从字段路径改为数值 token。

```python
# app/gate/contracts.py 中的派生方法
    def registered_numeric_tokens(self) -> dict[str, str]:
        """返回「数值 token → 字段路径」。由处方层在 build_provenance 时同步填充。

        为什么不是直接用 facts_provenance 的键：键是字段路径
        （pesticide_items[0].dosage_max），文本里出现的是数值加单位（30克），
        两者形态不同，必须由生成侧提供映射才能做反向扫描。
        """
        return dict(self.numeric_tokens)
```

### 豁免清单必须极短（硬性）

`STRUCTURAL_EXEMPTIONS` 用于豁免步骤序号这类结构性数字。技术方案 8.3.3 明确："豁免清单必须极短且有明确语义，否则会变成漏洞。建议在 CI 中加一条测试，断言 `STRUCTURAL` 集合大小不超过 5。"

```python
# app/gate/rules/provenance.py
# 结构性数字豁免：仅限处方卡的步骤序号 1~5，带单位的事实性数字一律不豁免。
# 该集合的每个成员都必须能对应到输出模板里的一个具体位置，否则不许加入。
STRUCTURAL_EXEMPTIONS: frozenset[str] = frozenset({"1", "2", "3", "4", "5"})
_STRUCTURAL_MAX = 5
```

CI 断言：

```python
# tests/gate/test_structural_exemptions.py
from app.gate.rules.provenance import STRUCTURAL_EXEMPTIONS, _STRUCTURAL_MAX

def test_structural_exemptions_stay_small():
    """豁免集合是漏洞的入口。规模上限写死在测试里，扩集合必须同时改测试并说明理由。"""
    assert len(STRUCTURAL_EXEMPTIONS) <= _STRUCTURAL_MAX

def test_structural_exemptions_are_integers():
    """豁免项必须是纯数字序号，不允许出现带单位的 token（如 '7天'）。"""
    assert all(item.isdigit() for item in STRUCTURAL_EXEMPTIONS)
```

第二条断言比第一条更重要：它堵住了"把 `7天` 加进豁免集合"这条最可能的漏洞路径。豁免只对纯序号开放，任何带单位的 token 一律不可豁免。

### 中文数字的处理边界

原文的 docstring 写"含中文数字与单位"，但 `UNIT_PATTERN` 只匹配阿拉伯数字。本项不改正则（原文保留优先），而是在文档中明确当前边界：展示层要求所有事实性数字使用阿拉伯数字（处方层与展示层已如此约定，`PrescriptionAction` 的各字段由代码填充），因此中文数字（"七天"、"三亩"）不会出现；若未来展示层引入中文数字，需新增一条规则而非放宽本正则。这一边界要在模块顶部注释写清，防止执行者擅自扩展正则。

### 与 R-NO-DATA 的分界

| 规则 | 检查对象 | 判定 | 等级 |
| --- | --- | --- | --- |
| `R-PROVENANCE`（本项） | 展示文本中的数字 + `facts_provenance` | 文本里出现的数字是否有登记 | block |
| `R-NO-DATA`（T2-44） | `Prescription` 数值字段 + `facts_provenance` | 结构里的数值字段是否有登记 | block |

前者是**反向扫描**（从文本找数字），后者是**正向核对**（从字段找漏登记）。两层互补：正向核对拦住"结构里漏登记"，反向扫描拦住"文本里多出来的数字"（模型改写时插入的数字不会出现在任何字段里，只有反向扫描能发现）。两条规则共用 `UNIT_PATTERN` 与 `STRUCTURAL_EXEMPTIONS`，但不共用 `check`。

### 拦截频率监控方式

`gate_audit` 中按 `rule_id = 'R-PROVENANCE'` 聚合，并对命中的 token 做集中统计。

- **命中 token 的分布**：把 `violations[].message` 里的 token 抽取出来做 top-N。若高频 token 是具体的剂量数值（如 `30克`），说明处方层的 `build_provenance` 漏登记了某个字段路径，属确定性缺陷，可修复；若高频 token 各式各样，说明模型在自由改写时引入数字，属提示词问题。
- **与 `R-NO-DATA` 的联合观察**：只命中 `R-PROVENANCE` 而不命中 `R-NO-DATA`，说明字段齐全但模型在文本里加了数字，指向提示词的"不得改动任何数字"约束失效；两条同时命中，说明处方层根本没填 `facts_provenance`，属上游缺陷。
- **STRUCTURAL 豁免的使用频率**：统计被豁免的 token 实际出现次数。豁免项若从未被用到，应删除（保持集合最小）；若某一序号（如 `5`）频繁被豁免而处方卡其实只有 3 个步骤，说明豁免被当成规避手段，需人工介入。
- 兜底：该规则命中数为 0 且 `R-NO-DATA` 也为 0，需检查 `GatePayload.rendered_text` 是否为空字符串——空文本会让本规则静默通过。

## 验收标准

- `pytest tests/gate/rules/test_provenance.py` 全部通过，含正例与反例各不少于 2 条：
  - 正例 1：文本含 `30克`，`numeric_tokens` 含 `{"30克": "pesticide_items[0].dosage_max"}`，返回空列表。
  - 正例 2：文本含步骤序号 `1. 清理病株`，无带单位数字，返回空列表（豁免生效）。
  - 反例 1：文本含 `每亩用50克`，`numeric_tokens` 为空，返回 1 条 block 违规，message 含"50克"与"无事实来源登记"。
  - 反例 2：文本含 `安全间隔期7天`，`numeric_tokens` 只登记了 `30克`，返回 1 条 block 违规，`field == "facts_provenance"`。
- `pytest tests/gate/test_structural_exemptions.py` 全部通过，断言 `len(STRUCTURAL_EXEMPTIONS) <= 5` 且每项均为纯数字字符串；构造 `"7天"` 加入集合的假设样例，断言第二条测试会失败。
- 断言 `UNIT_PATTERN` 能匹配 `亩`、`克`、`毫升`、`千克`、`公斤`、`升`、`%`、`℃`、`天`、`小时`、`次`、`头`、`株`、`丛`、`片` 全部 15 个单位，各给出一个样例断言。
- 断言 `rule_id == "R-PROVENANCE"`、`severity == "block"`，`build_rules(["R-PROVENANCE"])` 可实例化。
- 断言 `GatePayload` 含 `rendered_text` 字段，且 `R-REDLINE`、`R-CAUTION` 读的是同一字段（用 grep 断言三处引用同名属性）。
- 该模块内无 `app.agent` / `app.services` 导入（L1）。
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0。

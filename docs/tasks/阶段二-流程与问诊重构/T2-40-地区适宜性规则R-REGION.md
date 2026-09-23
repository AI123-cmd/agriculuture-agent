# T2-40 地区适宜性规则 R-REGION

## 这一项要做什么

在 `app/gate/rules/region_fitness.py` 中实现 `R-REGION`：处方建议必须与农户所在地区、当前生育阶段匹配。等级是 `degrade` 而非 `block`——地区不匹配通常意味着"这条建议在你那儿可能不适用"，而不是"这条建议有害"，正确处置是把输出整体替换为转人工引导文案，而不是拒绝服务。该规则同时承载茶流程的出口标准要求（`TEA-05`：出口茶园不得忽略输入国标准），把残留限量标准纳入地区维度。

## 前置依赖

- 依赖 T2-34：必须已提供 `Rule` 基类、`register`、`GateContext`、`GateViolation`、`GateRuleId`、`ctx.region_code`、`DEGRADE_OUTPUT_TEXT`。
- 依赖 T2-35：必须已提供 `KnowledgeFacts` 假实现；本项需要 `region_scope` 字段。
- 依赖 T2-06：`RULE_PAYLOAD_SPEC` 中 `R-REGION` 接受键 `{regions, standards}`；`TEA-05` 声明 `severity: degrade`。
- 依赖 T2-50（地区回退匹配）：知识库层的行政区划编码前缀规则由它提供；本项只做前缀比对，不自行构造回退链。
- 依赖 T2-08：`ConsultationState` 中的地块与档案地区字段是 `ctx.region_code` 的来源。
- 外部条件：残留限量标准与出口国要求为农技/法规内容，需人工确认（全局约定第八节）。

## 参考章节

- 技术方案 第8章 8.2 规则清单（`R-REGION` 一行：建议必须与农户所在地区、当前生育阶段匹配，等级 degrade）
- 技术方案 第8章 8.4 流程红线配置化（`tea.yaml` 的 `TEA-05`：出口茶园不得忽略输入国标准，`rule: R-REGION`，`severity: degrade`）
- 技术方案 第9章 9.2 知识库分层（L2 农时知识带 `region_code`，L3 条目带 `region_scope`）
- 全局约定 第六节 地区回退保留实际命中级（按县查不到退市、退省、全国通用；每次回退记下实际命中级）
- 产品说明书 第5章 5.3 茶流程（出口茶园不得忽略输入国标准）；第4章 4.2.3 功能点 8（类型不在六条之内时进入通用农业流程并降低确定性表述）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/gate/rules/region_fitness.py` | `RegionFitnessRule` |
| 修改 | `app/gate/rules/__init__.py` | 导入本模块触发登记 |
| 新增 | `tests/gate/rules/test_region_fitness.py` | 区划前缀匹配与出口标准分支的正反例 |
| 修改 | `app/gate/constants.py` | 增加 `REGION_MISMATCH_NOTE` 与 `EXPORT_STANDARD_NOTE` 文案常量 |

## 实现要点

### 规则实现

```python
@register
class RegionFitnessRule(Rule):
    """R-REGION：建议必须与所在地地区、当前阶段匹配。

    等级为 degrade：地区不匹配的正确处置是引导农户咨询本地农技站，
    而不是拒绝服务——农户所在地区与知识条目适用范围不一致，不代表建议有害。
    """

    rule_id = GateRuleId.REGION.value
    severity: GateSeverity = "degrade"
    description = "建议必须与所在地地区匹配"

    def check(self, ctx: GateContext) -> list[GateViolation]:
        violations: list[GateViolation] = []
        if ctx.region_code:
            violations.extend(self._check_knowledge_scope(ctx))
        violations.extend(self._check_declared_regions(ctx))
        violations.extend(self._check_export_standards(ctx))
        return violations

    def _check_knowledge_scope(self, ctx: GateContext) -> list[GateViolation]:
        """药剂条目的 region_scope 必须覆盖农户所在地区。"""
        out: list[GateViolation] = []
        scope = _region_prefix(ctx.region_code)
        for item in ctx.prescription.pesticide_items:
            entry = ctx.knowledge.get_entry(item.knowledge_id)
            if entry is None or not entry.region_scope:
                continue                      # 无地区限定＝全国通用
            if not _matches_prefix(scope, entry.region_scope):
                out.append(self.violation(
                    field=f"pesticide_items[{item.knowledge_id}].region_scope",
                    message=(f"《{item.name}》的适用范围为 {entry.region_scope}，"
                             f"不覆盖你所在的 {ctx.region_code}"),
                ))
        return out

    def _check_declared_regions(self, ctx: GateContext) -> list[GateViolation]:
        """红线配置声明的适用地区清单核对（data.regions）。"""
        raw = ctx.rule_configs.get(GateRuleId.REGION.value, {}).get("regions")
        if not raw or not ctx.region_code:
            return []
        allowed = [str(r) for r in raw] if isinstance(raw, (list, tuple)) else [str(raw)]
        scope = _region_prefix(ctx.region_code)
        if not any(_matches_prefix(scope, a) for a in allowed):
            return [self.violation(
                field="region_code",
                message=f"本流程建议的适用地区为 {'、'.join(allowed)}，与 {ctx.region_code} 不一致",
            )]
        return []

    def _check_export_standards(self, ctx: GateContext) -> list[GateViolation]:
        """TEA-05：出口茶园不得忽略输入国标准。data.standards 声明需要的标准。"""
        raw = ctx.rule_configs.get(GateRuleId.REGION.value, {}).get("standards")
        if not raw:
            return []
        standards = [str(s) for s in raw] if isinstance(raw, (list, tuple)) else [str(raw)]
        covered = ctx.prescription.facts_provenance.values()
        missing = [s for s in standards if not any(s in str(v) for v in covered)]
        if missing:
            return [self.violation(
                field="facts_provenance",
                message=(f"出口茶园处方未引用输入国/地区标准 {'、'.join(missing)}，"
                         f"无法确认残留限量合规"),
            )]
        return []


def _region_prefix(code: str) -> str:
    """取行政区划编码的稳定前缀。省 2 位、市 4 位、县 6 位。"""
    digits = "".join(ch for ch in code if ch.isdigit())
    return digits


def _matches_prefix(scope: str, entry_scope: str) -> bool:
    """上级编码覆盖下级：条目范围 330000 覆盖农户 330100。"""
    a, b = _region_prefix(scope), _region_prefix(entry_scope)
    if not a or not b:
        return True                            # 无法比对时不报（degrade 规则的保守方向）
    return a.startswith(b) or b.startswith(a)
```

### 等级为什么是 degrade 而不是 block

`R-REGION` 是 8.2 中唯一一条以 `degrade` 为默认等级的规则，另两条相关等级是 `R-CAUTION` 的 `warn` 与其余全部 `block`。技术方案 5.3.6 明确 `degrade` 的语义是"不通过但不属严重违规，输出替换为 `degrade_output`（标准转人工引导文案）"，且产品说明书 6.2 要求"校验不通过时，输出降级为建议联系当地农技站，而不是修改后输出"。地区不匹配恰好落在这个档位：知识条目在其他省份可能同样有效，但系统无法确认，因此不能给农户一个未经确认的方案，也不能断言方案有害。degrade 是唯一正确的处置。

### 三条分支的边界

| 分支 | 输入 | 触发条件 | 不触发条件 |
| --- | --- | --- | --- |
| `_check_knowledge_scope` | 条目的 `region_scope` | 条目有地区限定且不覆盖农户所在地区 | 条目无 `region_scope`（视为全国通用） |
| `_check_declared_regions` | 红线 `data.regions` | 配置声明了适用地区且农户不在其中 | 配置未声明 `regions`，或 `ctx.region_code` 为空 |
| `_check_export_standards` | 红线 `data.standards` + `facts_provenance` | 配置声明了标准且处方溯源未引用 | 配置未声明 `standards` |

第三条分支的形态值得说明：它不检查"残留限量数值是否达标"（那需要一本标准库，属知识库层职责），而是检查"这张处方是否引用了相关标准"。这是个弱检查，但它是 fail-closed 的正确粒度——未引用标准等于无法证明合规，因此 degrade；引用了标准则说明生成侧已把标准作为事实纳入溯源，合规性由知识条目的核准流程保证。

### 与地区回退的关系

本规则不执行地区回退。回退发生在知识检索层（T2-50）：按县级查不到退市级、再退省级、最后全国通用，且每次回退都在结果里记下实际命中的级别。闸门拿到的是已经过回退的检索结果，因此本规则看到的是"条目实际命中的 scope"。二者的分工是：检索层负责"尽量找到适用的条目"，闸门负责"确认找到的条目确实适用于该地区"。若闸门频繁命中 `_check_knowledge_scope`，说明回退链的层级选择有问题，应修 T2-50，而不是放宽本规则。

### 拦截频率监控方式

`gate_audit` 中按 `rule_id = 'R-REGION'` 聚合。由于本规则只产生 degrade，它的命中直接推高 8.5 第二条复盘用途中的 degrade 率指标。

- **按 `process_id` 与地区分组**：某条流程在特定省份的 degrade 率显著偏高，说明该地区的知识条目覆盖不足（8.5 明确"某条流程 degrade 率持续偏高，说明该流程的知识库覆盖不足，而不是模型不行"）。这条指标应驱动知识库补条目，而不是调整规则。
- **按分支拆分**：`_check_export_standards` 的命中代表出口茶园标准缺失，属知识库专项缺口；`_check_knowledge_scope` 命中代表地区覆盖缺口；`_check_declared_regions` 命中通常意味着流程配置的 `regions` 与档案地区字典不同步，属配置错误。
- **误杀率校准**：按 14.2 的要求，灰度期用影子模式（T2-46）记录本规则的判定，人工抽查"被降级但不该降级"的比例，低于阈值后才切强制拦截。本规则是误杀风险最高的规则，应最后切换。

## 验收标准

- `pytest tests/gate/rules/test_region_fitness.py` 全部通过，含正例与反例各不少于 2 条：
  - 正例 1：`region_code="330100"`，条目 `region_scope="330000"`（省覆盖市），返回空列表。
  - 正例 2：条目 `region_scope=None` 且配置未声明 `regions`/`standards`，返回空列表。
  - 反例 1：`region_code="330100"`，条目 `region_scope="320000"`，返回 1 条 `degrade` 违规，message 含"不覆盖你所在的 330100"。
  - 反例 2：配置 `rule_configs["R-REGION"]["regions"]=["320000"]` 且 `region_code="330100"`，返回 1 条 `degrade` 违规，message 含"适用地区"。
  - 反例 3：配置 `standards=["EU MRL"]` 且 `facts_provenance` 未引用该标准，返回 1 条 `degrade` 违规，message 含"EU MRL"。
- 断言该规则 `severity == "degrade"`（不是 block），且引擎在仅有该违规时 `decision="degrade"`、`degrade_output` 非空、`passed=False`。
- 断言 `degrade_output` 的内容为标准转人工引导文案常量 `DEGRADE_OUTPUT_TEXT`，且该文案中不含任何被降级的原始处方文本（degrade 是整体替换，不是修改后输出）。
- 断言 `_matches_prefix("330100", "330000")` 为真、`_matches_prefix("330100", "3301")` 为真、`_matches_prefix("330100", "320000")` 为假。
- `ctx.region_code=None` 时三条分支均不触发，返回空列表。
- 该模块内无 `app.agent` / `app.services` 导入（L1）。
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0。

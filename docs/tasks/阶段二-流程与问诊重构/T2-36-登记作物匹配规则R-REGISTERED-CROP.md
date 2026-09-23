# T2-36 登记作物匹配规则 R-REGISTERED-CROP

## 这一项要做什么

在 `app/gate/rules/registered_crop.py` 中实现 `R-REGISTERED-CROP`：药剂条目的登记作物必须包含当前流程对应的作物。这是茶流程最高频的拦截点——茶农面临的不只是减产，而是"鲜叶入口"带来的农残风险，因此茶流程要求药剂同时满足"已登记 + 安全间隔期 + 残留限量"三重条件中的第一重。等级 `block`。技术方案 8.3.1 给出了本规则的完整实现，本项**原样保留该实现**，只在其上补齐注册装饰器、`KnowledgeFacts` 协议方法与测试。

## 前置依赖

- 依赖 T2-34：必须已提供 `Rule` 基类、`register` 注册表、`GateContext`、`GateViolation`、`GateRuleId`。
- 依赖 T2-35：必须已提供 `KnowledgeFacts` 假实现与 `tests/gate/fixtures/fake_knowledge.py`；本项需要其中的 `registered_crops` 字段。
- 依赖 T2-04：必须已提供七条流程配置的作物口径；`ProcessId.TEA` 对应"茶"，与茶流程配置的 `object_aliases` 与红线 `TEA-01` 保持一致。
- 依赖 T2-01：`ProcessId` 的七值（本项经 `app.gate.enums` 的镜像枚举读取）。
- 外部条件：登记作物清单、登记作物名措辞为农技内容，需人工确认（全局约定第八节）。
- 与 T2-42 的关系：`TEA-01` 在配置里引用 `R-REGISTERED-CROP`，`TEA-02` 引用 `R-REDLINE`，两条规则的职责在配置层已分开。

## 参考章节

- 技术方案 第8章 8.3.1 登记作物匹配（本规则的完整实现原文，含 `CROP_MAPPING` 与茶流程三重条件的说明）
- 技术方案 第8章 8.2 规则清单（`R-REGISTERED-CROP` 一行：药剂登记作物必须包含当前 `process_id` 对应作物，茶流程强制）；第8章 8.4 流程红线配置化（`tea.yaml` 的 `TEA-01`）
- 技术方案 第9章 9.2 知识库分层（L3 处置知识的 `registered_crops` 字段）
- 产品说明书 第5章 5.3 茶流程（不得推荐未在茶树上登记的农药；茶流程三重条件）；第4章 4.2.3 功能点 5（流程专属红线举例）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/gate/rules/registered_crop.py` | `RegisteredCropRule`（方案 8.3.1 原文 + 注册装饰器） |
| 修改 | `app/gate/rules/__init__.py` | 导入本模块触发登记 |
| 修改 | `app/gate/ports.py` | `KnowledgeFacts` 增加 `get_registered_crops` 方法声明 |
| 新增 | `tests/gate/rules/test_registered_crop.py` | 茶/水稻正例与两条反例；杂草流程豁免用例 |

## 实现要点

### 方案 8.3.1 原文（逐字保留）

```python
# gate/rules/registered_crop.py
class RegisteredCropRule:
    """R-REGISTERED-CROP：药剂登记作物必须包含当前流程对应作物。

    茶流程要求最严：药剂需同时满足已登记 + 安全间隔期 + 残留限量三重条件。
    """
    rule_id = "R-REGISTERED-CROP"
    severity = "block"

    # 流程 → 登记作物名（植保口径）
    CROP_MAPPING = {
        ProcessId.TEA: "茶",
        ProcessId.RICE: "水稻",
        ProcessId.CHILI: "辣椒",
        ProcessId.RAPESEED: "油菜",
    }

    def check(self, ctx: GateContext) -> list[GateViolation]:
        registered = self.CROP_MAPPING.get(ctx.process_id)
        if registered is None:
            return []  # 杂草/通用/养猪不做作物登记匹配
        violations = []
        for item in ctx.prescription.pesticide_items:
            crops = ctx.knowledge.get_registered_crops(item.knowledge_id)
            if registered not in crops:
                violations.append(GateViolation(
                    rule_id=self.rule_id,
                    severity="block",
                    message=(
                        f"药剂《{item.name}》未在{registered}上登记"
                        f"（登记作物：{'、'.join(crops) or '无'}）"
                    ),
                    field=f"pesticide_items[{item.knowledge_id}]",
                ))
        return violations
```

原文保留，不删不改。以下三处是在原文之上的补全，逐条说明理由。

第一，**加 `@register` 装饰器与类型继承**。原文未写注册与继承，因为它示范的是 `check` 的形态；落地时必须接上 T2-34 的注册表，否则规则不会被引擎加载。补全后类声明为 `class RegisteredCropRule(Rule):`，并在类体首行保留 `rule_id` 与 `severity` 的写法（`severity` 标注为 `GateSeverity`）。

第二，**`crops` 的来源用协议方法**。原文调用 `ctx.knowledge.get_registered_crops(item.knowledge_id)`，本项在 `app/gate/ports.py` 的 `KnowledgeFacts` 中显式声明该方法，返回 `tuple[str, ...]`。返回元组而不是列表：`frozenset` 也能用，但元组保留知识库里的登记顺序，便于提示语按原序展示。

第三，**空登记作物的提示语**。原文用 `'、'.join(crops) or '无'` 处理空集合，保留该写法。`crops` 为空意味着条目缺 `registered_crops` 字段，这属于知识条目残缺，与"未登记在当前作物"不同，运维上要能看出来，因此 `'无'` 不改成"未登记"。

### CROP_MAPPING 为什么只有四条约流程

`CROP_MAPPING` 只覆盖 `TEA`、`RICE`、`CHILI`、`RAPESEED` 四条作物流程，杂草、养猪、通用返回空列表即放行。三条理由。

- 杂草防治是跨作物的问题类型（`ProcessId.WEED` 不绑定地块），同一除草剂在不同作物上的登记差异无法用单一"登记作物"表达，登记匹配在该流程内无定义。
- 养猪流程不涉及农药登记体系，用药由执业兽医负责，红线是"不得输出用药方案替代执业兽医"（`R-REDLINE`），不是登记匹配。
- 通用农业流程是兜底路径，知识条目来自其他流程的底层知识，登记作物字段可能不齐；在该流程强制登记匹配会导致大面积误杀，正确处置是降低确定性表述（`R-REGION` 的 degrade）而非拦截。

油菜流程的特别处理：产品说明书 5.4 明确油菜知识库补齐前"不得输出药剂推荐类内容"，因此油菜的处置应发生在检索层（`kind_filter` 排除 `pesticide`，见 T2-18 与 T2-21），登记匹配作为第二道防线保留在 `CROP_MAPPING` 中。

### 与茶流程三重条件的关系

茶流程的三重条件是"已登记 + 安全间隔期 + 残留限量"。本规则只实现第一重。

| 条件 | 负责规则 | 说明 |
| --- | --- | --- |
| 已登记 | `R-REGISTERED-CROP`（本项） | 登记作物含"茶" |
| 安全间隔期 | `R-INTERVAL`（T2-38） | 显式给出且与采摘计划不冲突 |
| 残留限量 | `R-REGION`（T2-40） | 出口茶园按输入国标准校验，等级 degrade |

三者分散在三条规则里而不是合成一条"茶流程三重校验规则"，原因是每条规则的输入与等级各不相同：登记作物来自知识条目、间隔期来自处方字段与槽位、残留限量来自地区与标准配置。合成一条会让该规则成为唯一无法单测的规则，也会让等级（block 与 degrade）无法表达。

### 拦截频率监控方式

`gate_audit` 中按 `rule_id = 'R-REGISTERED-CROP'` 聚合，并按下述方式拆分。

- 按 `process_id` 分组：茶流程命中数应显著高于其他流程，这是设计预期（其他三条作物流程登记面较宽）。若水稻流程命中数超过茶流程，说明水稻知识库的 `registered_crops` 录入有误。
- 按 `violations[].field` 的去重计数：同一 `knowledge_id` 反复命中说明该条目本身登记不全，应触发知识条目复核工单（8.5 第一条），而不是调提示词。
- 与 `R-WHITELIST` 的联合观察：`R-WHITELIST` 命中少而本规则命中多，说明知识库有该药剂但登记作物不含当前作物，属"有事实但事实不适用"，是知识库覆盖问题；反之则属模型越界。两者的比值是最有诊断价值的单一指标。

## 验收标准

- `pytest tests/gate/rules/test_registered_crop.py` 全部通过，含正例与反例各不少于 2 条：
  - 正例 1：茶流程，药剂条目 `registered_crops=("茶", "茶树")`，`check` 返回空列表。
  - 正例 2：杂草流程（`ProcessId.WEED`），任何药剂条目均返回空列表（`CROP_MAPPING` 未覆盖即豁免）。
  - 反例 1：茶流程，条目 `registered_crops=("水稻", "小麦")`，返回 1 条 block 违规，message 含"未在茶上登记"与"登记作物：水稻、小麦"。
  - 反例 2：水稻流程，条目 `registered_crops=()`，返回 1 条 block 违规，message 含"登记作物：无"。
- 断言 `RegisteredCropRule.CROP_MAPPING` 的键集合恰为 `{TEA, RICE, CHILI, RAPESEED}` 四值，且 `GENERAL` 与 `PIG` 不在其中。
- 断言 `build_rules(["R-REGISTERED-CROP"])` 能实例化该规则，`rule_id == "R-REGISTERED-CROP"`、`severity == "block"`。
- 断言 `KnowledgeFacts` 协议声明了 `get_registered_crops`，且假实现与协议结构一致（用 `typing.get_type_hints` 或 `inspect.signature` 断言返回类型含 `tuple[str, ...]`）。
- 该模块内无 `app.agent` / `app.services` 导入（L1）。
- `ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0。

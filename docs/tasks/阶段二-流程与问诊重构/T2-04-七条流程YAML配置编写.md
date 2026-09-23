# T2-04 七条流程 YAML 配置编写

## 这一项要做什么

在 `app/process_config/seeds/` 下写出七份流程配置文件：`rice.yaml`、`tea.yaml`、`rapeseed.yaml`、`chili.yaml`、`weed.yaml`、`pig.yaml`、`general.yaml`。每份配置覆盖技术方案 7.4 差异点清单里的全部字段，包括 `required_slots`、`redlines`、`terminology`、`timing_rules`、`output_template`、`calendar_source`、`followup_policy`、`offline_questions`、`panel_enabled`、`object_aliases`、`confusable_items`、`banned_topics`。 这七份 YAML 是"新增第九条流程不改编排代码"这条判断标准的实物。所有流程差异只能落在配置里，若某项行为无法用现有字段表达，说明 T2-02 的模型缺字段，应回到 T2-02 补模型而不是在代码里加 `if process_id == ...`。农技专业内容（药剂名、剂量、间隔期、决定性特征）需农技人员确认后定稿，执行者只负责按给定数据录入与建表。

## 前置依赖

- 依赖 T2-03：必须已提供 `load_process_config` 与 `_check_integrity` 的全部校验规则；依赖 T2-02：字段名与类型以 `ProcessConfig` 模型为准
- 外部条件：农技专业内容、红线措辞、油菜流程排期（A7）三处需人工确认，确认前用占位值并标注 `# TODO(待确认)`；本项不依赖数据库；配置落库在 T2-07

## 参考章节

- 技术方案 第7章 7.4 七条流程的子图（流程间差异点清单，共十项）；技术方案 第9章 9.1 流程配置模型（字段定义）
- 技术方案 第8章 8.4 流程红线配置化（茶流程红线 YAML 原样参考）；技术方案 第6章 6.4 受限并行会诊（茶、辣椒、杂草三条流程启用）
- 技术方案 第9章 9.5 知识库建设排期（油菜红线处理与一期五条流程侧重）；附录 B：流程配置样例（茶树配置骨架）
- 技术方案 第11章 回访（水稻按生育期窗口，辣椒药后 3 至 7 天，杂草药后 7 至 15 天）

## 改动范围

| 类型 | 文件路径 | 说明 |
| --- | --- | --- |
| 新增 | `app/process_config/seeds/tea.yaml` | 茶流程完整配置 |
| 新增 | `app/process_config/seeds/chili.yaml` | 辣椒流程完整配置 |
| 新增 | `app/process_config/seeds/rice.yaml` | 水稻流程配置 |
| 新增 | `app/process_config/seeds/rapeseed.yaml` | 油菜流程配置，红线含 RAPE-01 |
| 新增 | `app/process_config/seeds/weed.yaml` | 杂草防治流程配置，含药害鉴定分支回访 |
| 新增 | `app/process_config/seeds/pig.yaml` | 养猪流程配置，含法定报告红线与识别信号槽位 |
| 新增 | `app/process_config/seeds/general.yaml` | 通用农业兜底配置 |
| 新增 | `tests/process_config/test_seeds.py` | 断言七份配置全部可加载且关键字段非空 |
| 删除 | `app/process_config/seeds/.gitkeep` | T2-03 的占位文件 |

## 实现要点

### 公共约定

七份 YAML 的顶层键必须与 `ProcessConfig` 字段名逐字相同，不允许下划线变体或缺省键（`extra="forbid"` 会直接报错）。需要农户判断的字段（`display_label`、`options`、`ask`）用口语，给模型与检索用的字段（`label`、`internal_terms`、`decisive_features`）用植保口径书面语。`redlines` 的 `rule` 引用闸门规则 id，取值来自技术方案 8.2 的规则清单。

### 茶流程完整骨架 tea.yaml

```yaml
process_id: tea
version: 1
display_name: 茶树
role_definition: 茶园管理顾问，熟悉茶树病虫害、采摘节奏与安全间隔期约束
object_aliases: [茶, 茶树, 茶叶, 茶园, 普洱, 龙井, 铁观音, 白茶, 乌龙]
required_slots:
  - {key: tea_symptom_part, label: 受害部位, display_label: 茶树的哪个部位出问题了,
     group: 病虫情, required: true, owner: interviewer,
     options: [嫩芽, 叶片, 枝干, 根部], ask_order: 10}
  - {key: tea_symptom_pattern, label: 症状形态, display_label: 是什么样子的,
     group: 病虫情, required: true, owner: interviewer,
     options: [有虫, 有斑点, 发黄, 枯萎, 卷曲], ask_order: 20}
  - {key: tea_symptom_scope, label: 发生范围, display_label: 大概有多少茶树这样,
     group: 病虫情, required: true, owner: interviewer,
     options: [几棵, 一小片, 大片], ask_order: 30}
  - {key: tea_last_pesticide, label: 最近用药, display_label: 最近一次打药是什么时候,
     group: 管理, required: true, owner: interviewer, source_hint: [profile], ask_order: 40}
  - {key: tea_last_pick, label: 上次采摘日, display_label: 上次采摘是哪天,
     group: 管理, required: true, owner: interviewer, source_hint: [profile], ask_order: 50}
  - {key: tea_fertilize, label: 施肥情况, display_label: 最近施过什么肥,
     group: 肥情, required: false, owner: interviewer, source_hint: [profile], ask_order: 60}
terminology:
  internal_terms:
    叶发病率: 以叶片为单位统计的病叶数占调查总叶数的比例
    安全间隔期: 最后一次施药至茶叶可采摘的最短间隔天数
  colloquial_map:
    叶发病率: 有多少叶子出了问题
    生育期: 茶树长到哪个阶段
    安全间隔期: 打完药要等多少天才能采
  display_metrics: [叶发病率, 安全间隔期]
timing_rules:
  - {id: tea_pick_window, when: {season: spring_pick}, prompt: 春茶采摘期临近，注意采摘前安全间隔期}
  - {id: tea_rain_alert, when: {weather: continuous_rain}, prompt: 连续降雨，注意茶园排水与病害预防}
redlines:
  - {id: TEA-01, desc: 不得推荐未在茶树上登记的农药, rule: R-REGISTERED-CROP, severity: block}
  - id: TEA-02
    desc: 不得触碰禁限用品种
    rule: R-REDLINE
    severity: block
    data: {forbidden: [乙酰甲胺磷, 丁硫克百威, 乐果, 氰戊菊酯], reference: NY/T 4803 附录B}
  - {id: TEA-03, desc: 不得开出未核算安全间隔期与每季最多使用次数的方案,
     rule: R-INTERVAL,R-MAX-USE, severity: block}
  - {id: TEA-04, desc: 不得开出与采摘节奏冲突的方案, rule: R-INTERVAL, severity: block}
  - {id: TEA-05, desc: 出口茶园不得忽略输入国标准, rule: R-REGION, severity: degrade}
output_template:
  section_order: [现状判断, 处置建议, 用药安全提示, 回访安排]
  required_sections: [安全间隔期, 与下一轮采摘的时间关系]
  max_chars: 400
calendar_source: tea.picking_round
followup_policy:
  triggers:
    - {after_action: 用药, after_days: 7, ask: 药后效果如何, kind: effect_check}
    - {after_action: 修剪, after_days: 14, ask: 新芽长势如何, kind: effect_check}
  window_based: true
offline_questions:
  - {slot_key: tea_shade_level, text: 茶园有没有遮阴,
     options: [全遮阴, 半遮阴, 无遮阴], answer_type: single}
panel_enabled: true
confusable_items:
  - candidates: [侵染性病害, 生理性病害]
    decisive_features:
      侵染性病害: [病斑边界清晰并有扩展趋势, 相邻植株出现同向传播, 潮湿处可见霉层或菌脓]
      生理性病害: [同批次同管理茶园同时发黄, 症状沿根系或肥料接触带分布, 无霉层无菌脓]
    must_exclude: true
banned_topics: [发布病虫害预报, 提前发布采摘期预测]
```

`TEA-03` 与 `TEA-04` 都引用 `R-INTERVAL` 但语义不同：前者要求字段存在，后者要求与采摘计划不冲突，闸门侧分别实现为两条判定分支，配置侧保留两条以便逐条追溯。

### 辣椒流程完整骨架 chili.yaml

```yaml
process_id: chili
version: 1
display_name: 辣椒
role_definition: 辣椒种植顾问，擅长土传性枯萎类病害与青枯病的鉴别
object_aliases: [辣椒, 朝天椒, 线椒, 菜椒, 青椒, 干椒]
required_slots:
  - {key: chili_symptom_pattern, label: 症状形态, display_label: 辣椒是什么样子,
     group: 病虫情, required: true, owner: interviewer,
     options: [萎蔫, 有斑点, 发黄, 烂根, 有虫], ask_order: 10}
  - {key: chili_symptom_speed, label: 萎蔫速度, display_label: 是一天里就蔫下去还是慢慢蔫的,
     group: 病虫情, required: true, owner: interviewer,
     options: [半天内急速萎蔫, 几天逐渐萎蔫, 说不清], ask_order: 20}
  - {key: chili_vascular_bundle, label: 维管束表现, display_label: 把茎切断看里面，有没有发黄变褐,
     group: 病虫情, required: true, owner: interviewer,
     options: [维管束黄褐色, 未见变色, 没有切开看过], ask_order: 30}
  - {key: chili_exudate, label: 挤压切口渗出物, display_label: 挤一下切口，有没有乳白色液体冒出来,
     group: 病虫情, required: true, owner: interviewer,
     options: [有乳白色菌脓, 没有, 说不清], ask_order: 40}
  - {key: chili_soil_type, label: 土壤类型, display_label: 地里是什么土,
     group: 墒情, required: false, owner: interviewer, source_hint: [profile],
     options: [黏土, 沙壤土, 壤土], ask_order: 50}
  - {key: chili_greenhouse_humidity, label: 棚内湿度, display_label: 棚里潮不潮,
     group: 墒情, required: false, owner: interviewer, options: [很潮, 一般, 偏干], ask_order: 60}
terminology:
  internal_terms:
    青枯病: 由青枯雷尔氏菌引起的细菌性维管束病害
    枯萎病: 由镰刀菌引起的真菌性土传病害
    维管束: 茎内输导组织，纵向切开后可见
  colloquial_map:
    维管束: 茎里面那些纹路
    菌脓: 挤出来的白色浆液
    土传病害: 从土里传染的病
  display_metrics: [株发病率, 维管束变色率]
timing_rules:
  - {id: chili_rain_alert, when: {weather: continuous_rain},
     prompt: 连续降雨，注意排水防涝，土传病害高发期}
  - {id: chili_soil_temp, when: {soil_temp_above: 25}, prompt: 土温偏高，青枯病风险上升}
redlines:
  - {id: CHILI-01, desc: 不得在青枯病与枯萎病鉴别未完成时给出单一用药方案,
     rule: R-REDLINE, severity: block}
  - {id: CHILI-02, desc: 必须给出土壤层面处置措施, rule: R-REDLINE, severity: block}
  - {id: CHILI-03, desc: 不得推荐未在辣椒上登记的农药, rule: R-REGISTERED-CROP, severity: block}
  - {id: CHILI-04, desc: 不得建议更高剂量试图解决土传病害, rule: R-DOSAGE, severity: block}
output_template:
  section_order: [现状判断, 鉴别说明, 处置建议, 用药安全提示, 回访安排]
  required_sections: [鉴别说明, 土壤层面处置]
  max_chars: 400
calendar_source: chili.growth_stage
followup_policy:
  triggers:
    - {after_action: 用药, after_days: 3, ask: 三天后叶片恢复情况, kind: effect_check}
    - {after_action: 用药, after_days: 7, ask: 病情是否继续扩展, kind: effect_check}
  window_based: false
offline_questions:
  - {slot_key: chili_greenhouse_humidity, text: 棚内湿度大概多少,
     options: [很潮, 一般, 偏干], answer_type: single}
panel_enabled: true
confusable_items:
  - candidates: [青枯病, 枯萎病]
    decisive_features:
      青枯病: [叶片保绿而急速萎蔫, 维管束黄褐色, 挤压切口渗出乳白色菌脓]
      枯萎病: [无菌脓, 维管束变色伴蓝白色霉状物]
    must_exclude: true
banned_topics: [发布病虫害预报]
```

`chili_exudate` 与 `chili_vascular_bundle` 是辣椒流程的关键鉴别槽位，来自 `confusable_items` 里的决定性特征。技术方案 9.1 指出一份配置驱动三处行为，因此这两个槽位必须同时出现在 `required_slots` 与 `decisive_features` 中，且措辞要能让农户在家里做（"切开看"比"镜检"可行）。

### 其余五条流程的差异要点

| 流程 | `panel_enabled` | `calendar_source` | 专属红线要点 | `followup_policy` |
| --- | --- | --- | --- | --- |
| rice | false | `rice.growth_stage` | 抗药性暂停名单；共养区禁忌 | 按生育期窗口 |
| rapeseed | false | `rapeseed.growth_stage` | RAPE-01 禁止输出药剂推荐 | 按生育期窗口 |
| weed | true | null | 后茬长残留限制；不得混淆草相推荐药剂 | 药后 7 天查药效、15 天查后茬 |
| pig | false | null | 不得替代法定报告义务；禁用兽药清单 | 按法定时限链 |
| general | false | null | 不得输出药剂条目与专属阈值 | 由处方卡给出观察时点 |

杂草流程的 `confusable_items` 以候选词组形式列出稗草、稻稗、千金子、菵草、看麦娘、节节麦、雀麦，`decisive_features` 逐种给出叶形、叶舌、叶耳等鉴别特征。药害鉴定分支不写在 `followup_policy` 里，由 T2-21 按 `branch == "herbicide_damage"` 直接构造 T+0 回访，内容为现场固定动作清单。 养猪流程的 `panel_enabled` 为 false，但 `required_slots` 中必须包含识别信号类槽位（死亡数量、是否突然死亡、是否多处栏舍同时出现），供 T2-22 的报告守卫匹配。触发条件极低，因此识别信号槽位要拆细，不要合并成"有无异常"一个槽位。 通用兜底流程的 `required_slots` 只保留最基础的四到五项（作物名、症状部位、症状形态、发生范围、近期用药），`confusable_items` 为空，`redlines` 至少含一条禁止输出药剂推荐的规则，`banned_topics` 含发布预报类话题。技术方案 9.2 明确通用流程无专属阈值、无药剂条目，处方只能给观察与求助建议。

## 验收标准

- `python -c "from app.process_config.loader import load_all_configs; c=load_all_configs(); print(sorted(p.value for p in c))"` 输出恰好七个流程标识；`pytest tests/process_config/test_seeds.py` 全部通过，含断言：每份配置 `redlines` 非空、`output_template.max_chars` 在 200 至 500 之间、槽位 `owner` 均在已实现集合内
- 断言 `panel_enabled` 为 True 的流程恰好三条：`tea`、`chili`、`weed`；单独加载 `chili`，断言 `{s.key for s in config.required_slots}` 含 `chili_exudate` 与 `chili_vascular_bundle`
- 单独加载 `rapeseed`，断言红线中存在 `desc` 含"药剂推荐"的条目；`python -c "from app.process_config.loader import load_process_config; from app.agent.state import ProcessId; print(load_process_config(ProcessId.GENERAL).calendar_source)"` 输出 `None`
- 断言 `tea` 与 `chili` 的 `confusable_items[0].must_exclude` 均为 True；`ruff check`、`ruff format --check`、`mypy app` 三条命令返回 0

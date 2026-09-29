# 知识条目数据文件（一期）

本目录存放一期五条流程的知识条目数据文件，每行一个 JSON 对象（JSON Lines）。构建入口为
`app.services.knowledge_index_service.build_knowledge_index`，校验模型为
`app.knowledge.schema.KnowledgeSeedItem`，加载器为 `app.knowledge.indexer.load_seed_dir`。

选 jsonl 而非 yaml：单流程条目数量大，逐行便于 diff 与人工审阅，一处写坏也只影响一行，
不会导致整份文件不可解析。

## 文件清单

| 文件 | 流程 | 建设重点 |
| --- | --- | --- |
| `rice.jsonl` | 水稻 | 病虫害谱、量化指标、抗药性暂停名单、共养区禁忌 |
| `tea.jsonl` | 茶 | 禁限用清单、登记药剂与安全间隔期、叶发病率指标 |
| `chili.jsonl` | 辣椒 | 易混淆项判据、土壤层面处置、登记标签范围 |
| `weed.jsonl` | 杂草防治 | 草相鉴别特征、经济阈值、抗药性轮换、后茬长残留限制 |
| `pig.jsonl` | 养猪 | 强制免疫程序、法定时限链、禁用兽药、识别信号 |

## 字段说明

字段与 `KnowledgeSeedItem` 一一对应（`extra="forbid"`，字段名拼错会在载入期报错）。

| 字段 | 必填条件 | 说明 |
| --- | --- | --- |
| `knowledge_id` | 总是 | 具业务含义的字符串主键，形如 `K-RICE-0101` |
| `process_id` | 总是 | 一条流程：`rice` / `tea` / `chili` / `weed` / `pig` |
| `kind` | 总是 | `pest` / `disease` / `pesticide` / `cultivation` / `threshold` |
| `title` | 总是 | 标题，参与向量化与标题命中 |
| `content` | 总是 | 正文，给症状、阈值、判据或措施 |
| `pesticide_name` | `kind=pesticide` 时必填 | 药剂名，白名单/冲突规则精确查 |
| `registered_crops` | `kind=pesticide` 时建议 | 登记作物；茶流程必须含"茶"，辣椒流程必须含"辣椒" |
| `dosage_min` / `dosage_max` / `dosage_unit` | `kind=pesticide` 时必填 | 剂量三元组，`R-DOSAGE` 的比对基准 |
| `safety_interval_days` | `kind=pesticide` 时必填 | 安全间隔期，`R-INTERVAL` 的比对基准 |
| `max_uses_per_season` | 可选 | 每季最多使用次数，`R-MAX-USE` 的比对基准 |
| `forbidden_combinations` | 可选 | 禁忌搭配；共养区禁忌与后茬长残留限制写在这里 |
| `region_scope` | 可选 | 行政区编码前缀，`null` 表示全国通用 |
| `regions` | 可选 | 地区挂载点列表；为空且 `region_scope` 为空时自动挂全国 `000000` |
| `effective_date` / `expire_date` | 可选 | 生效期/失效期，为空表示不设该端边界 |
| `status` | 可选（默认 `draft`） | `draft` / `approved` / `suspended` / `deprecated` |
| `status_reason` | `status=suspended` 时必填 | 暂停理由；与表上 CHECK 约束一致 |
| `source_ref` | 建议 | 权威来源引用，追溯与人工复核的依据 |

## 状态语义

- `approved`：已核准，唯一可被检索使用的状态。
- `draft`：待核准，不参与检索。**未确认的数值一律置 `draft`**，确认后在 T2-54 走核准流转。
- `suspended`：临时停用。建模为状态而非删条目，让历史问诊能回答"当时为什么没推荐这味药"。
  水稻的抗药性暂停名单、茶的禁限用清单、养猪的禁用兽药都用它；恢复只需改回 `approved`。
- `deprecated`：已废弃，保留用于历史处方追溯，不参与新检索。

## 人工确认项清单

以下内容需农技人员确认后，才能从 `draft` 改为 `approved`（见 T2-52「人工确认项」）：

| 流程 | 需确认项 |
| --- | --- |
| 水稻 | 抗药性暂停名单（随时间变化）、共养区禁忌（与本地养殖模式相关） |
| 茶 | 禁限用清单的现行版本、登记药剂与其间隔期 |
| 辣椒 | 易混淆项的判据措辞、土壤处置措施的适用范围 |
| 杂草 | 后茬长残留限制的作物组合与时间窗口 |
| 养猪 | 免疫程序与法定时限的具体数值 |

未确认条目的 `source_ref` 以 `TODO(待确认)` 结尾，便于检索与统计。

## 录入约定

1. 药剂类条目（`kind=pesticide`）必须给出 `pesticide_name`、完整剂量区间、`dosage_unit` 与
   `safety_interval_days`，否则载入即失败。禁限用/禁用类药剂剂量无实际意义，填 `0` 并在
   `content` 说明禁用，靠 `status=suspended` 而非剂量表达"不可用"。
2. `status=suspended` 必须给出 `status_reason`。
3. 易混淆项条目的措辞要与流程配置的 `confusable_items.decisive_features` 对齐，且判据须是
   农户在家可操作的鉴别点（如"挤压切口看菌脓""切开看维管束"），不含只能镜检得到的信息。
4. 后茬长残留限制条目：`forbidden_combinations` 列出禁用组合，`content` 给时间窗口。
5. 共养区禁忌按养殖对象与药剂逐条成条，`content` 写明"稻渔/稻虾共养区禁用 X"。

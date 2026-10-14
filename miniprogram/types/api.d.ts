/** 全局接口类型定义（与后端改造后契约对齐，字段一律小驼峰 camelCase） */
export interface ApiResponse<T> {
  code: number
  message: string
  data: T
}

/* ---------- 认证 ---------- */
export interface LoginResult {
  token: string
  userId: string
  isNewUser: boolean
}

/* ---------- 农户档案 ---------- */
export interface CropItem {
  cropName: string
  /** 流程类型（七选一，取 ProcessId 值）；'other' / 空表示自由输入作物名 */
  processType?: string
  cropVariety?: string | null
  plantMu?: number | null
  /** 播种 / 起始时间（YYYY-MM-DD）；农事历推算依赖它 */
  sowingDate?: string | null
  growthStage?: string
}

export interface FarmProfile {
  realName: string
  age?: number | null
  gender: number
  phone: string
  experienceYear: number
  province: string
  city: string
  district: string
  address: string
  totalMu: number
  cultivationType: number
  waterSource: string
  remark: string
  crops: CropItem[]
}

export interface ProfileResult {
  hasProfile: boolean
  profile: FarmProfile | null
}

/* ---------- 地点 ---------- */
export interface ReverseGeocodeResult {
  province: string
  city: string
  district: string
  fullAddress: string
}

export interface SetLocationParams {
  locationName: string
  latitude?: number | null
  longitude?: number | null
  source?: string
}

export interface SetLocationResult {
  locationName: string
  weatherSummary: string
}

/* ---------- 提示快照（broadcast，改造后契约） ---------- */
/** 展示状态：today 当日 / yesterday 昨日 / stale 更早（弱化） */
export type DisplayStatus = 'today' | 'yesterday' | 'stale'
/** 强调级别：urgent 条目 → high，其余 → normal */
export type PromptEmphasis = 'high' | 'normal'

/** 单条提示（五段式：呼告 / 事实 / 影响 / 行动） */
export interface PromptItem {
  itemId: string
  sourceKind: string
  severity: string
  callout: string
  fact: string
  impact: string
  action: string
  executed: boolean
  ruleId: string
  emphasis: PromptEmphasis
}

/** 当日提示快照 */
export interface SnapshotView {
  items: PromptItem[]
  generatedAt: string
  displayStatus: DisplayStatus
  daysAgo: number
  snapshotId: string
  farmEntryId: string | null
  revision: number
  generatedLabel: string
  envSource: string
  envNote: string
}

/** 更新提示（POST）返回：快照 + 更新结果与提示语 */
export interface SnapshotResponse {
  snapshot: SnapshotView | null
  updateCompleted: boolean
  updateNotice: string | null
}

/** 读取当日提示（GET）返回 */
export interface SnapshotTodayResponse {
  snapshot: SnapshotView | null
}

/** 勾选/取消某条提示的执行状态返回 */
export interface ExecuteResult {
  id: string
  executed: boolean
  executedAt: string | null
}

/* ---------- 环境监测 ---------- */
/** 环境数据来源：本地传感器 / 本地陈旧 / 公共天气 / 暂无 */
export type EnvSource = 'sensor' | 'sensor_stale' | 'weather' | 'none'

/** 来源与新鲜度标注（后端 SourceBadge） */
export interface SourceBadge {
  source: EnvSource
  label: string
  /** 仅 sensor_stale 非空，如"数据为 3 小时前" */
  freshnessNote: string | null
  /** 仅天气来源时给出，标注命中的地区级 */
  regionHitLevel: string | null
}

export interface EnvironmentSummary {
  /** 无读数时为 null（后端返回 null，展示需占位） */
  temperature: number | null
  humidity: number | null
  soilStatus: string
  soilDetail: string
  lightStatus: string
  lightDetail: string
  lastUpdateAt: string
  sourceBadge?: SourceBadge | null
}

export interface MetricItem {
  id: string
  label: string
  value: string
  unit: string
}

export interface KVItem {
  label: string
  value: string
}

/** 设备列表项（与环境看板、设备管理列表共用，后端 DeviceListItem） */
export interface DeviceListItem {
  id: string
  deviceSn: string
  deviceName: string | null
  farmEntryId: string | null
  /** 在线状态：online / offline / fault */
  onlineState: 'online' | 'offline' | 'fault'
  lastReportAt: string | null
  /** 仅在非在线时非空，如"已离线 3 小时" */
  silenceNote: string | null
  mappedFields: number
  boundAt: string
}

export interface EnvironmentDetail {
  metrics: MetricItem[]
  airData: KVItem[]
  soilData: KVItem[]
  weatherData: KVItem[]
  devices: DeviceListItem[]
  lastUpdateAt: string
  sourceBadge?: SourceBadge | null
}

/** 看板单个指标行（后端 BoardMetric）：status 反映阈值、quality 反映读数可信度 */
export interface BoardMetric {
  metric: string
  label: string
  value: number | null
  unit: string
  status: string
  quality: string
  observedAt: string | null
}

/** 环境看板出参（后端 EnvironmentBoard） */
export interface EnvironmentBoard {
  metrics: BoardMetric[]
  sourceBadge: SourceBadge
  devices: DeviceListItem[]
  lastUpdateAt: string | null
}

/* ---------- 设备接入（三步向导，与后端 device schema 对齐） ---------- */
/** 绑定入参：deviceSn 必填，可选关联地块 */
export interface BindRequest {
  deviceSn: string
  farmEntryId?: string | null
}

/** 绑定结果：下一步引导 + 一次性下发的密钥与上报地址 */
export interface BindResponse {
  deviceId: string
  vendorTemplateMatched: boolean
  templateId?: string | null
  /** 命中模板走确认页（confirm_template），未命中走粘贴页（provide_sample） */
  nextStep: string
  reportUrl?: string | null
  /** 明文设备密钥，仅绑定时下发一次；幂等分支为 null */
  deviceSecret?: string | null
}

/** 解析出的单个字段（供农户确认或改选） */
export interface DetectedField {
  rawField: string
  sampleValue: string
  inferredMetric?: string | null
  candidates: string[]
  confidence: number
}

/** 样例报文解析结果 */
export interface ParseSampleResponse {
  parsed: boolean
  fields: DetectedField[]
  inferError?: string | null
}

/** 单条字段映射（保存时的完整视图，整体替换语义） */
export interface MappingItem {
  rawField: string
  standardMetric: string
  unit?: string
  scaleFactor?: number
  offsetValue?: number
  dataType?: string
  sampleRaw?: string | null
}

export interface MappingSaveRequest {
  deviceId: string
  mappings: MappingItem[]
}

/** 保存后预览的一行：原始字段 → 换算后的标准读数 */
export interface MappingPreview {
  rawField: string
  sampleRaw: string
  standardMetric: string
  value: number
  unit: string
}

export interface MappingSaveResponse {
  deviceId: string
  saved: number
  skipped: string[]
  warnings: string[]
  preview: MappingPreview[]
}

export interface DeviceListResponse {
  devices: DeviceListItem[]
}

/* ---------- AI 咨询（图驱动问诊，改造后契约） ---------- */
/** 问诊阶段枚举 */
export type ConsultStage =
  | 'intake'
  | 'triage'
  | 'observe'
  | 'interview'
  | 'verify'
  | 'judge'
  | 'prescribe'
  | 'gate'
  | 'followup'
  | 'handoff'
  | 'blocked'

/** 槽位作答：status 区分已答（filled）与农户明确答不出（unknown） */
export interface SlotAnswer {
  slotKey: string
  value: string
  status?: 'filled' | 'unknown'
}

export interface ChatParams {
  sessionId?: string | null
  farmEntryId?: string | null
  message?: string | null
  imageUrl?: string | null
  voiceUrl?: string | null
  /** 流程提示：替代旧的 cropTag 作物标签 */
  processHint?: string | null
  answers?: SlotAnswer[] | null
}

export interface ProgressInfo {
  current: number
  total: number
}

export type AnswerType = 'single' | 'multi' | 'text' | 'photo' | 'number'

/** 追问问题 */
export interface QuestionView {
  slotKey: string
  text: string
  why: string
  options: string[] | null
  answerType: AnswerType
  allowUnknown: boolean
}

export interface PrescriptionActionView {
  order: number
  action: string
  timing: string
  method: string
  cautions: string[]
  stopConditions: string[]
}

export interface PesticideItemView {
  name: string
  dosageMin?: number | null
  dosageMax?: number | null
  dosageUnit?: string | null
  safetyIntervalDays?: number | null
  maxUsesPerSeason?: number | null
  registeredCrops: string[]
  forbiddenCombinations: string[]
  sourceNote?: string | null
}

export type FollowupKind =
  | 'effect_check'
  | 'risk_check'
  | 'preserve_scene'
  | 'observe'
  | 'compliance_check'

export interface FollowupTaskView {
  dueOffsetDays: number
  kind: FollowupKind
  ask: string | null
}

export interface FollowupPlanView {
  tasks: FollowupTaskView[]
  note: string | null
}

/** 处方结论 */
export interface PrescriptionView {
  /**
   * 处方卡主键，用于「存入台账」（POST /ai/prescription/{card_id}/ledger）。
   * 后端当前 ChatResponse 尚未下发该字段，前端按可选处理：缺失时不渲染存入台账按钮。
   */
  cardId?: string | null
  diagnosisSummary: string
  actions: PrescriptionActionView[]
  safetyIntervalDays?: number | null
  maxUsesPerSeason?: number | null
  pesticideItems: PesticideItemView[]
  followupPlan: FollowupPlanView
  requiresTechStation: boolean
  detailTerms: Record<string, string>
}

export interface ContactInfoView {
  kind: 'vet_station' | 'agri_station' | 'authority' | 'hotline'
  name: string
  phone?: string | null
  regionCode?: string | null
  note?: string | null
}

/** 转人工上下文包 */
export interface HandoffView {
  reason:
    | 'out_of_scope'
    | 'safety_degrade'
    | 'safety_block'
    | 'not_converged'
    | 'mandatory_report'
  summary: string
  confirmedInfo: string[]
  observationPoints: string[]
  excludedItems: string[]
  contacts: ContactInfoView[]
  speakable: boolean
}

/** 统一"问诊结果"类别：出方 / 转人工 / 安全拦截 / 法定报告 */
export type ConsultResultKind = 'prescription' | 'handoff' | 'blocked' | 'report'

/**
 * 统一"问诊结果"。所有终态路径都产出（含转人工），前端以它作为唯一渲染源。
 * 出方路径用 actions / pesticideItems / followupPlan；转人工路径用
 * conclusion / confirmedInfo / observationPoints / nextStep / contacts。
 */
export interface ConsultResultView {
  kind: ConsultResultKind
  title: string
  conclusion: string
  confidenceLabel?: string | null
  basis: string[]
  confirmedInfo: string[]
  observationPoints: string[]
  excludedItems: string[]
  actions: PrescriptionActionView[]
  pesticideItems: PesticideItemView[]
  followupPlan?: FollowupPlanView | null
  /** 有序"该做的事"：法定报告情形的强制动作，顺序即展示顺序 */
  steps: string[]
  nextStep?: string | null
  contacts: ContactInfoView[]
  requiresTechStation: boolean
  speakable: boolean
  /** 处方卡主键，用于「存入台账」；转人工路径为 null */
  cardId?: string | null
}

export interface QuotaPromptView {
  scene: string
  text: string
  granted: boolean
}

/** 问诊响应：questions / prescription / handoff 三者最多出现一个 */
export interface ChatResponse {
  sessionId: string
  stage: ConsultStage
  stageLabel: string
  progress: ProgressInfo
  reply: string
  questions?: QuestionView[] | null
  prescription?: PrescriptionView | null
  environmentNote?: string | null
  handoff?: HandoffView | null
  quotaPrompt?: QuotaPromptView | null
  /** 统一"问诊结果"：终态非空，追问轮为空。前端以此渲染结果卡 */
  result?: ConsultResultView | null
  degraded: boolean
}

/** 历史会话条目（不再携带 messages 全量） */
export interface HistoryItem {
  id: string
  title: string
  preview: string
  messageCount: number
  updatedAt: number
}

export interface HistoryResult {
  list: HistoryItem[]
  total: number
}

/** 会话详情里的一条消息 */
export interface ConversationMessage {
  role: 'user' | 'ai'
  content: string
  imageUrl?: string | null
}

/** 单个会话的完整消息（回看历史对话用） */
export interface ConversationDetail {
  sessionId: string
  title: string
  messages: ConversationMessage[]
  /** 该会话最新一张处方卡；转人工会话为 null */
  prescription?: PrescriptionView | null
  /** 统一"问诊结果"快照；转人工会话也有值，使历史回看与实时一致 */
  result?: ConsultResultView | null
  /** 处方卡是否已存入台账 */
  ledgerSaved?: boolean
}

/* ---------- 用户 ---------- */
export interface UploadResult {
  url: string
}

/* ---------- 农事台账 ---------- */
/** 处方卡存入台账后的回执（后端 LedgerSaveResponse） */
export interface LedgerSaveResponse {
  entryId: string
  entryDate: string
  processId: string | null
  title: string | null
  actions: Record<string, any>[]
  pesticides: Record<string, any>[]
  safetyIntervalDays: number | null
  followupNeeded: boolean
  ledgerSaved: boolean
}

/* ---------- 订阅授权 ---------- */
/** 推送额度视图：weakened 为真时前端只做弱提示（不弹强索要） */
export interface QuotaView {
  remaining: number
  grantedTotal: number
  consumedTotal: number
  weakened: boolean
}

export interface GrantRequest {
  granted: boolean
  scene?: string | null
}

export type GrantResponse = QuotaView

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
  plantMu?: number | null
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

/* ---------- 用户 ---------- */
export interface UploadResult {
  url: string
}

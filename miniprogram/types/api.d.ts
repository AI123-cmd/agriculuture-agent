/** 全局接口类型定义 */
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

/* ---------- 农事播报 ---------- */
export interface BroadcastItem {
  id: string
  time: string
  title: string
  content: string
  done: boolean
  type: string
}

export interface BroadcastTodayResult {
  date: string
  items: BroadcastItem[]
}

/* ---------- 环境监测 ---------- */
export interface EnvironmentSummary {
  temperature: number
  humidity: number
  soilStatus: string
  soilDetail: string
  lightStatus: string
  lightDetail: string
  lastUpdateAt: string
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

export interface DeviceItem {
  id: string
  name: string
  type: string
  status: number
  lastOnline: string
}

export interface EnvironmentDetail {
  metrics: MetricItem[]
  airData: KVItem[]
  soilData: KVItem[]
  weatherData: KVItem[]
  devices: DeviceItem[]
  lastUpdateAt: string
}

/* ---------- AI 对话 ---------- */
export interface ChatParams {
  message: string
  sessionId: string
  cropTag?: string | null
  imageUrl?: string | null
}

export interface ChatResult {
  sessionId: string
  reply: string
  relatedSuggestions: string[]
  tagsAdded: string[]
}

export interface HistoryMessageItem {
  id: number
  role: string
  content: string
}

export interface HistoryItem {
  id: string
  title: string
  preview: string
  updatedAt: number
  messages: HistoryMessageItem[]
}

export interface HistoryResult {
  list: HistoryItem[]
  total: number
}

/* ---------- 用户 ---------- */
export interface UploadResult {
  url: string
}
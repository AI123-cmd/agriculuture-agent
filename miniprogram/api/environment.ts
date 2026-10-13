import { request } from '../utils/request'
import type { EnvironmentBoard, EnvironmentDetail, EnvironmentSummary } from '../types/api'

/** 环境看板：指标行（含阈值状态与读数质量）+ 来源标注 + 设备列表 */
export function getBoard(plotId?: string | null) {
  const q = plotId ? `?plot_id=${encodeURIComponent(plotId)}` : ''
  return request<EnvironmentBoard>({ url: `/api/environment/board${q}` })
}

export function getSummary() {
  return request<EnvironmentSummary>({ url: '/api/environment/summary' })
}

export function getDetail() {
  return request<EnvironmentDetail>({ url: '/api/environment/detail' })
}
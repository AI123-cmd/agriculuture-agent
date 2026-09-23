import { request } from '../utils/request'
import type { EnvironmentDetail, EnvironmentSummary } from '../types/api'

export function getSummary() {
  return request<EnvironmentSummary>({ url: '/api/environment/summary' })
}

export function getDetail() {
  return request<EnvironmentDetail>({ url: '/api/environment/detail' })
}
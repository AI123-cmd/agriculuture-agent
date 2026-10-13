import { request } from '../utils/request'
import type { QuotaView, GrantRequest } from '../types/api'

/** 读当前用户推送额度与降级索要判定（weakened 为真时只做弱提示） */
export function getQuota() {
  return request<QuotaView>({ url: '/api/subscription/quota' })
}

/** 回传一次授权结果（同意/拒绝），返回最新额度视图 */
export function grant(payload: GrantRequest) {
  return request<QuotaView>({ url: '/api/subscription/grant', method: 'POST', data: payload })
}

import { request } from '../utils/request'
import type { BroadcastTodayResult } from '../types/api'

export function getToday() {
  return request<BroadcastTodayResult>({ url: '/api/broadcast/today' })
}

export function markDone(itemId: string) {
  return request<void>({ url: '/api/broadcast/mark-done', method: 'POST', data: { itemId } })
}
import { request } from '../utils/request'
import type { ChatParams, ChatResult, HistoryResult } from '../types/api'

export function chat(payload: ChatParams) {
  return request<ChatResult>({ url: '/api/ai/chat', method: 'POST', data: payload })
}

export function history(page = 1, size = 20) {
  return request<HistoryResult>({ url: `/api/ai/history?page=${page}&size=${size}` })
}

export function removeHistory(id: string) {
  return request<void>({ url: `/api/ai/history/${id}`, method: 'DELETE' })
}
import { request } from '../utils/request'
import type { ChatParams, ChatResponse, HistoryResult, ConversationDetail } from '../types/api'

/** 图驱动问诊：单轮对话（携带 answers 时提交槽位作答） */
export function chat(payload: ChatParams) {
  return request<ChatResponse>({ url: '/api/ai/chat', method: 'POST', data: payload })
}

/** 历史会话列表（不含完整消息） */
export function history(page = 1, size = 20) {
  return request<HistoryResult>({ url: `/api/ai/history?page=${page}&size=${size}` })
}

/** 回看单个会话的完整消息（历史列表不返回消息） */
export function conversationDetail(sessionId: string) {
  return request<ConversationDetail>({ url: `/api/ai/consult/${sessionId}` })
}

/** 删除某会话 */
export function removeHistory(sessionId: string) {
  return request<void>({ url: `/api/ai/history/${sessionId}`, method: 'DELETE' })
}

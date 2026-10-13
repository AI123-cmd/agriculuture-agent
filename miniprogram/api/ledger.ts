import { request } from '../utils/request'
import type { LedgerSaveResponse } from '../types/api'

/**
 * 把处方卡存入农事台账（幂等，重复点击返回既有条目）。
 *
 * 注意：后端路由为 POST /api/ai/prescription/{card_id}/ledger，需要处方卡主键。
 * 当前问诊响应（ChatResponse.prescription）尚未下发 cardId，调用方须先确认其存在。
 */
export function savePrescriptionToLedger(cardId: string) {
  return request<LedgerSaveResponse>({
    url: `/api/ai/prescription/${cardId}/ledger`,
    method: 'POST',
  })
}

import { request } from '../utils/request'
import type {
  SnapshotResponse,
  SnapshotTodayResponse,
  ExecuteResult,
} from '../types/api'

/** 生成/更新当日提示快照（点“更新提示”时调用） */
export function postSnapshot(farmEntryId?: string | null) {
  const q = farmEntryId ? `?farmEntryId=${encodeURIComponent(farmEntryId)}` : ''
  return request<SnapshotResponse>({ url: `/api/broadcast/snapshot${q}`, method: 'POST' })
}

/** 读取当日已生成的提示快照 */
export function getSnapshot(farmEntryId?: string | null) {
  const q = farmEntryId ? `?farmEntryId=${encodeURIComponent(farmEntryId)}` : ''
  return request<SnapshotTodayResponse>({ url: `/api/broadcast/snapshot${q}` })
}

/** 勾选/取消某条提示的执行状态（executed 为 true 勾选，false 取消） */
export function executeItem(itemId: string, executed: boolean) {
  return request<ExecuteResult>({
    url: `/api/broadcast/items/${itemId}/execute`,
    method: 'POST',
    data: { executed },
  })
}

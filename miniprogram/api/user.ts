import { request, upload } from '../utils/request'
import type { UploadResult } from '../types/api'

export function uploadAvatar(filePath: string) {
  return upload<UploadResult>({ url: '/api/user/avatar/upload', filePath, name: 'file' })
}

export function cancelAccount() {
  return request<void>({ url: '/api/user/cancel', method: 'DELETE' })
}
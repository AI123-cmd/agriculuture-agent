import { request } from '../utils/request'
import type {
  BindRequest,
  BindResponse,
  ParseSampleResponse,
  MappingSaveRequest,
  MappingSaveResponse,
  DeviceListResponse,
} from '../types/api'

/** 三步接入第一步：绑定设备编号，返回下一步引导与一次性下发的密钥/上报地址 */
export function bindDevice(payload: BindRequest) {
  return request<BindResponse>({ url: '/api/device/bind', method: 'POST', data: payload })
}

/** 三步接入第二步：粘贴样例报文，后端解析并推断字段含义 */
export function parseSample(deviceId: string, payload: string) {
  return request<ParseSampleResponse>({
    url: '/api/device/parse-sample',
    method: 'POST',
    data: { deviceId, payload },
  })
}

/** 三步接入第三步：保存完整字段映射（整体替换语义），返回换算预览 */
export function saveMapping(payload: MappingSaveRequest) {
  return request<MappingSaveResponse>({ url: '/api/device/mapping', method: 'POST', data: payload })
}

/** 设备管理列表：在绑设备的在线状态、最近上报时间与映射条数 */
export function listDevices() {
  return request<DeviceListResponse>({ url: '/api/device/list' })
}

/** 解绑设备（软删除，保留历史读数与映射） */
export function unbindDevice(deviceId: string) {
  return request<{ unbound: boolean }>({
    url: `/api/device/${deviceId}`,
    method: 'DELETE',
  })
}

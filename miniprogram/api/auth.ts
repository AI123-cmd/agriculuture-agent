import { request } from '../utils/request'
import type { LoginResult } from '../types/api'

export function login(payload: { code: string; userInfo?: any }) {
  return request<LoginResult>({ url: '/api/login', method: 'POST', data: payload, auth: false })
}

export function logout() {
  return request<void>({ url: '/api/logout', method: 'POST' })
}
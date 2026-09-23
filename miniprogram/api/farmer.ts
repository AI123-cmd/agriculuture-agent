import { request } from '../utils/request'
import type { FarmProfile, ProfileResult } from '../types/api'

export function getProfile() {
  return request<ProfileResult>({ url: '/api/farmer/profile' })
}

export function saveProfile(payload: FarmProfile) {
  return request<ProfileResult>({ url: '/api/farmer/profile', method: 'POST', data: payload })
}
import { request } from '../utils/request'
import type { ReverseGeocodeResult, SetLocationParams, SetLocationResult } from '../types/api'

export function setLocation(payload: SetLocationParams) {
  return request<SetLocationResult>({ url: '/api/location/set', method: 'POST', data: payload })
}

export function reverseGeocode(latitude: number, longitude: number) {
  return request<ReverseGeocodeResult>({
    url: `/api/location/reverse-geocode?latitude=${latitude}&longitude=${longitude}`,
    auth: false,
  })
}
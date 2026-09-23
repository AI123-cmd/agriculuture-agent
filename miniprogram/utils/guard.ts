import { KEY } from './constants'

/**
 * 登录态 / 建档态守卫。
 * 无登录态 → 登录页；已登录但无档案 → 建档页。返回是否通过。
 */
export function ensureAuth(requireProfile = true): boolean {
  const token = wx.getStorageSync(KEY.TOKEN)
  const hasLogin = wx.getStorageSync(KEY.HAS_LOGIN)
  if (!token && !hasLogin) {
    wx.reLaunch({ url: '/pages/login/login' })
    return false
  }
  if (requireProfile) {
    const profile = wx.getStorageSync(KEY.FARMER_PROFILE)
    if (!profile || !profile.realName) {
      wx.reLaunch({ url: '/pages/profile/farmer-profile' })
      return false
    }
  }
  return true
}
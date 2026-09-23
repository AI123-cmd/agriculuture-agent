import { reverseGeocode, setLocation } from '../api/location'
import { KEY } from './constants'

export interface LocateResult {
  locationName: string
}

/** 本地缓存 + 后端持久化地点，后端失败不阻断本地保存 */
async function persist(name: string, lat?: number, lng?: number, source = 'manual') {
  wx.setStorageSync(KEY.FARM_LOCATION, name)
  try {
    await setLocation({
      locationName: name,
      latitude: lat ?? null,
      longitude: lng ?? null,
      source,
    })
  } catch (e) {
    // 离线/后端异常：保留本地，不打断用户
  }
}

/** 自动定位（三态权限状态机），成功返回地点名 */
export function autoLocate(): Promise<LocateResult> {
  return new Promise((resolve, reject) => {
    wx.showLoading({ title: '定位中...' })
    const fallback = () => {
      wx.hideLoading()
      reject(new Error('定位失败'))
    }

    const doGetLocation = (cbFallback: () => void) => {
      wx.getLocation({
        type: 'gcj02',
        success: async (res) => {
          try {
            const addr = await reverseGeocode(res.latitude, res.longitude)
            const name =
              addr.fullAddress ||
              `定位坐标 ${res.latitude.toFixed(4)},${res.longitude.toFixed(4)}`
            await persist(name, res.latitude, res.longitude, 'gps')
            wx.hideLoading()
            resolve({ locationName: name })
          } catch (e) {
            wx.hideLoading()
            cbFallback()
          }
        },
        fail: (err) => {
          const m = (err && err.errMsg) || ''
          wx.hideLoading()
          if (m.indexOf('auth') >= 0 || m.indexOf('deny') >= 0) {
            wx.showModal({
              title: '需要定位权限',
              content: '请到设置页开启位置权限',
              confirmText: '去设置',
              success: (r) => r.confirm && wx.openSetting({ fail: cbFallback }),
              fail: cbFallback,
            })
          } else if (m.indexOf('system permission') >= 0 || m.indexOf('location service') >= 0) {
            wx.showModal({
              title: 'GPS未开启',
              content: '请在手机系统设置中开启定位服务/GPS后重试',
              showCancel: false,
              success: () => cbFallback(),
            })
          } else {
            cbFallback()
          }
        },
      })
    }

    wx.getSetting({
      success: (setting) => {
        const scope = setting.authSetting['scope.userLocation']

        if (scope === true) {
          doGetLocation(fallback)
          return
        }

        if (scope === false) {
          wx.hideLoading()
          wx.showModal({
            title: '需要定位权限',
            content: '您曾拒绝过定位授权，请到右上角「...」设置中开启位置权限后重试',
            confirmText: '去设置',
            success: (r) => {
              if (r.confirm) {
                wx.openSetting({
                  success: (s) => {
                    if (s.authSetting['scope.userLocation']) {
                      wx.showLoading({ title: '定位中...' })
                      doGetLocation(fallback)
                    } else {
                      fallback()
                    }
                  },
                  fail: fallback,
                })
              } else {
                fallback()
              }
            },
            fail: fallback,
          })
          return
        }

        wx.authorize({
          scope: 'scope.userLocation',
          success: () => doGetLocation(fallback),
          fail: (err) => {
            const m = (err && err.errMsg) || ''
            wx.hideLoading()
            if (m.indexOf('deny') >= 0 || m.indexOf('cancel') >= 0 || m.indexOf('auth') >= 0) {
              wx.showModal({
                title: '未授权定位',
                content: '需要开启位置权限才能自动定位，是否进入设置页开启？',
                confirmText: '去设置',
                success: (r) => {
                  if (r.confirm) {
                    wx.openSetting({
                      success: (s) => {
                        if (s.authSetting['scope.userLocation']) {
                          wx.showLoading({ title: '定位中...' })
                          doGetLocation(fallback)
                        } else {
                          fallback()
                        }
                      },
                      fail: fallback,
                    })
                  } else {
                    fallback()
                  }
                },
                fail: fallback,
              })
            } else {
              fallback()
            }
          },
        })
      },
      fail: fallback,
    })
  })
}

/** 手动输入地点，成功返回地点名 */
export function manualInputLocation(): Promise<LocateResult> {
  return new Promise((resolve, reject) => {
    wx.showModal({
      title: '设置地点',
      placeholderText: '如：湖南省长沙市芙蓉区',
      editable: true,
      success: async (res) => {
        if (res.confirm && res.content) {
          const name = res.content.trim()
          await persist(name)
          resolve({ locationName: name })
        } else {
          reject(new Error('已取消'))
        }
      },
      fail: () => reject(new Error('已取消')),
    })
  })
}
import { BASE_URL } from './config'
import { KEY } from './constants'

interface ApiResponse<T> {
  code: number
  message: string
  data: T
}

interface RequestOptions {
  url: string
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  data?: any
  /** 是否需要鉴权（默认 true） */
  auth?: boolean
}

let handling401 = false

/** 401 统一处理：清 token、防抖、仅跳转一次登录页 */
function handle401() {
  if (handling401) return
  handling401 = true
  wx.removeStorageSync(KEY.TOKEN)
  wx.removeStorageSync(KEY.HAS_LOGIN)
  wx.showToast({ title: '登录已过期，请重新登录', icon: 'none' })
  setTimeout(() => {
    wx.reLaunch({ url: '/pages/login/login' })
    handling401 = false
  }, 800)
}

export function getToken(): string {
  return wx.getStorageSync(KEY.TOKEN) || ''
}

/** 统一 wx.request 封装：注入 token、解析 {code,message,data}、拦截 401 */
export function request<T>(options: RequestOptions): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const header: any = { 'content-type': 'application/json' }
    if (options.auth !== false) {
      const token = getToken()
      if (token) header.Authorization = `Bearer ${token}`
    }

    wx.request({
      url: BASE_URL + options.url,
      method: options.method || 'GET',
      data: options.data,
      header,
      success: (res) => {
        const status = res.statusCode
        const body = res.data as ApiResponse<T>
        if (status >= 200 && status < 300 && body && body.code === 200) {
          resolve(body.data)
          return
        }
        if ((body && body.code === 401) || status === 401) {
          handle401()
          reject(new Error((body && body.message) || '登录已过期'))
          return
        }
        reject(new Error((body && body.message) || '请求失败'))
      },
      fail: (err) => {
        reject(new Error(err.errMsg || '网络异常'))
      },
    })
  })
}

/** 统一 wx.uploadFile 封装（头像等文件上传） */
export function upload<T>(options: {
  url: string
  filePath: string
  name?: string
  formData?: any
}): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const header: any = {}
    const token = getToken()
    if (token) header.Authorization = `Bearer ${token}`

    wx.uploadFile({
      url: BASE_URL + options.url,
      filePath: options.filePath,
      name: options.name || 'file',
      formData: options.formData,
      header,
      success: (res) => {
        if (res.statusCode === 401) {
          handle401()
          reject(new Error('登录已过期'))
          return
        }
        let body: ApiResponse<T>
        try {
          body = JSON.parse(res.data) as ApiResponse<T>
        } catch (e) {
          reject(new Error('响应解析错误'))
          return
        }
        if (body && body.code === 200) {
          resolve(body.data)
        } else {
          reject(new Error((body && body.message) || '上传失败'))
        }
      },
      fail: (err) => reject(new Error(err.errMsg || '网络异常')),
    })
  })
}
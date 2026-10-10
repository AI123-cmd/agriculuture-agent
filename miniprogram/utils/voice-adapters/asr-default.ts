/**
 * 默认语音识别适配器（T4-04）。
 *
 * ⚠ 本实现为占位：方言后端待产品说明书附录 A12 定稿后替换。
 *   接口契约（recognize / stop / cancel 与 viaDialect 字段）不得更改；
 *   A12 定稿后只需替换 createAsrAdapter 的返回实现，页面代码零改动。
 *
 * 当前口径：普通话识别 + viaDialect 恒为 false。
 * 录音用 wx.getRecorderManager（无需额外权限声明），识别结果由后端接口返回。
 * 授权被拒时 reject 可读错误，页面提示「需要允许使用麦克风才能语音提问，也可以直接打字」，
 * 打字输入路径完全不受影响（4.7.3 功能点 4、4.6 功能点 5）。
 */

import { BASE_URL } from '../config'
import { getToken } from '../request'
import type { AsrAdapter } from '../voice'

class DefaultAsrAdapter implements AsrAdapter {
  private recorder: WechatMiniprogram.RecorderManager | null = null
  private settled = false
  private resolveFn: ((v: { text: string; viaDialect: boolean }) => void) | null = null
  private rejectFn: ((e: Error) => void) | null = null

  recognize(opts: { dialect?: boolean }): Promise<{ text: string; viaDialect: boolean }> {
    void opts
    return new Promise((resolve, reject) => {
      this.settled = false
      this.resolveFn = resolve
      this.rejectFn = reject

      const mgr = wx.getRecorderManager()
      this.recorder = mgr

      mgr.onStop((res) => this.upload(res.tempFilePath))
      mgr.onError(() => this.fail(new Error('没听清，再说一次，或者打字告诉我')))

      mgr.start({
        duration: 60000,
        sampleRate: 16000,
        numberOfChannels: 1,
        encodeBitRate: 48000,
        format: 'aac',
      })
    })
  }

  stop(): void {
    if (this.recorder && !this.settled) {
      this.recorder.stop()
    }
  }

  cancel(): void {
    if (this.settled) return
    this.settled = true
    if (this.recorder) {
      try {
        this.recorder.stop()
      } catch (e) {
        // 未在录音时忽略
      }
    }
    this.cleanup()
  }

  /** 上传录音文件到识别接口，返回文本与方言标记 */
  private upload(tempFilePath: string): void {
    const header: Record<string, string> = {}
    const token = getToken()
    if (token) header.Authorization = `Bearer ${token}`

    wx.uploadFile({
      url: `${BASE_URL}/api/ai/asr`,
      filePath: tempFilePath,
      name: 'audio',
      header,
      success: (res) => {
        try {
          const body = JSON.parse(res.data) as {
            code: number
            data: { text: string; viaDialect: boolean }
          }
          if (body && body.code === 200 && body.data) {
            this.ok({ text: body.data.text || '', viaDialect: !!body.data.viaDialect })
          } else {
            this.fail(new Error('没听清，再说一次，或者打字告诉我'))
          }
        } catch (e) {
          this.fail(new Error('没听清，再说一次，或者打字告诉我'))
        }
      },
      fail: () => this.fail(new Error('没听清，再说一次，或者打字告诉我')),
    })
  }

  private ok(v: { text: string; viaDialect: boolean }): void {
    if (this.settled) return
    this.settled = true
    if (this.resolveFn) this.resolveFn(v)
    this.cleanup()
  }

  private fail(e: Error): void {
    if (this.settled) return
    this.settled = true
    if (this.rejectFn) this.rejectFn(e)
    this.cleanup()
  }

  private cleanup(): void {
    this.recorder = null
    this.resolveFn = null
    this.rejectFn = null
  }
}

export function createAsrAdapter(): AsrAdapter {
  return new DefaultAsrAdapter()
}

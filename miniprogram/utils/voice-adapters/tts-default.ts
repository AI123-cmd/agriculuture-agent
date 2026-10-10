/**
 * 默认朗读适配器（T4-04）。
 *
 * 实现口径（T4-04 实现要点三）：
 * - 优先走后端 TTS 接口取音频 URL，用 wx.createInnerAudioContext 在前台播放；
 * - 后端未提供 TTS 接口时，speak 必须**明确失败**（reject 带可读 message），
 *   由页面提示「朗读暂不可用」。不允许静默无响应，也不允许弹假 toast。
 * - 只使用前台 innerAudioContext，不注册任何后台播放能力（4.6 功能点 4、9.3、附录 A5）。
 */

import { request } from '../request'
import type { TtsAdapter } from '../voice'

interface TtsResult {
  audioUrl: string
}

class DefaultTtsAdapter implements TtsAdapter {
  private ctx: WechatMiniprogram.InnerAudioContext | null = null
  private speaking = false

  speak(text: string): Promise<void> {
    const content = (text || '').trim()
    if (!content) return Promise.reject(new Error('没有可朗读的内容'))

    // 同文本可被反复调用：先停掉上一次，再重新播放（无限次重听）
    this.stop()

    return request<TtsResult>({
      url: '/api/ai/tts',
      method: 'POST',
      data: { text: content },
    }).then(
      (res) =>
        new Promise<void>((resolve, reject) => {
          if (!res || !res.audioUrl) {
            reject(new Error('朗读暂不可用'))
            return
          }
          const ctx = wx.createInnerAudioContext()
          this.ctx = ctx
          this.speaking = true
          ctx.onEnded(() => {
            this.speaking = false
            this.teardown()
            resolve()
          })
          ctx.onError(() => {
            this.speaking = false
            this.teardown()
            reject(new Error('现在读不出来，稍后再试'))
          })
          ctx.src = res.audioUrl
          ctx.play()
        }),
      () => {
        this.speaking = false
        return Promise.reject(new Error('朗读暂不可用'))
      },
    )
  }

  stop(): void {
    this.speaking = false
    this.teardown()
  }

  isSpeaking(): boolean {
    return this.speaking
  }

  private teardown(): void {
    if (this.ctx) {
      try {
        this.ctx.stop()
        this.ctx.destroy()
      } catch (e) {
        // 已销毁时忽略
      }
      this.ctx = null
    }
  }
}

export function createTtsAdapter(): TtsAdapter {
  return new DefaultTtsAdapter()
}

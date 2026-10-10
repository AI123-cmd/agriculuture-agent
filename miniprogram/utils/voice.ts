/**
 * 语音朗读（TTS）与语音识别（ASR）的统一封装（T4-04）。
 *
 * 设计边界（产品说明书 4.6 功能点 4、9.3、附录 A5）：
 * - 只做「农户主动点击时朗读」，不注册后台播放，不写页面不可见时触发朗读的定时器。
 * - 方法名一律「动词 + 对象」，不导出任何带 auto 语义的方法，从命名上排除自动播报。
 * - 朗读不承担触达职责，触达由 T3-12 的一次性订阅消息链路负责。
 *
 * 可替换设计（附录 A12 未定）：
 * - 两条能力都走适配器，页面只对本文件的接口编程。
 * - A12 定稿后只替换 getTtsAdapter / getAsrAdapter 返回的实现，页面代码零改动。
 */

import { createTtsAdapter } from './voice-adapters/tts-default'
import { createAsrAdapter } from './voice-adapters/asr-default'

export interface TtsAdapter {
  /** 朗读一段中文文本；同一文本可被反复调用（无限次重听） */
  speak(text: string): Promise<void>
  /** 立即停止当前朗读；未在朗读时安全返回 */
  stop(): void
  /** 查询是否正在朗读，供页面切换按钮图标 */
  isSpeaking(): boolean
}

export interface AsrAdapter {
  /** 开始录音并识别；返回识别文本与是否走了方言通道 */
  recognize(opts: { dialect?: boolean }): Promise<{ text: string; viaDialect: boolean }>
  /** 结束录音并触发识别（松手即结束，不要求点击两次） */
  stop(): void
  /** 取消本次识别，不返回文本；未在录音时安全返回 */
  cancel(): void
}

let ttsSingleton: TtsAdapter | null = null
let asrSingleton: AsrAdapter | null = null

/** 适配器注册点：A12 定稿后，只替换这里返回的实现 */
export function getTtsAdapter(): TtsAdapter {
  if (!ttsSingleton) ttsSingleton = createTtsAdapter()
  return ttsSingleton
}

export function getAsrAdapter(): AsrAdapter {
  if (!asrSingleton) asrSingleton = createAsrAdapter()
  return asrSingleton
}

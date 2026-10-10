#!/usr/bin/env node
/**
 * 语音能力契约断言（T4-04）。
 *
 * 小程序 TS 无法在 Node 里直接 require（依赖 wx 运行时），因此本脚本对源码做
 * 结构化静态断言，覆盖验收标准要求的四组：
 *   1. getTtsAdapter() / getAsrAdapter() 的实现类具备接口声明的全部方法；
 *   2. speak 对同一文本可连续调用两次且第二次仍播放（先停后播，不缓存短路）；
 *   3. stop() 在未播放时调用安全返回（空值守卫）；
 *   4. cancel() 在未录音时调用安全返回（空值守卫）。
 * 另附加两条边界断言：
 *   5. 三个语音相关文件未注册任何后台播放能力（backgroundAudioManager / autoPlay）；
 *   6. chat.ts 已删除占位反馈「语音播放中」。
 *
 * 输出：`voice contract: N checks, 0 failures`。
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('../..', import.meta.url));
const read = (p) => readFileSync(join(ROOT, p), 'utf8');

const VOICE = read('miniprogram/utils/voice.ts');
const TTS = read('miniprogram/utils/voice-adapters/tts-default.ts');
const ASR = read('miniprogram/utils/voice-adapters/asr-default.ts');
const CHAT = read('miniprogram/pages/ai/chat.ts');

const failures = [];
const check = (name, cond) => {
  if (!cond) failures.push(name);
};

/** 取 interface 声明的方法名。 */
function interfaceMethods(src, name) {
  const m = src.match(new RegExp(`interface\\s+${name}\\s*\\{([\\s\\S]*?)\\n\\}`));
  if (!m) return [];
  return [...m[1].matchAll(/^\s*(\w+)\s*\(/gm)].map((x) => x[1]);
}

/** 取 class 内的方法名（缩进两格的成员方法）。 */
function classMethods(src) {
  return [...src.matchAll(/^\s{2}(?:private\s+)?(\w+)\s*\(/gm)].map((x) => x[1]);
}

const ttsIface = interfaceMethods(VOICE, 'TtsAdapter');
const asrIface = interfaceMethods(VOICE, 'AsrAdapter');
const ttsImpl = classMethods(TTS);
const asrImpl = classMethods(ASR);

// 1. 实现类覆盖接口全部方法
check('TtsAdapter 接口含 speak/stop/isSpeaking', ['speak', 'stop', 'isSpeaking'].every((m) => ttsIface.includes(m)));
check('AsrAdapter 接口含 recognize/stop/cancel', ['recognize', 'stop', 'cancel'].every((m) => asrIface.includes(m)));
check('tts 实现类覆盖 TtsAdapter 全部方法', ttsIface.every((m) => ttsImpl.includes(m)));
check('asr 实现类覆盖 AsrAdapter 全部方法', asrIface.every((m) => asrImpl.includes(m)));

// 2. speak 先停后播，保证同一文本可反复重听
check('speak 内调用 this.stop() 先停后播', /speak\([\s\S]*?this\.stop\(\)/.test(TTS));
check('speak 未对重复文本做缓存短路', !/cached|lastText|if\s*\(\s*text\s*===/.test(TTS));

// 3. stop() 具备空值守卫，未播放时安全返回
check('tts stop() 有 ctx 空值守卫', /stop\(\)\s*:\s*void\s*\{[\s\S]*?if\s*\(\s*this\.ctx\s*\)/.test(TTS));
check('tts stop() 用 try/catch 兜住销毁异常', /teardown\(\)[\s\S]*?try\s*\{[\s\S]*?\}\s*catch/.test(TTS) || /stop\(\)[\s\S]*?try/.test(TTS));

// 4. cancel() 未录音时安全返回
check('asr cancel() 先判 settled 再返回', /cancel\(\)\s*:\s*void\s*\{[\s\S]*?if\s*\(\s*this\.settled\s*\)\s*return/.test(ASR));
check('asr cancel() 对 recorder 调用有 try/catch', /cancel\(\)[\s\S]*?try\s*\{[\s\S]*?\}\s*catch/.test(ASR));

// 5. 未注册后台播放能力
const bg = /backgroundAudioManager|setBackgroundAudio|autoPlay/;
check('voice.ts 未注册后台播放', !bg.test(VOICE));
check('tts-default.ts 未注册后台播放', !bg.test(TTS));
check('chat.ts 未引用后台播放', !bg.test(CHAT));

// 6. 占位反馈已删除
check('chat.ts 已删除「语音播放中」占位', !/语音播放中/.test(CHAT));
check('chat.ts 已删除「语音功能开发中」占位', !/语音功能开发中/.test(CHAT));

// 7. viaDialect 字段贯穿接口与实现（A12 可替换前提）
check('AsrAdapter 含 viaDialect 字段', /viaDialect/.test(VOICE));
check('asr 实现返回 viaDialect', /viaDialect/.test(ASR));

// 8. 失败降级：朗读失败给出可读提示而非空白/假 toast
const INDEX = read('miniprogram/pages/index/index.ts');
check('chat.ts 朗读失败给出可读提示', /speak\([\s\S]*?\.catch\([\s\S]*?showToast/.test(CHAT));
check('index.ts 朗读失败给出可读提示', /speak\([\s\S]*?\.catch\([\s\S]*?showToast/.test(INDEX));
check('asr 授权/识别失败给出可读提示', /需要允许使用麦克风|没听清/.test(ASR));

const total = 18;
console.log(`voice contract: ${total} checks, ${failures.length} failures`);
if (failures.length > 0) {
  for (const f of failures) console.error(`  FAIL: ${f}`);
  process.exit(1);
}
process.exit(0);

#!/usr/bin/env node
/**
 * 首页选择计数断言（T4-03）。
 *
 * 依据产品说明书 4.7.3 与附录 A13：农户打开首页后需要做出的核心选择不超过两处
 * （提问、查看今日提示），不含底部导航。
 *
 * 做法：解析 pages/index/index.wxml 的 `<view class="page-content">` 内容区，
 * 收集其中所有 bindtap 事件处理器，按下述口径分类：
 *   - 计入（核心动作）：onAsk（提问）、onUpdateTips（查看今日提示）
 *   - 不计入（模块内/账号级）：onToggleExecuted（执行勾选）、chooseLocation（地点设置）
 *   - 其他未登记处理器：视为新的导航分叉，计入总数（反向验证会命中此类）
 * 断言计入总数恰为 2。
 *
 * 输出：`home choices: 2 (ask, view-tips) - pass`。
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('../..', import.meta.url));
const WXML = join(ROOT, 'miniprogram/pages/index/index.wxml');

const COUNTED = { onAsk: 'ask', onUpdateTips: 'view-tips' };
// onReadTip（朗读）见 T4-03 §七：属提示阅读的辅助动作，与执行勾选同级，不构成导航分叉，不计入两处。
const EXEMPT = new Set(['onToggleExecuted', 'chooseLocation', 'onReadTip']);

/** 取 <view class="page-content"> 内层文本（按 view 标签配对计数）。 */
function extractContentArea(src) {
  const start = src.indexOf('class="page-content"');
  if (start < 0) return '';
  const bodyStart = src.indexOf('>', start) + 1;
  let depth = 1;
  let i = bodyStart;
  while (i < src.length && depth > 0) {
    const open = src.indexOf('<view', i);
    const close = src.indexOf('</view>', i);
    if (close < 0) break;
    if (open >= 0 && open < close) {
      depth += 1;
      i = open + 5;
    } else {
      depth -= 1;
      if (depth === 0) return src.slice(bodyStart, close);
      i = close + 7;
    }
  }
  return src.slice(bodyStart);
}

function main() {
  const src = readFileSync(WXML, 'utf8');
  const content = extractContentArea(src);

  const handlers = [...content.matchAll(/bindtap\s*=\s*"([^"]+)"/g)].map((m) => m[1]);
  const ORDER = ['ask', 'view-tips'];
  const counted = [];
  for (const h of handlers) {
    if (EXEMPT.has(h)) continue;
    counted.push(COUNTED[h] ?? h);
  }
  counted.sort((a, b) => {
    const ia = ORDER.indexOf(a);
    const ib = ORDER.indexOf(b);
    return (ia < 0 ? ORDER.length : ia) - (ib < 0 ? ORDER.length : ib);
  });

  const ok = counted.length === 2;
  const labels = counted.join(', ');
  console.log(`home choices: ${counted.length} (${labels}) - ${ok ? 'pass' : 'fail'}`);
  if (!ok) {
    console.error(`  expected 2, got ${counted.length}`);
    return 1;
  }

  if (/pages\/env\/env/.test(content)) {
    console.error('  首页内容区出现指向 pages/env/env 的跳转，环境入口应只在「我的」页');
    return 1;
  }
  return 0;
}

process.exit(main());

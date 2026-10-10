#!/usr/bin/env node
/**
 * 逐页适老化断言（T4-02）。
 *
 * 对 8 个页面 WXSS 逐页做四类断言，任一命中即失败：
 *   A1  font-size 字面量小于 32rpx（字号下限）
 *   A2  width / height / min-width / min-height 字面量小于 88rpx（热区下限）
 *   A3  无单位 line-height 小于 1.3（行距下限）
 *   A4  .block 内的模块级垂直间距小于 72rpx（段间距下限）
 *
 * 例外与 T4-01 门禁共用 scripts/wxss-token-whitelist.json：登记为装饰图形、
 * 1rpx 分隔线、安全区偏移的字面量在逐页断言中同样跳过，避免两套口径打架。
 *
 * 输出：`8 pages checked, 0 violations`。
 */

import { readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('../..', import.meta.url));
const WHITELIST_PATH = join(ROOT, 'scripts', 'wxss-token-whitelist.json');

const PAGES = [
  'miniprogram/pages/index/index.wxss',
  'miniprogram/pages/ai/chat.wxss',
  'miniprogram/pages/history/list.wxss',
  'miniprogram/pages/mine/mine.wxss',
  'miniprogram/pages/env/env.wxss',
  'miniprogram/pages/profile/farmer-profile.wxss',
  'miniprogram/pages/login/login.wxss',
  'miniprogram/custom-tab-bar/index.wxss',
];

function loadWhitelist() {
  if (!existsSync(WHITELIST_PATH)) return [];
  return JSON.parse(readFileSync(WHITELIST_PATH, 'utf8')).map((e) => ({
    file: e.file,
    match: new RegExp(e.match),
  }));
}

function allowed(entries, file, context) {
  return entries.some((e) => e.file === file && e.match.test(context));
}

function auditPage(file, entries) {
  const abs = join(ROOT, file);
  if (!existsSync(abs)) return [`${file}: 页面 WXSS 不存在`];
  const lines = readFileSync(abs, 'utf8').split(/\r?\n/);
  const violations = [];
  let selector = '';

  lines.forEach((line, i) => {
    const trimmed = line.trim();
    if (trimmed.endsWith('{')) selector = trimmed.slice(0, -1).trim();
    const context = `${selector} ${trimmed}`;
    if (allowed(entries, file, context)) return;

    let m;
    if ((m = trimmed.match(/\bfont-size\s*:\s*([0-9.]+)rpx/)) && parseFloat(m[1]) < 32) {
      violations.push(`${file}:${i + 1}: font-size ${m[1]}rpx 小于下限 32rpx`);
    }
    if ((m = trimmed.match(/\b(?:width|height|min-width|min-height)\s*:\s*([0-9.]+)rpx/)) && parseFloat(m[1]) < 88) {
      violations.push(`${file}:${i + 1}: 尺寸 ${m[1]}rpx 小于热区下限 88rpx`);
    }
    if ((m = trimmed.match(/\bline-height\s*:\s*([0-9]*\.?[0-9]+)\s*;/)) && parseFloat(m[1]) < 1.3) {
      violations.push(`${file}:${i + 1}: line-height ${m[1]} 低于下限 1.3`);
    }
    if (/\.block/.test(selector)) {
      const nums = [];
      const mt = trimmed.match(/\bmargin-top\s*:\s*([0-9.]+)rpx/);
      if (mt) nums.push(mt[1]);
      const pad = trimmed.match(/\bpadding(?:-top|-bottom)?\s*:\s*([^;]+);/);
      if (pad) for (const n of pad[1].matchAll(/([0-9.]+)rpx/g)) nums.push(n[1]);
      if (nums.some((n) => parseFloat(n) < 72)) {
        violations.push(`${file}:${i + 1}: 模块间距低于段间距下限 72rpx`);
      }
    }
  });

  return violations;
}

function main() {
  const entries = loadWhitelist();
  const all = [];
  for (const page of PAGES) all.push(...auditPage(page, entries));

  console.log(`${PAGES.length} pages checked, ${all.length} violations`);
  if (all.length > 0) {
    for (const v of all) console.error(`  ${v}`);
    return 1;
  }
  return 0;
}

process.exit(main());

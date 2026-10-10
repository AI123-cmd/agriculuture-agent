#!/usr/bin/env node
/**
 * WXSS 字面量门禁（T4-01）。
 *
 * 适老约束（字号下限 32rpx、行距不低于 1.3、热区不小于 88rpx、段间距不低于
 * 72rpx）只允许在 miniprogram/styles/tokens.wxss 里出现字面量，页面只使用公共
 * 类或 var(--token)。本脚本扫描 miniprogram/**\/*.wxss（tokens.wxss 除外），
 * 命中下列形态即报错并给出文件与行号：
 *
 *   R1  font-size: <数字>rpx                      字号字面量，应改用公共字号类
 *   R2  width / height / min-width / min-height: <数字>rpx 且 < 88
 *                                                  可点元素尺寸低于热区下限
 *   R3  line-height: <无单位小数> 且 < 1.3         行距低于下限
 *   R4  .block 选择器内 margin-top / 垂直 padding 的 <数字>rpx 且 < 72
 *                                                  模块间距低于段间距下限
 *
 * 确有必要的例外（纯装饰图形、1rpx 分隔线、安全区偏移）登记在
 * scripts/wxss-token-whitelist.json，每条含 file、match、reason、category。
 * 脚本不解析行内注释跳过——例外必须集中登记，才能被评审看见，这与
 * backend/scripts/check_layers.py 的白名单做法同形。
 *
 * 同时校验每个页面/组件/app.wxss 首行含 @import 指向 styles/tokens.wxss。
 */

import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const MINIPROGRAM = join(ROOT, 'miniprogram');
const TOKENS_REL = 'miniprogram/styles/tokens.wxss';
const WHITELIST_PATH = join(ROOT, 'scripts', 'wxss-token-whitelist.json');
const TOKENS_IMPORT = /@import\s+['"][^'"]*styles\/tokens\.wxss['"]\s*;/;
const CATEGORIES = ['decoration', 'divider', 'safe-area'];

function listWxss(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) out.push(...listWxss(full));
    else if (name.endsWith('.wxss')) out.push(full);
  }
  return out.sort();
}

function relOf(abs) {
  return relative(ROOT, abs).split('\\').join('/');
}

function loadWhitelist() {
  if (!existsSync(WHITELIST_PATH)) return [];
  return JSON.parse(readFileSync(WHITELIST_PATH, 'utf8')).map((e) => ({
    file: e.file,
    match: new RegExp(e.match),
    reason: e.reason,
    category: e.category,
  }));
}

function whitelisted(entries, file, context) {
  return entries.find((e) => file === e.file && e.match.test(context));
}

function scanFile(file, entries) {
  const rel = relOf(file);
  const lines = readFileSync(file, 'utf8').split(/\r?\n/);
  const violations = [];
  const hits = { decoration: 0, divider: 0, 'safe-area': 0 };

  const mustImport =
    rel.startsWith('miniprogram/pages/') ||
    rel.startsWith('miniprogram/custom-tab-bar/') ||
    rel === 'miniprogram/app.wxss';
  if (mustImport) {
    const first = lines.find((l) => l.trim() !== '');
    if (!first || !TOKENS_IMPORT.test(first)) {
      violations.push(`${rel}:1: 缺少 @import 指向 styles/tokens.wxss`);
    }
  }

  if (rel === TOKENS_REL) return { violations, hits };

  let selector = '';
  lines.forEach((line, i) => {
    const trimmed = line.trim();
    if (trimmed.endsWith('{')) selector = trimmed.slice(0, -1).trim();
    const context = `${selector} ${trimmed}`;

    const flag = (value, message) => {
      const wl = whitelisted(entries, rel, context);
      if (wl) {
        if (CATEGORIES.includes(wl.category)) hits[wl.category] += 1;
        return;
      }
      violations.push(`${rel}:${i + 1}: ${message}`);
    };

    let m;

    if ((m = trimmed.match(/\bfont-size\s*:\s*([0-9.]+)rpx/))) {
      flag(m[1], `font-size 使用字面量 ${m[1]}rpx，请改用公共字号类或 var(--font-*)`);
    }

    if ((m = trimmed.match(/\b(?:width|height|min-width|min-height)\s*:\s*([0-9.]+)rpx/)) && parseFloat(m[1]) < 88) {
      flag(m[1], `尺寸 ${m[1]}rpx 小于热区下限 88rpx，请改用 var(--tap-min) 或登记白名单`);
    }

    if ((m = trimmed.match(/\bline-height\s*:\s*([0-9]*\.?[0-9]+)\s*;/)) && parseFloat(m[1]) < 1.3) {
      flag(m[1], `无单位 line-height ${m[1]} 低于下限 1.3，请改用 var(--line-tight)`);
    }

    if (/\.block/.test(selector)) {
      const nums = [];
      const mt = trimmed.match(/\bmargin-top\s*:\s*([0-9.]+)rpx/);
      if (mt) nums.push(mt[1]);
      const pad = trimmed.match(/\bpadding(?:-top|-bottom)?\s*:\s*([^;]+);/);
      if (pad) for (const n of pad[1].matchAll(/([0-9.]+)rpx/g)) nums.push(n[1]);
      for (const n of nums) {
        if (parseFloat(n) < 72) {
          flag(n, `模块间距 ${n}rpx 低于段间距下限 72rpx，请改用 var(--space-block)`);
          break;
        }
      }
    }
  });

  return { violations, hits };
}

function main() {
  const entries = loadWhitelist();
  const allViolations = [];
  const hits = { decoration: 0, divider: 0, 'safe-area': 0 };
  let scanned = 0;

  for (const file of listWxss(MINIPROGRAM)) {
    if (relOf(file) === TOKENS_REL) continue;
    scanned += 1;
    const { violations, hits: h } = scanFile(file, entries);
    allViolations.push(...violations);
    for (const c of CATEGORIES) hits[c] += h[c];
  }

  const whitelistedCount = CATEGORIES.reduce((n, c) => n + hits[c], 0);
  const group = CATEGORIES.map((c) => `${c} ${hits[c]}`).join(' / ');
  console.log(
    `scanned ${scanned} wxss files, ${allViolations.length} violations, ${whitelistedCount} whitelisted entries (${group})`,
  );

  if (allViolations.length > 0) {
    for (const v of allViolations) console.error(`  ${v}`);
    console.error('不得在 WXSS 中散落字号/热区/行距/间距字面量；确有例外请在 scripts/wxss-token-whitelist.json 登记。');
    return 1;
  }
  return 0;
}

process.exit(main());

#!/usr/bin/env node
/**
 * 系统状态查看工具
 * 
 * 用法：
 *   node status.mjs              完整状态报告
 *   node status.mjs --brief      简要状态
 *   node status.mjs --proposals  只看待审批建议
 */

import { readFileSync, existsSync, readdirSync, statSync } from 'fs';
import { join, basename } from 'path';

const ROOT = process.env.SELF_IMPROVE_ROOT || process.cwd();
const DATA = join(ROOT, 'data');
const MODULES_DIR = join(ROOT, 'modules');
const CONFIG = join(ROOT, 'config.yaml');
const PENDING = join(ROOT, 'proposals', 'PENDING.md');

const args = process.argv.slice(2);
const brief = args.includes('--brief');
const proposalsOnly = args.includes('--proposals');

function countLines(fp) {
  if (!existsSync(fp)) return 0;
  return readFileSync(fp, 'utf-8').split('\n').filter(l => l.trim()).length;
}

function countJsonl(dir) {
  if (!existsSync(dir)) return 0;
  let total = 0;
  for (const f of readdirSync(dir).filter(f => f.endsWith('.jsonl'))) {
    const lines = readFileSync(join(dir, f), 'utf-8').trim().split('\n').filter(Boolean);
    total += lines.length;
  }
  return total;
}

function countFiles(dir, ext) {
  if (!existsSync(dir)) return 0;
  return readdirSync(dir).filter(f => f.endsWith(ext)).length;
}

function pendingCount() {
  if (!existsSync(PENDING)) return 0;
  const content = readFileSync(PENDING, 'utf-8');
  return (content.match(/⏳ 待审批/g) || []).length;
}

function lastModified(fp) {
  if (!existsSync(fp)) return '—';
  return statSync(fp).mtime.toISOString().split('T')[0];
}

// ─── proposals only ───

if (proposalsOnly) {
  if (!existsSync(PENDING)) {
    console.log('📭 没有待审批建议');
  } else {
    console.log(readFileSync(PENDING, 'utf-8'));
  }
  process.exit(0);
}

// ─── 完整状态 ───

console.log('🧠 Self-Improve 系统状态\n');

// 版本
if (existsSync(CONFIG)) {
  const config = readFileSync(CONFIG, 'utf-8');
  const version = config.match(/^version: "([^"]+)"/m)?.[1] || '?';
  console.log(`版本: v${version}`);
}
console.log(`路径: ${ROOT}`);
console.log('');

// 模块状态
if (!brief) {
  console.log('📦 模块:');
  if (existsSync(CONFIG)) {
    const config = readFileSync(CONFIG, 'utf-8');
    const moduleNames = ['feedback-collector', 'memory-layer', 'evaluator', 'reflector', 'skill-evolver', 'profiler', 'proposer'];
    for (const mod of moduleNames) {
      const enabledMatch = config.match(new RegExp(`${mod}:[\\s\\S]*?enabled: (true|false)`));
      const versionMatch = config.match(new RegExp(`${mod}:[\\s\\S]*?version: "([^"]+)"`));
      const enabled = enabledMatch?.[1] === 'true';
      const ver = versionMatch?.[1] || '?';
      const icon = enabled ? '✅' : '⏸️';
      console.log(`  ${icon} ${mod} v${ver}`);
    }
  }
  console.log('');
}

// 数据统计
console.log('📊 数据:');
console.log(`  HOT 层 (memory.md): ${countLines(join(DATA, 'memory.md'))} 行`);
console.log(`  纠正日志: ${countLines(join(DATA, 'corrections.md'))} 行`);
console.log(`  反思日志: ${countLines(join(DATA, 'reflections.md'))} 行`);
console.log(`  反馈记录: ${countJsonl(join(DATA, 'feedback'))} 条`);
console.log(`  技能模板: ${countFiles(join(DATA, 'skills'), '.yaml')} 个`);
console.log(`  领域经验: ${countFiles(join(DATA, 'domains'), '.md')} 个`);
console.log(`  项目经验: ${countFiles(join(DATA, 'projects'), '.md')} 个`);
console.log(`  归档: ${countFiles(join(DATA, 'archive'), '.md')} 个`);
console.log('');

// 待审批
const pending = pendingCount();
if (pending > 0) {
  console.log(`🔔 待审批建议: ${pending} 条`);
  console.log(`   查看: node status.mjs --proposals`);
} else {
  console.log('✅ 无待审批建议');
}
console.log('');

// 最近活动
if (!brief) {
  console.log('📅 最近更新:');
  const files = [
    ['memory.md', join(DATA, 'memory.md')],
    ['corrections.md', join(DATA, 'corrections.md')],
    ['reflections.md', join(DATA, 'reflections.md')],
    ['profile.md', join(DATA, 'profile.md')],
  ];
  for (const [name, fp] of files) {
    console.log(`  ${name}: ${lastModified(fp)}`);
  }
}

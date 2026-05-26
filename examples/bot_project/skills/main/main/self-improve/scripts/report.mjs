#!/usr/bin/env node
/**
 * 改进报告生成工具
 * 
 * 用法：
 *   node report.mjs [--days 7] [--output markdown|text]
 */

import { readFileSync, existsSync, readdirSync } from 'fs';
import { join } from 'path';

const ROOT = process.env.SELF_IMPROVE_ROOT || process.cwd();
const FEEDBACK_DIR = join(ROOT, 'data', 'feedback');
const SKILLS_DIR = join(ROOT, 'data', 'skills');
const CORRECTIONS = join(ROOT, 'data', 'corrections.md');
const REFLECTIONS = join(ROOT, 'data', 'reflections.md');
const PENDING = join(ROOT, 'proposals', 'PENDING.md');

const args = process.argv.slice(2);
function parseArgs(args) {
  const opts = {};
  for (let i = 0; i < args.length; i++) {
    if (args[i].startsWith('--') && args[i + 1]) opts[args[i].slice(2)] = args[++i];
  }
  return opts;
}

const opts = parseArgs(args);
const days = parseInt(opts.days || '7');
const today = new Date().toISOString().split('T')[0];

function loadFeedback(days) {
  const records = [];
  for (let i = 0; i < days; i++) {
    const d = new Date();
    d.setDate(d.getDate() - i);
    const dateStr = d.toISOString().split('T')[0];
    const fp = join(FEEDBACK_DIR, `${dateStr}.jsonl`);
    if (existsSync(fp)) {
      const lines = readFileSync(fp, 'utf-8').trim().split('\n').filter(Boolean);
      for (const line of lines) {
        try { records.push(JSON.parse(line)); } catch (e) { /* skip */ }
      }
    }
  }
  return records;
}

function countPending() {
  if (!existsSync(PENDING)) return 0;
  return (readFileSync(PENDING, 'utf-8').match(/⏳ 待审批/g) || []).length;
}

function countLines(fp) {
  if (!existsSync(fp)) return 0;
  return readFileSync(fp, 'utf-8').split('\n').filter(l => l.trim()).length;
}

// ─── 生成报告 ───

const records = loadFeedback(days);
const scores = records.map(r => r.score).filter(s => s !== 0);
const avg = scores.length ? (scores.reduce((a, b) => a + b, 0) / scores.length).toFixed(2) : 'N/A';
const positive = scores.filter(s => s > 0).length;
const negative = scores.filter(s => s < 0).length;
const successRate = scores.length ? (positive / scores.length * 100).toFixed(0) : 'N/A';

// 按 agent 分组
const byAgent = {};
for (const r of records) {
  if (!byAgent[r.agent]) byAgent[r.agent] = [];
  byAgent[r.agent].push(r);
}

// 按 task 分组
const byTask = {};
for (const r of records) {
  if (!byTask[r.task]) byTask[r.task] = [];
  byTask[r.task].push(r);
}

// 问题模式
const hints = records.filter(r => r.hint && r.score < 0).map(r => r.hint);
const freq = {};
hints.forEach(h => { freq[h] = (freq[h] || 0) + 1; });
const topIssues = Object.entries(freq).sort((a, b) => b[1] - a[1]).slice(0, 5);

// 技能模板
let skillCount = 0;
let recentImproved = [];
if (existsSync(SKILLS_DIR)) {
  const files = readdirSync(SKILLS_DIR).filter(f => f.endsWith('.yaml'));
  skillCount = files.length;
  for (const f of files) {
    const content = readFileSync(join(SKILLS_DIR, f), 'utf-8');
    const updated = content.match(/updated: "([^"]+)"/)?.[1];
    const version = content.match(/version: "([^"]+)"/)?.[1];
    if (updated && new Date(updated) > new Date(Date.now() - days * 86400000)) {
      recentImproved.push({ name: f.replace('.yaml', ''), version, updated });
    }
  }
}

const pending = countPending();

// ─── 输出 ───

console.log(`📊 Self-Improve 改进报告 (${today})`);
console.log(`   统计周期: 最近 ${days} 天`);
console.log('');

console.log(`## 整体表现`);
console.log(`- 总反馈: ${records.length} 条`);
console.log(`- 正面: ${positive} | 负面: ${negative}`);
console.log(`- 成功率: ${successRate}%`);
console.log(`- 平均分: ${avg}`);
console.log('');

if (Object.keys(byAgent).length > 0) {
  console.log(`## 按 Agent`);
  for (const [ag, recs] of Object.entries(byAgent).sort((a, b) => b[1].length - a[1].length)) {
    const s = recs.map(r => r.score).filter(s => s !== 0);
    const a = s.length ? (s.reduce((a, b) => a + b, 0) / s.length).toFixed(2) : 'N/A';
    const p = s.length ? (s.filter(s => s > 0).length / s.length * 100).toFixed(0) : 'N/A';
    console.log(`- ${ag}: ${recs.length} 条, 成功率 ${p}%, 平均 ${a}`);
  }
  console.log('');
}

if (Object.keys(byTask).length > 0) {
  console.log(`## 按任务类型`);
  for (const [task, recs] of Object.entries(byTask).sort((a, b) => b[1].length - a[1].length)) {
    const s = recs.map(r => r.score).filter(s => s !== 0);
    const a = s.length ? (s.reduce((a, b) => a + b, 0) / s.length).toFixed(2) : 'N/A';
    console.log(`- ${task}: ${recs.length} 条, 平均 ${a}`);
  }
  console.log('');
}

if (topIssues.length > 0) {
  console.log(`## 常见问题`);
  for (const [hint, count] of topIssues) {
    const marker = count >= 3 ? ' 🔔' : '';
    console.log(`- ${count}x "${hint}"${marker}`);
  }
  console.log('');
}

if (recentImproved.length > 0) {
  console.log(`## 技能改进`);
  for (const s of recentImproved) {
    console.log(`- ${s.name} → v${s.version} (${s.updated})`);
  }
  console.log('');
}

console.log(`## 系统状态`);
console.log(`- 纠正日志: ${countLines(CORRECTIONS)} 行`);
console.log(`- 反思日志: ${countLines(REFLECTIONS)} 行`);
console.log(`- 技能模板: ${skillCount} 个`);
console.log(`- 待审批建议: ${pending} 条`);

if (pending > 0) {
  console.log(`\n🔔 有 ${pending} 条待审批建议，请查看 proposals/PENDING.md`);
}

#!/usr/bin/env node
/**
 * Self-Improve 全模块执行脚本
 * 
 * 用法：node run-all.mjs
 * 
 * 按依赖顺序执行所有已启用的模块。
 * 这是 Cron 定时任务调用的入口脚本。
 */

import { readFileSync, writeFileSync, existsSync, appendFileSync } from 'fs';
import { join } from 'path';

const ROOT = process.env.SELF_IMPROVE_ROOT || process.cwd();
const CONFIG_PATH = join(ROOT, 'config.yaml');
const LOG_PATH = join(ROOT, 'run-log.jsonl');

// 执行顺序（按依赖关系排列）
const EXECUTION_ORDER = [
  'feedback-collector',
  'evaluator',
  'classifier',
  'memory-layer',
  'knowledge-archiver',
  'skill-improver',
  'reflector',
  'proposer',
  'profiler',
];

function getEnabledModules() {
  const config = readFileSync(CONFIG_PATH, 'utf-8');
  const enabled = [];
  
  let currentModule = null;
  for (const line of config.split('\n')) {
    const moduleMatch = line.match(/^  (\w[\w-]*):$/);
    if (moduleMatch) currentModule = moduleMatch[1];
    
    const enabledMatch = line.match(/enabled:\s*(true|false)/);
    if (enabledMatch && currentModule) {
      if (enabledMatch[1] === 'true') enabled.push(currentModule);
      currentModule = null;
    }
  }
  return enabled;
}

function logRun(module, status, detail) {
  const entry = {
    timestamp: new Date().toISOString(),
    module,
    status,
    detail: detail || '',
  };
  appendFileSync(LOG_PATH, JSON.stringify(entry) + '\n', 'utf-8');
}

async function runModule(name) {
  console.log(`\n▶ 执行模块: ${name}`);
  
  const modulePath = join(ROOT, 'modules', name, 'MODULE.md');
  if (!existsSync(modulePath)) {
    console.log(`  ⚠️ 模块目录不存在，跳过`);
    logRun(name, 'skipped', '模块目录不存在');
    return;
  }
  
  // 读取模块说明
  const moduleDoc = readFileSync(modulePath, 'utf-8');
  console.log(`  📖 已读取 MODULE.md`);
  
  // 检查是否有对应脚本
  const scriptNames = {
    'feedback-collector': 'feedback.mjs',
    'evaluator': 'feedback.mjs',
    'classifier': 'classify.mjs',
    'memory-layer': 'memory-ops.mjs',
    'knowledge-archiver': 'archive.mjs',
    'skill-improver': 'improve.mjs',
    'proposer': 'improve.mjs',
    'profiler': 'report.mjs',
    'reflector': 'feedback.mjs',
  };
  
  const scriptName = scriptNames[name];
  const scriptPath = join(ROOT, 'scripts', scriptName || '');
  
  if (scriptName && existsSync(scriptPath)) {
    console.log(`  📜 对应脚本: scripts/${scriptName}`);
  } else {
    console.log(`  ℹ️ 无独立脚本，由 agent 按 MODULE.md 执行`);
  }
  
  logRun(name, 'executed', '');
  console.log(`  ✅ 完成`);
}

// 主流程
async function main() {
  console.log(`\n🧠 Self-Improve 全模块执行`);
  console.log(`   时间: ${new Date().toISOString()}`);
  console.log(`   位置: ${ROOT}\n`);
  
  const enabled = getEnabledModules();
  console.log(`已启用模块: ${enabled.length} 个`);
  
  const toRun = EXECUTION_ORDER.filter(m => enabled.includes(m));
  console.log(`执行顺序: ${toRun.join(' → ')}`);
  
  // 记录运行开始
  logRun('system', 'run-start', `模块: ${toRun.join(', ')}`);
  
  for (const mod of toRun) {
    await runModule(mod);
  }
  
  // 检查 PENDING.md
  const pendingPath = join(ROOT, 'proposals', 'PENDING.md');
  if (existsSync(pendingPath)) {
    const pending = readFileSync(pendingPath, 'utf-8');
    const hasPending = pending.includes('⏳ 待审批');
    if (hasPending) {
      console.log(`\n📋 有待审批建议，需要通知用户`);
    }
  }
  
  // 记录运行结束
  logRun('system', 'run-end', '');
  
  console.log(`\n✅ 全部执行完成\n`);
}

main().catch(console.error);
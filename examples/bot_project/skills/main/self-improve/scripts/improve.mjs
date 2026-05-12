#!/usr/bin/env node
/**
 * 技能改进工具
 * 
 * 用法：
 *   node improve.mjs analyze --task code [--days 7]     分析某任务类型的反馈
 *   node improve.mjs create --task code                 创建/更新技能模板
 *   node improve.mjs list                               列出所有技能模板
 *   node improve.mjs show --task code                   查看某个技能模板
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync, readdirSync } from 'fs';
import { join } from 'path';

const ROOT = process.env.SELF_IMPROVE_ROOT || process.cwd();
const FEEDBACK_DIR = join(ROOT, 'data', 'feedback');
const SKILLS_DIR = join(ROOT, 'data', 'skills');

const args = process.argv.slice(2);
const command = args[0];

function parseArgs(args) {
  const opts = {};
  for (let i = 1; i < args.length; i++) {
    if (args[i].startsWith('--') && args[i + 1]) {
      opts[args[i].slice(2)] = args[++i];
    }
  }
  return opts;
}

function loadFeedback(days, taskFilter) {
  const records = [];
  for (let i = 0; i < days; i++) {
    const d = new Date();
    d.setDate(d.getDate() - i);
    const dateStr = d.toISOString().split('T')[0];
    const filePath = join(FEEDBACK_DIR, `${dateStr}.jsonl`);
    if (existsSync(filePath)) {
      const lines = readFileSync(filePath, 'utf-8').trim().split('\n').filter(Boolean);
      for (const line of lines) {
        try {
          const r = JSON.parse(line);
          if (!taskFilter || r.task === taskFilter) records.push(r);
        } catch (e) { /* skip */ }
      }
    }
  }
  return records;
}

function loadSkill(taskName) {
  const filePath = join(SKILLS_DIR, `${taskName}.yaml`);
  if (!existsSync(filePath)) return null;
  // 简单 YAML 解析（key: value 格式）
  const content = readFileSync(filePath, 'utf-8');
  return { raw: content, path: filePath };
}

// ─── analyze: 分析反馈 ───

function analyzeFeedback(opts) {
  const task = opts.task;
  const days = parseInt(opts.days || '7');

  if (!task) {
    console.log('❌ 必须指定 --task');
    return;
  }

  const records = loadFeedback(days, task);

  if (records.length === 0) {
    console.log(`📭 最近 ${days} 天没有 "${task}" 类型的反馈`);
    return;
  }

  const scores = records.map(r => r.score).filter(s => s !== 0);
  const avg = scores.length ? (scores.reduce((a, b) => a + b, 0) / scores.length).toFixed(2) : 'N/A';
  const positive = scores.filter(s => s > 0).length;
  const negative = scores.filter(s => s < 0).length;

  console.log(`📊 "${task}" 任务分析（最近 ${days} 天）\n`);
  console.log(`总反馈: ${records.length} 条`);
  console.log(`平均分: ${avg}`);
  console.log(`正面: ${positive} | 负面: ${negative}\n`);

  // 问题模式
  const hints = records.filter(r => r.hint).map(r => r.hint);
  const freq = {};
  hints.forEach(h => { freq[h] = (freq[h] || 0) + 1; });
  const sorted = Object.entries(freq).sort((a, b) => b[1] - a[1]);

  if (sorted.length > 0) {
    console.log('常见问题:');
    for (const [hint, count] of sorted.slice(0, 10)) {
      const marker = count >= 3 ? ' 🔔 建议改进' : '';
      console.log(`  ${count}x "${hint}"${marker}`);
    }

    // 生成改进建议
    const toImprove = sorted.filter(([_, c]) => c >= 3);
    if (toImprove.length > 0) {
      console.log('\n💡 改进建议:');
      for (const [hint, count] of toImprove) {
        console.log(`  → 针对 "${hint}"（${count} 次）添加 prompt 要求`);
      }
    }
  } else {
    console.log('✅ 没有发现重复问题');
  }

  // 按 agent 分组
  const byAgent = {};
  for (const r of records) {
    if (!byAgent[r.agent]) byAgent[r.agent] = [];
    byAgent[r.agent].push(r);
  }

  console.log('\n按 Agent:');
  for (const [ag, recs] of Object.entries(byAgent)) {
    const s = recs.map(r => r.score).filter(s => s !== 0);
    const a = s.length ? (s.reduce((a, b) => a + b, 0) / s.length).toFixed(2) : 'N/A';
    console.log(`  ${ag}: ${recs.length} 条, 平均 ${a}`);
  }
}

// ─── create: 创建/更新技能模板 ───

function createSkill(opts) {
  const task = opts.task;
  if (!task) {
    console.log('❌ 必须指定 --task');
    return;
  }

  if (!existsSync(SKILLS_DIR)) mkdirSync(SKILLS_DIR, { recursive: true });

  const filePath = join(SKILLS_DIR, `${task}.yaml`);
  const today = new Date().toISOString().split('T')[0];

  // 分析反馈生成改进建议
  const records = loadFeedback(30, task);
  const hints = records.filter(r => r.hint).map(r => r.hint);
  const freq = {};
  hints.forEach(h => { freq[h] = (freq[h] || 0) + 1; });
  const topIssues = Object.entries(freq).sort((a, b) => b[1] - a[1]).filter(([_, c]) => c >= 2);

  let promptAdditions = '';
  if (topIssues.length > 0) {
    promptAdditions = topIssues.map(([hint, count], i) => `  ${i + 1}. 注意: ${hint} (反馈 ${count} 次)`).join('\n');
  }

  // 提取成功案例
  const goodExamples = records.filter(r => r.score > 0 && r.hint).slice(-3);
  let examplesYaml = '';
  if (goodExamples.length > 0) {
    examplesYaml = goodExamples.map(r => `  - "${r.hint || 'good'}"`).join('\n');
  }

  const scores = records.map(r => r.score).filter(s => s !== 0);
  const avgScore = scores.length ? (scores.reduce((a, b) => a + b, 0) / scores.length).toFixed(2) : '0';
  const successRate = scores.length ? (scores.filter(s => s > 0).length / scores.length).toFixed(2) : '0';

  if (existsSync(filePath)) {
    // 更新现有模板
    let content = readFileSync(filePath, 'utf-8');

    // 更新版本号（简单递增）
    const versionMatch = content.match(/version: "(\d+)\.(\d+)"/);
    let newVersion = '1.1';
    if (versionMatch) {
      newVersion = `${versionMatch[1]}.${parseInt(versionMatch[2]) + 1}`;
    }

    content = content.replace(/version: "[^"]*"/, `version: "${newVersion}"`);
    content = content.replace(/updated: "[^"]*"/, `updated: "${today}"`);

    // 更新 metrics
    content = content.replace(/success_rate: [\d.]+/, `success_rate: ${successRate}`);
    content = content.replace(/avg_score: [\d.]+/, `avg_score: ${avgScore}`);
    content = content.replace(/usage_count: \d+/, `usage_count: ${records.length}`);

    // 更新 prompt_additions
    if (promptAdditions) {
      if (content.includes('prompt_additions:')) {
        content = content.replace(/prompt_additions: \|[\s\S]*?(?=\n\w)/, `prompt_additions: |\n${promptAdditions}\n`);
      } else {
        content += `\nprompt_additions: |\n${promptAdditions}\n`;
      }
    }

    writeFileSync(filePath, content, 'utf-8');
    console.log(`✅ 技能模板已更新: ${task} → v${newVersion}`);
  } else {
    // 创建新模板
    const content = `name: ${task}
version: "1.0"
created: "${today}"
updated: "${today}"

prompt_additions: |
${promptAdditions || '  (暂无改进要求)'}

positive_examples:
${examplesYaml || '  (暂无)'}

metrics:
  success_rate: ${successRate}
  avg_score: ${avgScore}
  usage_count: ${records.length}
  last_improved: "${today}"

history:
  - version: "1.0"
    date: "${today}"
    change: "初始版本"
`;

    writeFileSync(filePath, content, 'utf-8');
    console.log(`✅ 技能模板已创建: ${task} v1.0`);
  }

  if (topIssues.length > 0) {
    console.log('\n已添加的改进要求:');
    for (const [hint, count] of topIssues) {
      console.log(`  → ${hint} (${count} 次)`);
    }
  }
}

// ─── list: 列出技能 ───

function listSkills() {
  if (!existsSync(SKILLS_DIR)) {
    console.log('📭 还没有技能模板');
    return;
  }

  const files = readdirSync(SKILLS_DIR).filter(f => f.endsWith('.yaml'));
  if (files.length === 0) {
    console.log('📭 还没有技能模板');
    return;
  }

  console.log(`📦 技能模板（${files.length} 个）:\n`);
  for (const f of files) {
    const content = readFileSync(join(SKILLS_DIR, f), 'utf-8');
    const name = f.replace('.yaml', '');
    const version = content.match(/version: "([^"]+)"/)?.[1] || '?';
    const updated = content.match(/updated: "([^"]+)"/)?.[1] || '?';
    const rate = content.match(/success_rate: ([\d.]+)/)?.[1] || '?';
    console.log(`  ${name} v${version} | 成功率: ${rate} | 更新: ${updated}`);
  }
}

// ─── show: 查看技能 ───

function showSkill(opts) {
  const task = opts.task;
  if (!task) {
    console.log('❌ 必须指定 --task');
    return;
  }

  const skill = loadSkill(task);
  if (!skill) {
    console.log(`📭 技能模板 "${task}" 不存在`);
    return;
  }

  console.log(skill.raw);
}

// ─── 主入口 ───

function main() {
  const opts = parseArgs(args);

  switch (command) {
    case 'analyze':
      analyzeFeedback(opts);
      break;
    case 'create':
      createSkill(opts);
      break;
    case 'list':
      listSkills();
      break;
    case 'show':
      showSkill(opts);
      break;
    default:
      console.log(`技能改进工具

用法:
  node improve.mjs analyze --task <type> [--days 7]   分析某任务类型的反馈
  node improve.mjs create --task <type>               创建/更新技能模板
  node improve.mjs list                               列出所有技能模板
  node improve.mjs show --task <type>                 查看某个技能模板

示例:
  node improve.mjs analyze --task code --days 14
  node improve.mjs create --task search
  node improve.mjs list`);
  }
}

main();

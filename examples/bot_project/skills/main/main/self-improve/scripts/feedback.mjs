#!/usr/bin/env node
/**
 * 反馈收集工具
 * 
 * 用法：
 *   node feedback.mjs log --agent nex --task search --score -1 --hint "结果不相关"
 *   node feedback.mjs log --agent forge --task code --score 1
 *   node feedback.mjs list [--agent nex] [--days 7]
 *   node feedback.mjs stats [--agent nex] [--days 30]
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync, readdirSync } from 'fs';
import { join } from 'path';

const ROOT = process.env.SELF_IMPROVE_ROOT || process.cwd();
const FEEDBACK_DIR = join(ROOT, 'data', 'feedback');
const CORRECTIONS_FILE = join(ROOT, 'data', 'corrections.md');

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

function today() {
  return new Date().toISOString().split('T')[0];
}

function now() {
  return new Date().toISOString();
}

function feedbackPath(date) {
  return join(FEEDBACK_DIR, `${date}.jsonl`);
}

// ─── log: 记录反馈 ───

function logFeedback(opts) {
  if (!opts.agent || !opts.task) {
    console.log('❌ 必须指定 --agent 和 --task');
    process.exit(1);
  }

  const record = {
    ts: now(),
    agent: opts.agent,
    task: opts.task,
    score: parseInt(opts.score || '0'),
    hint: opts.hint || null,
    type: opts.type || 'explicit',
  };

  if (!existsSync(FEEDBACK_DIR)) mkdirSync(FEEDBACK_DIR, { recursive: true });

  const filePath = feedbackPath(today());
  const line = JSON.stringify(record) + '\n';
  
  if (existsSync(filePath)) {
    const existing = readFileSync(filePath, 'utf-8');
    writeFileSync(filePath, existing + line, 'utf-8');
  } else {
    writeFileSync(filePath, line, 'utf-8');
  }

  console.log(`✅ 反馈已记录: ${opts.agent}/${opts.task} score=${record.score}`);

  // 如果是负面反馈，同时写入 corrections.md
  if (record.score < 0 && record.hint) {
    logCorrection(record);
  }

  return record;
}

function logCorrection(record) {
  const entry = `\n## ${today()} ${new Date().toTimeString().slice(0, 5)}

- **agent:** ${record.agent}
- **信号:** ${record.type === 'explicit' ? '用户纠正' : record.type}
- **内容:** "${record.hint}"
- **次数:** 1/3
- **状态:** ⏳ 待观察
`;

  if (!existsSync(CORRECTIONS_FILE)) {
    writeFileSync(CORRECTIONS_FILE, `# 纠正日志\n\n> 最近 50 条纠正记录\n${entry}`, 'utf-8');
  } else {
    let content = readFileSync(CORRECTIONS_FILE, 'utf-8');
    
    // 检查是否有相同内容的纠正，更新次数
    const hintEscaped = record.hint.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const regex = new RegExp(`"${hintEscaped}"\\n- \\*\\*次数:\\*\\* (\\d+)/3`);
    const match = content.match(regex);
    
    if (match) {
      const count = parseInt(match[1]) + 1;
      content = content.replace(
        `"${record.hint}"\n- **次数:** ${match[1]}/3`,
        `"${record.hint}"\n- **次数:** ${count}/3`
      );
      if (count >= 3) {
        content = content.replace(
          new RegExp(`"${hintEscaped}"\\n- \\*\\*次数:\\*\\* ${count}/3\\n- \\*\\*状态:\\*\\* ⏳ 待观察`),
          `"${record.hint}"\n- **次数:** ${count}/3\n- **状态:** 🔔 建议固化`
        );
        console.log(`🔔 纠正 "${record.hint}" 已达 ${count} 次，建议固化为规则`);
      }
      writeFileSync(CORRECTIONS_FILE, content, 'utf-8');
    } else {
      // 新纠正，追加
      writeFileSync(CORRECTIONS_FILE, content + entry, 'utf-8');
    }
  }
}

// ─── list: 列出反馈 ───

function listFeedback(opts) {
  const days = parseInt(opts.days || '7');
  const agent = opts.agent || null;
  const records = loadRecords(days, agent);

  if (records.length === 0) {
    console.log('📭 没有找到反馈记录');
    return;
  }

  console.log(`📋 最近 ${days} 天的反馈（${records.length} 条）:\n`);
  for (const r of records.slice(-20)) {
    const emoji = r.score > 0 ? '✅' : r.score < 0 ? '❌' : '➖';
    console.log(`  ${emoji} [${r.ts.slice(0, 10)}] ${r.agent}/${r.task} score=${r.score}${r.hint ? ` "${r.hint}"` : ''}`);
  }
}

// ─── stats: 统计 ───

function showStats(opts) {
  const days = parseInt(opts.days || '30');
  const agent = opts.agent || null;
  const records = loadRecords(days, agent);

  if (records.length === 0) {
    console.log('📭 没有数据');
    return;
  }

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

  console.log(`📊 Self-Improve 统计（最近 ${days} 天）\n`);
  console.log(`总反馈: ${records.length} 条`);
  
  const scores = records.map(r => r.score).filter(s => s !== 0);
  if (scores.length > 0) {
    const avg = (scores.reduce((a, b) => a + b, 0) / scores.length).toFixed(2);
    const positive = scores.filter(s => s > 0).length;
    console.log(`平均分: ${avg} | 正面: ${positive}/${scores.length} (${(positive / scores.length * 100).toFixed(0)}%)\n`);
  }

  console.log('按 Agent:');
  for (const [ag, recs] of Object.entries(byAgent)) {
    const s = recs.map(r => r.score).filter(s => s !== 0);
    const avg = s.length ? (s.reduce((a, b) => a + b, 0) / s.length).toFixed(2) : 'N/A';
    console.log(`  ${ag}: ${recs.length} 条, 平均 ${avg}`);
  }

  console.log('\n按任务类型:');
  for (const [task, recs] of Object.entries(byTask)) {
    const s = recs.map(r => r.score).filter(s => s !== 0);
    const avg = s.length ? (s.reduce((a, b) => a + b, 0) / s.length).toFixed(2) : 'N/A';
    console.log(`  ${task}: ${recs.length} 条, 平均 ${avg}`);
  }

  // Top hints
  const hints = records.filter(r => r.hint).map(r => r.hint);
  if (hints.length > 0) {
    const freq = {};
    hints.forEach(h => { freq[h] = (freq[h] || 0) + 1; });
    const top = Object.entries(freq).sort((a, b) => b[1] - a[1]).slice(0, 5);
    console.log('\n常见问题:');
    for (const [hint, count] of top) {
      console.log(`  ${count}x "${hint}"`);
    }
  }
}

// ─── 工具函数 ───

function loadRecords(days, agent) {
  const records = [];
  for (let i = 0; i < days; i++) {
    const d = new Date();
    d.setDate(d.getDate() - i);
    const dateStr = d.toISOString().split('T')[0];
    const filePath = feedbackPath(dateStr);
    if (existsSync(filePath)) {
      const lines = readFileSync(filePath, 'utf-8').trim().split('\n').filter(Boolean);
      for (const line of lines) {
        try {
          const record = JSON.parse(line);
          if (!agent || record.agent === agent) {
            records.push(record);
          }
        } catch (e) { /* skip bad lines */ }
      }
    }
  }
  return records.sort((a, b) => a.ts.localeCompare(b.ts));
}

// ─── 主入口 ───

function main() {
  const opts = parseArgs(args);

  switch (command) {
    case 'log':
      logFeedback(opts);
      break;
    case 'list':
      listFeedback(opts);
      break;
    case 'stats':
      showStats(opts);
      break;
    default:
      console.log(`反馈收集工具

用法:
  node feedback.mjs log --agent <name> --task <type> --score <-1|0|1> [--hint "说明"]
  node feedback.mjs list [--agent <name>] [--days 7]
  node feedback.mjs stats [--agent <name>] [--days 30]

示例:
  node feedback.mjs log --agent nex --task search --score -1 --hint "结果不相关"
  node feedback.mjs log --agent forge --task code --score 1
  node feedback.mjs stats --days 7`);
  }
}

main();

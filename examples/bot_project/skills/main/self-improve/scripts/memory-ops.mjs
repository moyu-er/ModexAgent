#!/usr/bin/env node
/**
 * 记忆操作工具
 * 
 * 用法：
 *   node memory-ops.mjs status                          查看各层级状态
 *   node memory-ops.mjs search <keyword>                搜索所有层级
 *   node memory-ops.mjs add --level hot --content "规则" 添加规则
 *   node memory-ops.mjs add --level warm --domain coding --content "规则"
 *   node memory-ops.mjs compact                         压缩 HOT 层
 *   node memory-ops.mjs maintain                        执行升降级维护
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync, readdirSync, statSync, renameSync } from 'fs';
import { join, basename } from 'path';

const ROOT = process.env.SELF_IMPROVE_ROOT || process.cwd();
const DATA = join(ROOT, 'data');
const HOT = join(DATA, 'memory.md');
const DOMAINS = join(DATA, 'domains');
const PROJECTS = join(DATA, 'projects');
const ARCHIVE = join(DATA, 'archive');
const INDEX = join(DATA, 'index.md');

const args = process.argv.slice(2);
const command = args[0];

function parseArgs(args) {
  const opts = {};
  for (let i = 1; i < args.length; i++) {
    if (args[i].startsWith('--') && args[i + 1]) {
      opts[args[i].slice(2)] = args[++i];
    } else if (!args[i].startsWith('--')) {
      opts._positional = opts._positional || [];
      opts._positional.push(args[i]);
    }
  }
  return opts;
}

function countLines(filePath) {
  if (!existsSync(filePath)) return 0;
  return readFileSync(filePath, 'utf-8').split('\n').filter(l => l.trim()).length;
}

function lastModified(filePath) {
  if (!existsSync(filePath)) return '—';
  return statSync(filePath).mtime.toISOString().split('T')[0];
}

// ─── status: 查看状态 ───

function showStatus() {
  console.log('📊 Self-Improve 记忆状态\n');

  // HOT
  const hotLines = countLines(HOT);
  console.log(`🔥 HOT（始终加载）:`);
  console.log(`   memory.md: ${hotLines} 行 (上限 100) | 更新: ${lastModified(HOT)}\n`);

  // WARM - domains
  console.log(`🌡️  WARM（按需加载）:`);
  if (existsSync(DOMAINS)) {
    const files = readdirSync(DOMAINS).filter(f => f.endsWith('.md'));
    if (files.length === 0) {
      console.log('   domains/: (空)');
    } else {
      for (const f of files) {
        const fp = join(DOMAINS, f);
        console.log(`   domains/${f}: ${countLines(fp)} 行 | 更新: ${lastModified(fp)}`);
      }
    }
  } else {
    console.log('   domains/: (空)');
  }

  // WARM - projects
  if (existsSync(PROJECTS)) {
    const files = readdirSync(PROJECTS).filter(f => f.endsWith('.md'));
    if (files.length === 0) {
      console.log('   projects/: (空)');
    } else {
      for (const f of files) {
        const fp = join(PROJECTS, f);
        console.log(`   projects/${f}: ${countLines(fp)} 行 | 更新: ${lastModified(fp)}`);
      }
    }
  } else {
    console.log('   projects/: (空)');
  }

  // COLD
  console.log('');
  if (existsSync(ARCHIVE)) {
    const files = readdirSync(ARCHIVE).filter(f => f.endsWith('.md'));
    console.log(`❄️  COLD（归档）:`);
    if (files.length === 0) {
      console.log('   archive/: (空)');
    } else {
      for (const f of files) {
        const fp = join(ARCHIVE, f);
        console.log(`   archive/${f}: ${countLines(fp)} 行`);
      }
    }
  } else {
    console.log('❄️  COLD: (空)');
  }
}

// ─── search: 搜索 ───

function searchMemory(keyword) {
  if (!keyword) {
    console.log('❌ 请指定搜索关键词');
    return;
  }

  console.log(`🔍 搜索: "${keyword}"\n`);
  const kw = keyword.toLowerCase();
  let found = 0;

  // 搜索函数
  function searchFile(filePath, label) {
    if (!existsSync(filePath)) return;
    const lines = readFileSync(filePath, 'utf-8').split('\n');
    for (let i = 0; i < lines.length; i++) {
      if (lines[i].toLowerCase().includes(kw)) {
        console.log(`  📍 ${label}:${i + 1} → ${lines[i].trim()}`);
        found++;
      }
    }
  }

  // HOT
  searchFile(HOT, 'memory.md');

  // WARM
  if (existsSync(DOMAINS)) {
    for (const f of readdirSync(DOMAINS).filter(f => f.endsWith('.md'))) {
      searchFile(join(DOMAINS, f), `domains/${f}`);
    }
  }
  if (existsSync(PROJECTS)) {
    for (const f of readdirSync(PROJECTS).filter(f => f.endsWith('.md'))) {
      searchFile(join(PROJECTS, f), `projects/${f}`);
    }
  }

  // COLD
  if (existsSync(ARCHIVE)) {
    for (const f of readdirSync(ARCHIVE).filter(f => f.endsWith('.md'))) {
      searchFile(join(ARCHIVE, f), `archive/${f}`);
    }
  }

  // corrections & reflections
  searchFile(join(DATA, 'corrections.md'), 'corrections.md');
  searchFile(join(DATA, 'reflections.md'), 'reflections.md');

  if (found === 0) {
    console.log('  (无匹配)');
  } else {
    console.log(`\n共 ${found} 条匹配`);
  }
}

// ─── add: 添加规则 ───

function addRule(opts) {
  if (!opts.content) {
    console.log('❌ 必须指定 --content');
    return;
  }

  const level = opts.level || 'hot';
  const content = opts.content;
  const ts = new Date().toISOString().split('T')[0];
  const entry = `- ${content} (${ts})\n`;

  let targetFile;

  if (level === 'hot') {
    targetFile = HOT;
  } else if (level === 'warm' && opts.domain) {
    if (!existsSync(DOMAINS)) mkdirSync(DOMAINS, { recursive: true });
    targetFile = join(DOMAINS, `${opts.domain}.md`);
    if (!existsSync(targetFile)) {
      writeFileSync(targetFile, `# ${opts.domain} 领域经验\n\n`, 'utf-8');
    }
  } else if (level === 'warm' && opts.project) {
    if (!existsSync(PROJECTS)) mkdirSync(PROJECTS, { recursive: true });
    targetFile = join(PROJECTS, `${opts.project}.md`);
    if (!existsSync(targetFile)) {
      writeFileSync(targetFile, `# ${opts.project} 项目经验\n\n`, 'utf-8');
    }
  } else {
    console.log('❌ warm 层级需要指定 --domain 或 --project');
    return;
  }

  const existing = existsSync(targetFile) ? readFileSync(targetFile, 'utf-8') : '';
  writeFileSync(targetFile, existing + entry, 'utf-8');
  console.log(`✅ 已添加到 ${level}: "${content}"`);
}

// ─── compact: 压缩 HOT 层 ───

function compactHot() {
  if (!existsSync(HOT)) {
    console.log('📭 memory.md 不存在');
    return;
  }

  const lines = readFileSync(HOT, 'utf-8').split('\n');
  const contentLines = lines.filter(l => l.trim() && !l.startsWith('#') && !l.startsWith('>'));

  if (contentLines.length <= 100) {
    console.log(`✅ memory.md 当前 ${contentLines.length} 行，无需压缩`);
    return;
  }

  console.log(`⚠️  memory.md 当前 ${contentLines.length} 行，超过 100 行上限`);
  console.log(`💡 建议手动审查后：`);
  console.log(`   1. 合并重复/相似规则`);
  console.log(`   2. 将低频规则移到 domains/ 或 projects/`);
  console.log(`   3. 将过期规则移到 archive/`);
  console.log(`\n当前不自动压缩，避免丢失重要规则。`);
}

// ─── maintain: 升降级维护 ───

function maintain() {
  console.log('🔧 执行记忆维护...\n');

  // 检查 HOT 层大小
  const hotLines = countLines(HOT);
  if (hotLines > 100) {
    console.log(`⚠️  HOT 层超限: ${hotLines}/100 行，需要压缩`);
  } else {
    console.log(`✅ HOT 层正常: ${hotLines}/100 行`);
  }

  // 检查 WARM 层大小
  for (const dir of [DOMAINS, PROJECTS]) {
    if (!existsSync(dir)) continue;
    for (const f of readdirSync(dir).filter(f => f.endsWith('.md'))) {
      const fp = join(dir, f);
      const lines = countLines(fp);
      if (lines > 200) {
        console.log(`⚠️  WARM ${basename(dir)}/${f} 超限: ${lines}/200 行`);
      }
    }
  }

  // 更新索引
  updateIndex();
  console.log('\n✅ 维护完成');
}

// ─── 更新索引 ───

function updateIndex() {
  let content = `# 数据索引\n\n> 自动维护，记录所有数据文件的状态\n\n| 文件 | 层级 | 行数 | 最后更新 |\n|------|------|------|---------|\n`;

  content += `| memory.md | HOT | ${countLines(HOT)} | ${lastModified(HOT)} |\n`;
  content += `| corrections.md | — | ${countLines(join(DATA, 'corrections.md'))} | ${lastModified(join(DATA, 'corrections.md'))} |\n`;
  content += `| reflections.md | — | ${countLines(join(DATA, 'reflections.md'))} | ${lastModified(join(DATA, 'reflections.md'))} |\n`;
  content += `| profile.md | — | ${countLines(join(DATA, 'profile.md'))} | ${lastModified(join(DATA, 'profile.md'))} |\n`;

  for (const dir of [DOMAINS, PROJECTS]) {
    if (!existsSync(dir)) continue;
    const label = basename(dir);
    for (const f of readdirSync(dir).filter(f => f.endsWith('.md'))) {
      const fp = join(dir, f);
      content += `| ${label}/${f} | WARM | ${countLines(fp)} | ${lastModified(fp)} |\n`;
    }
  }

  if (existsSync(ARCHIVE)) {
    for (const f of readdirSync(ARCHIVE).filter(f => f.endsWith('.md'))) {
      const fp = join(ARCHIVE, f);
      content += `| archive/${f} | COLD | ${countLines(fp)} | ${lastModified(fp)} |\n`;
    }
  }

  writeFileSync(INDEX, content, 'utf-8');
  console.log('📝 索引已更新');
}

// ─── 主入口 ───

function main() {
  const opts = parseArgs(args);

  switch (command) {
    case 'status':
      showStatus();
      break;
    case 'search':
      searchMemory(opts._positional?.[0] || opts.keyword);
      break;
    case 'add':
      addRule(opts);
      break;
    case 'compact':
      compactHot();
      break;
    case 'maintain':
      maintain();
      break;
    default:
      console.log(`记忆操作工具

用法:
  node memory-ops.mjs status                                    查看各层级状态
  node memory-ops.mjs search <keyword>                          搜索所有层级
  node memory-ops.mjs add --level hot --content "规则内容"       添加到 HOT
  node memory-ops.mjs add --level warm --domain coding --content "规则"  添加到领域
  node memory-ops.mjs add --level warm --project myapp --content "规则"  添加到项目
  node memory-ops.mjs compact                                   检查 HOT 层是否需要压缩
  node memory-ops.mjs maintain                                  执行升降级维护`);
  }
}

main();

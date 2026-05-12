#!/usr/bin/env node
/**
 * Self-Improve 系统一键安装脚本
 * 版本：2.2.0
 * 
 * 用法：node setup.mjs [--config user-config.yaml]
 * 
 * 功能：
 * 1. 读取用户配置
 * 2. 创建完整目录结构
 * 3. 初始化所有数据文件
 * 4. 复制模块文件
 * 5. 生成 Cron 任务建议
 */

import { mkdirSync, writeFileSync, readFileSync, existsSync, cpSync } from 'fs';
import { join, dirname, resolve } from 'path';
import { fileURLToPath } from 'url';
import { parse as parseYaml } from 'yaml';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const SOURCE_DIR = join(__dirname, '..');

// 解析命令行参数
const args = process.argv.slice(2);
let configPath = 'user-config.yaml';
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--config' && args[i + 1]) {
    configPath = args[i + 1];
    break;
  }
}

// 读取用户配置
let userConfig = {};
const fullConfigPath = join(SOURCE_DIR, configPath);
if (existsSync(fullConfigPath)) {
  try {
    const configContent = readFileSync(fullConfigPath, 'utf-8');
    userConfig = parseYaml(configContent);
    console.log(`📋 已加载配置: ${configPath}`);
  } catch (e) {
    console.log(`⚠️  配置文件解析失败，使用默认值: ${e.message}`);
  }
} else {
  console.log(`⚠️  未找到配置文件 ${configPath}，使用当前目录`);
}

// 确定安装路径
const ROOT = userConfig.storage?.root || SOURCE_DIR;
const KNOWLEDGE_ROOT = userConfig.storage?.knowledge_root || join(ROOT, 'knowledge');
const WORKSPACE_ROOT = userConfig.storage?.workspace_root || '';
const OWNER_NAME = userConfig.owner?.name || 'Owner';
const OWNER_TZ = userConfig.owner?.timezone || 'Asia/Shanghai';
const MAIN_AGENT = userConfig.agent?.main_agent || 'main';
const CRON_MODEL = userConfig.agent?.cron_model || 'bailian/qwen3.5-plus';
const CRON_EXPR = userConfig.schedule?.cron || '0 4 */3 * *';
const NOTIFICATION_CHANNEL = userConfig.owner?.notification?.channel || '';
const NOTIFICATION_TO = userConfig.owner?.notification?.to || '';

console.log(`\n🧠 Self-Improve 系统安装 (v2.2.0)`);
console.log(`   目标路径: ${ROOT}`);
console.log(`   知识库: ${KNOWLEDGE_ROOT}`);
console.log(`   时区: ${OWNER_TZ}\n`);

// ─── 1. 创建目录结构 ───

const dirs = [
  '',
  'modules',
  'modules/feedback-collector',
  'modules/distill-classifier',
  'modules/memory-layer',
  'modules/proposer',
  'modules/profiler',
  'modules/reflector',
  'modules/notify',
  'modules/archived',
  'data',
  'data/feedback',
  'data/skills',
  'data/themes',
  'data/themes/behavior',
  'data/themes/communication',
  'data/themes/tools',
  'data/themes/coding',
  'data/themes/search',
  'data/themes/writing',
  'data/themes/collaboration',
  'data/themes/preferences',
  'data/themes/professional',
  'data/themes/personality',
  'data/themes/devops',
  'data/domains',
  'data/projects',
  'data/jobs',
  'data/archive',
  'data/backup',
  'data/high-value',
  'data/errors',
  'data/lessons',
  'proposals',
  'drafts',
  'scripts',
];

for (const dir of dirs) {
  const fullPath = join(ROOT, dir);
  if (!existsSync(fullPath)) {
    mkdirSync(fullPath, { recursive: true });
    console.log(`  📁 创建: ${dir || '(root)'}`);
  }
}

// ─── 2. 复制核心文件 ───

const coreFiles = [
  'ENGINE.md',
  'SYSTEM.md',
  'RUNTIME.md',
  'config.yaml',
  'changelog.md',
];

for (const file of coreFiles) {
  const src = join(SOURCE_DIR, file);
  const dst = join(ROOT, file);
  if (existsSync(src) && !existsSync(dst)) {
    cpSync(src, dst);
    console.log(`  📄 复制: ${file}`);
  }
}

// 复制模块文件
const modules = [
  'feedback-collector',
  'distill-classifier',
  'memory-layer',
  'proposer',
  'profiler',
  'reflector',
  'notify',
];

for (const mod of modules) {
  const srcDir = join(SOURCE_DIR, 'modules', mod);
  const dstDir = join(ROOT, 'modules', mod);
  if (existsSync(srcDir)) {
    cpSync(srcDir, dstDir, { recursive: true });
    console.log(`  📦 模块: ${mod}`);
  }
}

// 复制 prompts
const promptsDir = join(SOURCE_DIR, 'prompts');
if (existsSync(promptsDir)) {
  cpSync(promptsDir, join(ROOT, 'prompts'), { recursive: true });
  console.log(`  📦 Prompts`);
}

// 复制脚本
const scriptsDir = join(SOURCE_DIR, 'scripts');
if (existsSync(scriptsDir)) {
  cpSync(scriptsDir, join(ROOT, 'scripts'), { recursive: true });
  console.log(`  📦 脚本`);
}

// ─── 3. 初始化数据文件 ───

const dataFiles = {
  'data/hot.md': `# HOT 层活跃规则

> ≤100 行，固化前加载

## 确认的偏好

（暂无）

## 活跃规则

（暂无）
`,

  'data/corrections.md': `# 纠正日志

> 最近 50 条纠正记录

（暂无）
`,

  'data/reflections.md': `# 自反思日志

> 任务完成后的反思

（暂无）
`,

  'data/profile.md': `# 团队能力画像

> 最后更新：待首次生成

（数据不足，待积累后生成）
`,

  'data/notification-log.jsonl': '',
  'run-log.jsonl': '',
};

for (const [file, content] of Object.entries(dataFiles)) {
  const fullPath = join(ROOT, file);
  if (!existsSync(fullPath)) {
    writeFileSync(fullPath, content, 'utf-8');
    console.log(`  📝 初始化: ${file}`);
  }
}

// 初始化主题索引
const themes = ['behavior', 'communication', 'tools', 'coding', 'search', 'writing', 'collaboration', 'preferences', 'professional', 'personality', 'devops'];
for (const theme of themes) {
  const indexPath = join(ROOT, 'data', 'themes', theme, 'data_structure.md');
  if (!existsSync(indexPath)) {
    const themeContent = `# ${theme} 主题\n\n> 自动生成\n\n## 规则列表\n\n（暂无）\n`;
    writeFileSync(indexPath, themeContent, 'utf-8');
  }
}

// ─── 4. 创建目录索引 ───

const mainIndex = `# Self-Improve 自我进化系统

> 安装位置：${ROOT}
> 版本：2.2.0

## 文件清单

### 核心
- ENGINE.md — 动力机制（触发规则、分类映射）
- SYSTEM.md — 系统说明
- RUNTIME.md — 运行时机制
- config.yaml — 模块配置
- changelog.md — 升级日志

### 模块（modules/）
可插拔模块，详见各模块的 MODULE.md

### 数据（data/）
- hot.md — HOT 层活跃规则
- corrections.md — 纠正日志
- reflections.md — 反思日志
- profile.md — 能力画像
- feedback/ — 原始反馈
- themes/ — 主题分类
- archive/ — 归档

### 审批（proposals/）
- PENDING.md — 待固化建议

### 脚本（scripts/）
- setup.mjs — 安装
- run-all.mjs — 执行全部
- 其他工具脚本
`;

writeFileSync(join(ROOT, 'data_structure.md'), mainIndex, 'utf-8');
console.log(`  📝 创建索引`);

// ─── 5. 更新 config.yaml 中的路径 ───

const configYaml = `# Self-Improve 系统配置
# 版本：2.2.0
# 自动生成，请勿手动修改路径

version: "2.2.0"
updated: "${new Date().toISOString().split('T')[0]}"

storage:
  root: "${ROOT.replace(/\\/g, '\\\\')}"
  knowledge_root: "${KNOWLEDGE_ROOT.replace(/\\/g, '\\\\')}"
  workspace_root: "${WORKSPACE_ROOT.replace(/\\/g, '\\\\')}"
  last_success_ts: ""

schedule:
  cron: "${CRON_EXPR}"
  model: "${CRON_MODEL}"

modules:
  feedback-collector:
    enabled: true
    version: "1.1.0"
    order: 1
    description: "扫描对话，提取信号"
    prompt: "prompts/feedback-collector.md"
    data_dirs: ["data/feedback"]
    inputs: ["memory/*.md", "data/reflections.md"]

  distill-classifier:
    enabled: true
    version: "1.0.0"
    order: 2
    description: "提炼三层次规则 + 分类"
    prompt: "prompts/distill-classifier.md"
    data_dirs: ["data/themes"]
    inputs: ["data/feedback/*.jsonl"]

  memory-layer:
    enabled: true
    version: "3.2.0"
    order: 3
    description: "分层记忆管理"
    prompt: "prompts/memory-layer.md"
    data_dirs: ["data/hot.md", "data/themes", "data/archive"]
    params:
      hot_limit: 100
      promote_threshold: 3
      demote_days: 30
      archive_days: 90

  proposer:
    enabled: true
    version: "1.1.0"
    order: 4
    description: "判断产出通道"
    prompt: "prompts/proposer.md"
    data_dirs: ["proposals/PENDING.md", "drafts", "data/errors", "data/lessons", "data/high-value"]

  reflector:
    enabled: true
    version: "1.0.0"
    order: 5
    description: "自反思"
    prompt: "prompts/reflector.md"
    data_dirs: ["data/reflections.md"]

  profiler:
    enabled: true
    version: "1.2.0"
    order: 6
    description: "团队能力画像"
    prompt: "prompts/profiler.md"
    data_dirs: ["data/profile.md"]
    params:
      run_every: 3

  notify:
    enabled: true
    version: "1.0.0"
    order: 7
    description: "通知待审批建议"
    prompt: "prompts/notify.md"
    inputs: ["proposals/PENDING.md"]

  execution:
    enabled: true
    version: "1.0.0"
    order: 99
    description: "审批后执行"
    prompt: "prompts/execution.md"
    trigger: "manual"

approval:
  required_files:
    - "AGENTS.md"
    - "SOUL.md"
    - "TOOLS.md"
    - "MEMORY.md"
    - "HEARTBEAT.md"
    - "openclaw.json"
    - "SKILL.md"

global:
  learning_from_silence: false
  auto_delete: false
  confirm_before_promote: false
`;

writeFileSync(join(ROOT, 'config.yaml'), configYaml, 'utf-8');
console.log(`  ⚙️  更新 config.yaml`);

// ─── 6. 生成 Cron 建议 ───

const workspaceScanInstruction = WORKSPACE_ROOT 
  ? `扫描 ${WORKSPACE_ROOT} 下所有 agent 的 memory/ 目录`
  : `扫描 OpenClaw workspace 下所有 agent 的 memory/ 目录`;

const notificationInstruction = NOTIFICATION_CHANNEL && NOTIFICATION_TO
  ? `如有待审批建议，用 message 工具发送到 ${NOTIFICATION_CHANNEL}:${NOTIFICATION_TO}`
  : `如有待审批建议，记录到 run-log.jsonl`;

const cronProposal = `# 待审批的配置修改建议

> 只有固化到系统文件时需要确认，其他全自动。

---

## [P-001] 添加 Cron 定时任务

- **来源:** self-improve 安装
- **建议修改:** 在 openclaw.json 的 cron 中添加：

\`\`\`json
{
  "name": "self-improve",
  "schedule": {
    "kind": "cron",
    "expr": "${CRON_EXPR}",
    "tz": "${OWNER_TZ}"
  },
  "sessionTarget": "isolated",
  "payload": {
    "kind": "agentTurn",
    "message": "你是 Self-Improve 系统的执行者。请按以下步骤执行：\\n1. 读取 ${ROOT.replace(/\\/g, '\\\\\\\\')}\\\\ENGINE.md 了解完整流程\\n2. 读取 ${ROOT.replace(/\\/g, '\\\\\\\\')}\\\\config.yaml 了解模块配置\\n3. ${workspaceScanInstruction}\\n4. 按执行顺序运行所有已启用模块\\n5. ${notificationInstruction}\\n6. 记录运行日志到 run-log.jsonl",
    "model": "${CRON_MODEL}"
  }
}
\`\`\`

- **目标文件:** openclaw.json
- **理由:** 每 3 天自动运行自我改进
- **状态:** ⏳ 待审批

---

## [P-002] 可选：添加 HEARTBEAT 入口

- **来源:** self-improve 安装
- **建议修改:** 在 HEARTBEAT.md 添加：

\`\`\`markdown
## Self-Improve（每 3 天）
- 检查上次运行时间，如 ≥ 3 天 → 提醒运行
- 检查 proposals/PENDING.md，如有待审批 → 提醒
\`\`\`

- **目标文件:** HEARTBEAT.md
- **理由:** 补充检查入口
- **状态:** ⏳ 待审批
`;

writeFileSync(join(ROOT, 'proposals', 'PENDING.md'), cronProposal, 'utf-8');
console.log(`  📝 生成 Cron 建议`);

// ─── 完成 ───

console.log(`\n✅ 安装完成！\n`);
console.log(`📖 系统说明: ${join(ROOT, 'SYSTEM.md')}`);
console.log(`⚙️  动力机制: ${join(ROOT, 'ENGINE.md')}`);
console.log(`📋 待审批建议: ${join(ROOT, 'proposals', 'PENDING.md')}`);
console.log(`\n💡 下一步:`);
console.log(`   1. 查看 proposals/PENDING.md`);
console.log(`   2. 将 Cron 任务添加到 OpenClaw 配置`);
console.log(`   3. 系统将每 3 天自动运行\n`);

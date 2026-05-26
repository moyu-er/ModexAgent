#!/usr/bin/env node
/**
 * 模块管理工具
 * 
 * 用法：
 *   node module.mjs enable <模块名>
 *   node module.mjs disable <模块名>
 *   node module.mjs list
 *   node module.mjs add <模块名>
 *   node module.mjs remove <模块名>
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync, rmdirSync } from 'fs';
import { join } from 'path';

const ROOT = process.env.SELF_IMPROVE_ROOT || process.cwd();
const CONFIG_PATH = join(ROOT, 'config.yaml');

const args = process.argv.slice(2);
const action = args[0];
const moduleName = args[1];

function loadConfig() {
  const content = readFileSync(CONFIG_PATH, 'utf-8');
  // 简单解析 YAML（实际应该用 yaml 库）
  return content;
}

function saveConfig(content) {
  writeFileSync(CONFIG_PATH, content, 'utf-8');
}

function enableModule(name) {
  let config = loadConfig();
  const regex = new RegExp(`(${name}:\\s*enabled:)\\s*false`, 'g');
  config = config.replace(regex, `$1 true`);
  saveConfig(config);
  console.log(`✅ 模块 ${name} 已启用`);
}

function disableModule(name) {
  let config = loadConfig();
  const regex = new RegExp(`(${name}:\\s*enabled:)\\s*true`, 'g');
  config = config.replace(regex, `$1 false`);
  saveConfig(config);
  console.log(`⏸️ 模块 ${name} 已禁用`);
}

function listModules() {
  const config = loadConfig();
  console.log('\n模块列表：\n');
  
  // 简单提取模块状态
  const lines = config.split('\n');
  let currentModule = null;
  
  for (const line of lines) {
    const moduleMatch = line.match(/^  (\w+):$/);
    if (moduleMatch) {
      currentModule = moduleMatch[1];
    }
    const enabledMatch = line.match(/enabled:\s*(true|false)/);
    if (enabledMatch && currentModule) {
      const status = enabledMatch[1] === 'true' ? '✅' : '⏸️';
      console.log(`  ${status} ${currentModule}`);
      currentModule = null;
    }
  }
  console.log('');
}

function addModule(name) {
  const moduleDir = join(ROOT, 'modules', name);
  
  if (existsSync(moduleDir)) {
    console.log(`❌ 模块 ${name} 已存在`);
    return;
  }
  
  // 创建目录
  mkdirSync(moduleDir, { recursive: true });
  
  // 创建 MODULE.md 模板
  const template = `# ${name}

> 版本：1.0.0
> 状态：active

## 职责
（描述这个模块做什么）

## 输入
（从哪读取数据）

## 输出
（写到哪）

## 接口
（如何调用）

## 依赖
（依赖哪些模块）

## 升级日志
- 1.0.0 (${new Date().toISOString().split('T')[0]}) 初始版本
`;
  
  writeFileSync(join(moduleDir, 'MODULE.md'), template, 'utf-8');
  
  console.log(`✅ 模块 ${name} 已创建`);
  console.log(`📝 请编辑 modules/${name}/MODULE.md`);
  console.log(`📝 请在 config.yaml 中注册此模块`);
}

function removeModule(name) {
  const moduleDir = join(ROOT, 'modules', name);
  
  if (!existsSync(moduleDir)) {
    console.log(`❌ 模块 ${name} 不存在`);
    return;
  }
  
  // 先禁用
  disableModule(name);
  
  // 删除目录
  try {
    rmdirSync(moduleDir, { recursive: true });
    console.log(`🗑️ 模块 ${name} 已删除`);
    console.log(`📝 请从 config.yaml 中移除注册`);
  } catch (e) {
    console.log(`❌ 删除失败：${e.message}`);
  }
}

// 执行
switch (action) {
  case 'enable':
    if (!moduleName) {
      console.log('用法：node module.mjs enable <模块名>');
      process.exit(1);
    }
    enableModule(moduleName);
    break;
  
  case 'disable':
    if (!moduleName) {
      console.log('用法：node module.mjs disable <模块名>');
      process.exit(1);
    }
    disableModule(moduleName);
    break;
  
  case 'list':
    listModules();
    break;
  
  case 'add':
    if (!moduleName) {
      console.log('用法：node module.mjs add <模块名>');
      process.exit(1);
    }
    addModule(moduleName);
    break;
  
  case 'remove':
    if (!moduleName) {
      console.log('用法：node module.mjs remove <模块名>');
      process.exit(1);
    }
    removeModule(moduleName);
    break;
  
  default:
    console.log(`
模块管理工具

用法：
  node module.mjs enable <模块名>   启用模块
  node module.mjs disable <模块名>  禁用模块
  node module.mjs list              列出所有模块
  node module.mjs add <模块名>      新增模块
  node module.mjs remove <模块名>   删除模块
`);
}
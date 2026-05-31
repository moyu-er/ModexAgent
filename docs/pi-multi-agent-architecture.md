# pi Multi-Agent 架构完整梳理

> 本文档基于 pi 本地源码逆向分析，旨在为后续设计/实现提供参考。
>
> **最后更新**: 2026-05-31

---

## 1. 本地源码路径总览

| 包名              | 本地路径                                                        | 职责                                                     |
| ----------------- | --------------------------------------------------------------- | -------------------------------------------------------- |
| `pi-coding-agent` | `~/.pi/agent/npm/node_modules/@earendil-works/pi-coding-agent/` | 主 Agent 运行时、Session 管理、工具注册、系统提示词构建  |
| `pi-subagents`    | `~/.pi/agent/npm/node_modules/pi-subagents/`                    | Subagent 扩展 — Agent 发现、执行编排、通信桥接、异步调度 |
| `pi-agent-core`   | `~/.pi/agent/npm/node_modules/@earendil-works/pi-agent-core/`   | Agent 核心抽象（Agent 实例、事件流、工具结果类型）       |
| `pi-ai`           | `~/.pi/agent/npm/node_modules/@earendil-works/pi-ai/`           | AI 层 — 模型调用、流式响应、Token 计算、Context 溢出处理 |
| `pi-tui`          | `~/.pi/agent/npm/node_modules/@earendil-works/pi-tui/`          | TUI 渲染层（Ink-based）                                  |

### 1.1 关键源码文件索引

```
pi-coding-agent/
├── dist/core/agent-session.js          # AgentSession 类 — 生命周期、事件订阅、Session 持久化
├── dist/core/system-prompt.js          # buildSystemPrompt() — 动态系统提示词构建
├── dist/core/skills.js                 # Skill 发现、格式化、注入
├── dist/core/tools/index.js            # 核心工具定义（read/bash/edit/write/grep/find/ls）
├── dist/core/extensions/               # 扩展系统（ExtensionRunner、工具包装器）
├── dist/core/compaction/               # Context 压缩（分支摘要、自动压缩）
├── dist/core/session-manager.js        # Session 文件管理、分支创建
└── docs/                               # 官方文档

pi-subagents/
├── src/agents/agents.ts                # Agent 发现与配置（discoverAgents、loadAgentsFromDir）
├── src/agents/agent-management.ts      # Agent CRUD 管理动作
├── src/intercom/intercom-bridge.ts     # Intercom 通信桥接 — 核心通信机制
├── src/intercom/result-intercom.ts     # 结果回传 Intercom 事件
├── src/shared/fork-context.ts          # Fork 上下文解析器
├── src/shared/types.ts                 # 全量类型定义（SubagentState、ControlEvent 等）
├── src/runs/foreground/subagent-executor.ts  # 前台执行器（SINGLE/CHAIN/PARALLEL）
├── src/runs/background/subagent-runner.ts    # 后台异步执行器
├── src/runs/shared/subagent-control.ts       # 控制事件（needs_attention / active_long_running）
├── src/runs/shared/worktree.ts         # Git worktree 隔离
├── src/runs/shared/pi-spawn.ts         # pi 子进程启动
├── src/runs/shared/pi-args.ts          # pi CLI 参数构建
└── agents/                             # 内置 Subagent 定义（.md 文件）
```

---

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              User（用户）                                │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        Main Agent（主 Agent）                            │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌────────────────┐ │
│  │ read        │  │ bash        │  │ edit        │  │ subagent       │ │
│  │ bash        │  │ edit        │  │ write       │  │ （委派工具）    │ │
│  │ web_reader  │  │ grep/find   │  │ lsp_*       │  │                │ │
│  │ mcp         │  │ ast_grep_*  │  │             │  │                │ │
│  └─────────────┘  └─────────────┘  └─────────────┘  └────────────────┘ │
│                                                                         │
│  • 系统提示词：动态构建（骨架 + 项目上下文 + skills + 运行时信息）          │
│  • 职责：用户对话入口、总调度、subagent 生命周期管理                       │
│  • 唯一持有 `subagent` 工具的实体                                         │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
                    ▼               ▼               ▼
            ┌──────────┐   ┌──────────┐   ┌──────────┐
            │ SINGLE   │   │ CHAIN    │   │ PARALLEL │
            │ 单任务   │   │ 顺序管道  │   │ 并发执行  │
            └────┬─────┘   └────┬─────┘   └────┬─────┘
                 │              │              │
                 ▼              ▼              ▼
            ┌──────────────────────────────────────────┐
            │         Subagent 实例（pi 子进程）          │
            │  • 无 `subagent` 工具 → 无法再委派          │
            │  • 通过 intercom/contact_supervisor 通信    │
            │  • 只能与父 Agent（调用者）通信              │
            └──────────────────────────────────────────┘
```

### 2.1 核心设计原则

| 原则           | 说明                                                               |
| -------------- | ------------------------------------------------------------------ |
| **星型拓扑**   | 所有 subagent 通信必须经过主 agent，subagent 之间不能直接通信      |
| **单层委派**   | Subagent 默认没有 `subagent` 工具，不能创建自己的 subagent         |
| **深度限制**   | 默认 `maxSubagentDepth = 2`，通过环境变量 `PI_SUBAGENT_DEPTH` 控制 |
| **上下文隔离** | `fresh`（全新）或 `fork`（继承父会话历史）两种模式                 |
| **进程隔离**   | 每个 subagent 是独立的 pi CLI 子进程，拥有独立的 session 文件      |

---

## 3. Agent 类型与定义

### 3.1 Agent 来源层级

pi 的 Agent 发现采用三层覆盖机制（后者覆盖前者）：

```
1. builtin    → ~/.pi/agent/npm/node_modules/pi-subagents/agents/
2. user       → ~/.agents/  或  ~/.pi/agent/agents/
3. project    → <project>/.pi/agents/  或  <project>/.agents/
```

通过 `settings.json` 中的 `subagents.agentOverrides` 可覆盖任意 builtin agent 的属性。

### 3.2 Agent 配置格式（YAML Frontmatter）

每个 agent 是一个 `.md` 文件，格式如下：

```markdown
---
name: worker
description: Implementation agent for normal tasks
tools: read, grep, find, ls, bash, edit, write, contact_supervisor
thinking: high
systemPromptMode: replace
inheritProjectContext: true
inheritSkills: false
defaultContext: fork
output: result.md
defaultReads: context.md, plan.md
defaultProgress: true
---

You are `worker`: the implementation subagent...
```

### 3.3 内置 Subagent（8 个）

| Agent               | 职责                                                    | 默认上下文 | Thinking | 关键工具                                                             |
| ------------------- | ------------------------------------------------------- | ---------- | -------- | -------------------------------------------------------------------- |
| **context-builder** | 需求分析 + 代码库侦察，产出 context.md + meta-prompt.md | fresh      | medium   | read, grep, find, ls, bash, write, web_search, intercom              |
| **scout**           | 快速代码库侦察，产出压缩上下文                          | fresh      | low      | read, grep, find, ls, bash, write, intercom                          |
| **planner**         | 将需求转为具体实现计划                                  | fork       | high     | read, grep, find, ls, write, intercom                                |
| **researcher**      | 网络调研，生成研究报告                                  | fresh      | medium   | read, write, web_search, fetch_content, get_search_content, intercom |
| **worker**          | 代码实现（核心写手）                                    | fork       | high     | read, grep, find, ls, bash, edit, write, contact_supervisor          |
| **reviewer**        | 审查代码/计划/方案/PR                                   | fresh      | high     | read, grep, find, ls, bash, edit, write, intercom                    |
| **oracle**          | 决策一致性校验，防止漂移                                | fork       | high     | read, grep, find, ls, bash, intercom                                 |
| **delegate**        | 轻量委托，无默认 reads                                  | fresh      | —        | read, grep, find, ls, bash, edit, write, contact_supervisor          |

---

## 4. 执行模式详解

### 4.1 执行模式对比

| 模式         | 调用方式                                    | 执行方式                       | 适用场景       |
| ------------ | ------------------------------------------- | ------------------------------ | -------------- |
| **SINGLE**   | `subagent({ agent, task })`                 | 单 agent 同步执行              | 单一任务       |
| **CHAIN**    | `subagent({ chain: [{agent, task}, ...] })` | 顺序管道，上一步输出注入下一步 | 多阶段流水线   |
| **PARALLEL** | `subagent({ tasks: [{agent, task}, ...] })` | 并发执行，结果聚合             | 独立任务并行   |
| **ASYNC**    | `subagent({ ..., async: true })`            | 后台子进程，非阻塞             | 长时间运行任务 |

### 4.2 CHAIN 模式数据流

```
Step 1 (scout)
  → output: "context.md 内容"
       ↓
Step 2 (planner) — task 中的 {previous} 被替换为 Step 1 的输出
  → output: "plan.md 内容"
       ↓
Step 3 (worker) — task 中的 {previous} 被替换为 Step 2 的输出
  → output: "实现结果"
```

CHAIN 支持并行子步骤（`parallel: [...]`），每个并行组可独立设置 `concurrency` 和 `worktree`。

### 4.3 PARALLEL 模式数据流

```
                    ┌→ Task A (agent: reviewer)
Main Agent 发起并行  ┼→ Task B (agent: reviewer)
                    └→ Task C (agent: reviewer)

结果聚合后返回主 Agent
```

限制：

- 最大并行任务数：`MAX_PARALLEL = 8`
- 默认并发度：`MAX_CONCURRENCY = 4`

### 4.4 ASYNC 模式

```
Main Agent
  → 启动 async 子进程（spawn pi CLI）
  → 立即返回 asyncId + asyncDir
  → 可通过 action: "status"/"interrupt"/"resume" 管理

Async Runner (独立进程)
  → 写入 status.json + events.jsonl
  → 主 Agent 轮询或事件监听获取状态
```

---

## 5. 通信机制（Intercom）

### 5.1 通信拓扑 — 严格星型

```
                        Main Agent
                           │
          ┌────────────────┼────────────────┐
          │                │                │
    Subagent A       Subagent B       Subagent C
          │                │                │
          └────────────────┼────────────────┘
                           │
                        ❌ 不能直接通信

Subagent A → intercom → Main Agent → 决定是否转发给 Subagent B
```

### 5.2 通信工具对比

| 工具                 | 方向              | 用途                          | 使用方                                    |
| -------------------- | ----------------- | ----------------------------- | ----------------------------------------- |
| `intercom`           | subagent → parent | 通用消息传递、提问、状态更新  | 拥有 `intercom` 工具的 subagent           |
| `contact_supervisor` | subagent → parent | 阻塞式上报（需要决策/被卡住） | 拥有 `contact_supervisor` 工具的 subagent |

### 5.3 Intercom Bridge 注入机制

当 subagent 的 `context` 为 `fork`（或 bridge mode 为 `always`）时，pi 会自动在 subagent 的系统提示词末尾注入 **Intercom Bridge 指令**：

```
Intercom orchestration channel:
The inherited thread is reference-only. Do not continue that conversation...

Use contact_supervisor first. It resolves the supervisor session "{orchestratorTarget}"...
- Need a decision, blocked, approval...: contact_supervisor({ reason: "need_decision", message: "<question>" })
- After contact_supervisor with reason "need_decision", stay alive and continue only after the reply arrives.
- Meaningful progress or unexpected discoveries: contact_supervisor({ reason: "progress_update", message: "UPDATE: <summary>" })
- Generic intercom is lower-level plumbing/fallback only: intercom({ action: "ask", to: "{orchestratorTarget}", message: "<question>" })

Do not use contact_supervisor or intercom for routine completion handoffs.
```

### 5.4 Intercom 目标解析

| 函数                                                   | 输出示例                     | 说明                        |
| ------------------------------------------------------ | ---------------------------- | --------------------------- |
| `resolveIntercomSessionTarget(sessionName, sessionId)` | `subagent-chat-abc123de`     | 主 Agent 的 intercom 接收端 |
| `resolveSubagentIntercomTarget(runId, agent, index)`   | `subagent-worker-a1b2c3d4-1` | Subagent 的 intercom 标识   |

### 5.5 通信约束总结

| 能力                    | Main Agent         | Subagent                             |
| ----------------------- | ------------------ | ------------------------------------ |
| 能否派发 subagent？     | ✅ `subagent` 工具 | ❌ 无 `subagent` 工具                |
| 能否向 parent 发消息？  | ✅ 接收方          | ✅ `intercom` / `contact_supervisor` |
| 能否向 sibling 发消息？ | N/A（中心节点）    | ❌ 禁止，星型拓扑                    |
| 能否广播？              | ✅ 可逐个派发      | ❌ 不能                              |

---

## 6. 上下文管理

### 6.1 上下文模式

| 模式      | 说明                          | 实现机制                                       | 适用场景                                    |
| --------- | ----------------------------- | ---------------------------------------------- | ------------------------------------------- |
| **fresh** | 全新会话，不继承父 Agent 历史 | 创建空 session 文件                            | 独立任务（researcher、reviewer）            |
| **fork**  | 继承父 Agent 的完整对话历史   | `SessionManager.createBranchedSession(leafId)` | 需要上下文的任务（worker、planner、oracle） |

### 6.2 Fork 上下文实现

```typescript
// src/shared/fork-context.ts
function createForkContextResolver(sessionManager, requestedContext) {
  if (requestedContext !== "fork")
    return { sessionFileForIndex: () => undefined };

  const parentSessionFile = sessionManager.getSessionFile();
  const leafId = sessionManager.getLeafId();

  return {
    sessionFileForIndex(index = 0) {
      const sourceManager = openSession(parentSessionFile);
      const sessionFile = sourceManager.createBranchedSession(leafId);
      return sessionFile;
    },
  };
}
```

Fork 后的 subagent 系统提示词会额外注入 **Fork Preamble**：

```
You are a delegated subagent running from a fork of the parent session.
Treat the inherited conversation as reference-only context, not a live thread to continue.
Do not continue or answer prior messages as if they are waiting for a reply.
Your sole job is to execute the task below and return a focused result for that task using your tools.
```

### 6.3 深度限制

```typescript
// 默认最大深度
const DEFAULT_SUBAGENT_MAX_DEPTH = 2;

// 环境变量控制
PI_SUBAGENT_DEPTH = 0; // 主 Agent
PI_SUBAGENT_DEPTH = 1; // 第一层 subagent
PI_SUBAGENT_DEPTH = 2; // 第二层 subagent（默认阻止再委派）
```

---

## 7. 工具设施

### 7.1 核心工具（pi-coding-agent 内置）

| 工具    | 源码文件                   | 功能                                   |
| ------- | -------------------------- | -------------------------------------- |
| `read`  | `dist/core/tools/read.js`  | 读取文件/图片，支持 offset/limit       |
| `bash`  | `dist/core/tools/bash.js`  | 执行 bash 命令，支持 timeout           |
| `edit`  | `dist/core/tools/edit.js`  | 精确文本替换（多 edit 一次调用）       |
| `write` | `dist/core/tools/write.js` | 创建/覆盖文件，自动创建父目录          |
| `grep`  | `dist/core/tools/grep.js`  | AST-aware 代码搜索（ast_grep_search）  |
| `find`  | `dist/core/tools/find.js`  | AST-aware 代码替换（ast_grep_replace） |
| `ls`    | `dist/core/tools/ls.js`    | 列出目录内容                           |

### 7.2 扩展工具

| 工具                   | 来源              | 功能                          |
| ---------------------- | ----------------- | ----------------------------- |
| `web_reader_webReader` | pi-coding-agent   | 网页抓取转 markdown           |
| `mcp`                  | pi-coding-agent   | MCP 网关，连接外部 MCP 服务器 |
| `subagent`             | pi-subagents 扩展 | Subagent 委派与管理           |
| `ast_grep_search`      | pi-coding-agent   | AST 模式搜索                  |
| `ast_grep_replace`     | pi-coding-agent   | AST 模式替换                  |
| `lsp_diagnostics`      | pi-coding-agent   | LSP 诊断                      |
| `lsp_navigation`       | pi-coding-agent   | LSP 代码导航                  |

### 7.3 通信工具（由 Intercom Bridge 注入）

| 工具                 | 注入条件                                        | 功能                    |
| -------------------- | ----------------------------------------------- | ----------------------- |
| `intercom`           | bridge.active && extensionSandboxAllowsIntercom | 通用 agent 间消息       |
| `contact_supervisor` | bridge.active && extensionSandboxAllowsIntercom | 阻塞式上报给 supervisor |

---

## 8. 控制与监控（Control System）

### 8.1 控制事件类型

| 事件类型              | 触发条件                     | 默认阈值                               |
| --------------------- | ---------------------------- | -------------------------------------- |
| `active_long_running` | Subagent 运行时间过长        | `activeNoticeAfterMs = 240000` (4min)  |
| `needs_attention`     | Subagent 无活动/工具反复失败 | `needsAttentionAfterMs = 60000` (1min) |

### 8.2 通知渠道

| 渠道       | 说明                             |
| ---------- | -------------------------------- |
| `event`    | 通过 Extension EventBus 发射事件 |
| `async`    | 异步通知（用于 async 模式）      |
| `intercom` | 通过 Intercom 发送给主 Agent     |

### 8.3 控制配置

```typescript
interface ControlConfig {
  enabled?: boolean; // 默认 true
  needsAttentionAfterMs?: number; // 默认 60000
  activeNoticeAfterMs?: number; // 默认 240000
  activeNoticeAfterTurns?: number; // 可选
  activeNoticeAfterTokens?: number; // 可选
  failedToolAttemptsBeforeAttention?: number; // 默认 3
  notifyOn?: ControlEventType[]; // 默认 ["active_long_running", "needs_attention"]
  notifyChannels?: ControlNotificationChannel[]; // 默认 ["event", "async", "intercom"]
}
```

### 8.4 前台运行状态追踪

```typescript
interface ForegroundControl {
  runId: string;
  mode: "single" | "parallel" | "chain";
  startedAt: number;
  updatedAt: number;
  currentAgent?: string;
  currentIndex?: number;
  currentActivityState?: ActivityState;
  lastActivityAt?: number;
  currentTool?: string;
  currentToolStartedAt?: number;
  currentPath?: string;
  turnCount?: number;
  tokens?: number;
  toolCount?: number;
  interrupt?: () => boolean; // 可中断
}
```

---

## 9. 系统提示词构建流程

### 9.1 主 Agent 系统提示词

```
buildSystemPrompt(options)
  ├── 1. 骨架模板（static）
  │     "You are an expert coding assistant operating inside pi..."
  │     + Available tools（动态列出）
  │     + Guidelines（动态生成）
  │     + Pi documentation reference
  │
  ├── 2. appendSystemPrompt（可选扩展追加）
  │
  ├── 3. Project Context（项目级指令注入）
  │     读取 AGENTS.md / CLAUDE.md / GEMINI.md 等
  │
  ├── 4. Skills（可用技能列表）
  │     通过 formatSkillsForPrompt() 格式化注入
  │
  └── 5. 运行时信息
        Current date: YYYY-MM-DD
        Current working directory: <cwd>
```

### 9.2 Subagent 系统提示词

```
加载 agent.md
  ├── 1. YAML Frontmatter 解析（metadata）
  │     name, description, tools, model, thinking, systemPromptMode, ...
  │
  ├── 2. Body → systemPrompt
  │
  ├── 3. 应用 Builtin Override（settings.json 中的覆盖）
  │
  ├── 4. 应用 Intercom Bridge（如果是 fork 上下文或 always 模式）
  │     追加 intercom/contact_supervisor 工具 + bridge 指令
  │
  └── 5. 启动 pi CLI 子进程时注入
        --system-prompt-mode <append|replace>
        --system-prompt <内容>
```

---

## 10. 隔离机制

### 10.1 Git Worktree 隔离

PARALLEL 模式支持 `worktree: true`，为每个并行任务创建独立的 git worktree：

```typescript
// src/runs/shared/worktree.ts
createWorktrees(cwd, runId, taskCount, {
  agents: ["worker", "worker", "reviewer"],
  setupHook: { hookPath, timeoutMs },
});
```

每个 worktree：

- 独立的文件系统视图
- 独立的 git 状态
- 任务结束后可 diff 合并

### 10.2 Session 文件隔离

每个 subagent 拥有独立的 session `.jsonl` 文件：

- **fresh**: 新建空 session
- **fork**: 从父 session 分支（`createBranchedSession`）
- **async**: 在 `~/.pi/agent/sessions/` 或临时目录创建

### 10.3 环境变量隔离

Subagent 子进程继承并覆盖以下环境变量：

```bash
PI_SUBAGENT_DEPTH=<父深度 + 1>
PI_SUBAGENT_MAX_DEPTH=<配置的最大深度>
```

---

## 11. 扩展系统（Extensions）

### 11.1 扩展注册

pi 通过 `ExtensionRunner` 管理扩展：

```
ExtensionRunner
  ├── 工具钩子：beforeToolCall / afterToolCall
  ├── 事件发射：emitToolCall / emitToolResult
  └── 扩展加载：从 ~/.pi/agent/extensions/ 或 npm 包加载
```

### 11.2 pi-subagents 作为扩展

pi-subagents 本身是 pi 的一个扩展包，注册为 `subagent` 工具：

```typescript
// pi-subagents 扩展注册流程
1. pi 启动时扫描 extensions/
2. 加载 pi-subagents 扩展
3. 扩展注册 `subagent` 工具到 AgentSession._toolDefinitions
4. 主 Agent 调用 `subagent` 时 → 路由到 pi-subagents 的 subagent-executor.ts
```

---

## 12. 关键数据流时序图

### 12.1 SINGLE 模式执行流程

```
User → Main Agent: "用 worker 实现这个功能"
       Main Agent → subagent 工具: { agent: "worker", task: "..." }
                    subagent-executor.ts:
                      1. discoverAgents() — 发现可用 agent
                      2. resolveIntercomBridge() — 解析通信桥
                      3. applyIntercomBridgeToAgent() — 注入通信工具
                      4. buildPiArgs() — 构建 pi CLI 参数
                      5. spawn pi CLI 子进程

                      Subagent 进程:
                        ├── 加载 worker.md
                        ├── 注入 intercom bridge 指令
                        ├── 执行 task（read/edit/write...）
                        ├── 如需决策 → contact_supervisor() → 等待主 Agent 回复
                        └── 返回结果

                      6. 收集 stdout/stderr
                      7. 解析 exitCode / output / error
                      8. 返回 AgentToolResult 给 Main Agent
       Main Agent → User: 展示结果
```

### 12.2 CHAIN 模式执行流程

```
User → Main Agent: "执行分析→计划→实现流水线"
       Main Agent → subagent 工具: { chain: [
         { agent: "scout", task: "侦察代码库" },
         { agent: "planner", task: "基于{previous}制定计划" },
         { agent: "worker", task: "基于{previous}实现代码" }
       ]}
                    subagent-executor.ts:
                      1. 顺序执行每个 step
                      2. 第 N 步的输出 → 替换第 N+1 步 task 中的 {previous}
                      3. 支持并行子步骤（parallel groups）
                      4. 每步完成后可选择 clarify UI（确认/修改）
                      5. 最终聚合结果返回
```

---

## 13. 设计约束与最佳实践

### 13.1 硬约束

| 约束                              | 来源                      | 说明                                                  |
| --------------------------------- | ------------------------- | ----------------------------------------------------- |
| Subagent 不能再委派               | `checkSubagentDepth()`    | 默认 maxDepth=2，subagent 进程 PI_SUBAGENT_DEPTH 递增 |
| Subagent 不能互相通信             | Intercom Bridge 设计      | 星型拓扑，所有消息路由经过主 Agent                    |
| 通信工具只在 fork/always 模式注入 | `resolveIntercomBridge()` | fresh 模式默认不注入 intercom                         |
| 最大并行 8 任务                   | `MAX_PARALLEL`            | 超过则报错                                            |
| 最大并发度 4                      | `MAX_CONCURRENCY`         | 同时运行的子进程数                                    |

### 13.2 推荐模式

| 场景                | 推荐模式     | 推荐 Agent                          |
| ------------------- | ------------ | ----------------------------------- |
| 需求分析 + 代码侦察 | SINGLE/CHAIN | context-builder → scout             |
| 制定实现计划        | SINGLE       | planner                             |
| 并行审查多个文件    | PARALLEL     | reviewer                            |
| 长时间运行任务      | ASYNC        | worker                              |
| 决策校验            | SINGLE       | oracle                              |
| 流水线执行          | CHAIN        | scout → planner → worker → reviewer |

---

## 14. 与 ModexAgent 的映射关系

本项目的 ModexAgent 框架与 pi 的 multi-agent 设计有诸多对应关系：

| pi 概念        | ModexAgent 对应                               | 差异                                         |
| -------------- | --------------------------------------------- | -------------------------------------------- |
| Main Agent     | `AgentRuntime` + 主 Agent                     | pi 是交互式 CLI，ModexAgent 是框架           |
| Subagent       | `subagent` 工具 / `AgentPool`                 | ModexAgent 有内置 AgentPool，pi 通过扩展实现 |
| Intercom       | `AgentMessageBus` + `InboxProducer/Consumer`  | 两者都是星型拓扑                             |
| Fork Context   | `fork` 上下文                                 | 相同设计                                     |
| CHAIN          | `Chain` 执行模式                              | 相同设计                                     |
| PARALLEL       | `Parallel` 执行模式                           | 相同设计                                     |
| Control System | `ControlDrainInterceptor` + `ApprovalRuntime` | ModexAgent 更复杂的拦截器体系                |
| Session        | `SessionManager` / `TurnStateStore`           | ModexAgent 有更细粒度的状态管理              |
| Skills         | `SkillManager`                                | 两者都支持动态 skill 注入                    |

---

## 15. 附录：关键常量

```typescript
// pi-subagents/src/shared/types.ts
const MAX_PARALLEL = 8;
const MAX_CONCURRENCY = 4;
const DEFAULT_SUBAGENT_MAX_DEPTH = 2;
const POLL_INTERVAL_MS = 250;
const MAX_WIDGET_JOBS = 4;

const DEFAULT_MAX_OUTPUT = {
  bytes: 200 * 1024, // 200KB
  lines: 5000,
};

const DEFAULT_ARTIFACT_CONFIG = {
  enabled: true,
  includeInput: true,
  includeOutput: true,
  includeJsonl: false,
  includeMetadata: true,
  cleanupDays: 7,
};

const TEMP_ROOT_DIR = path.join(
  os.tmpdir(),
  `pi-subagents-${resolveTempScopeId()}`,
);
const RESULTS_DIR = path.join(TEMP_ROOT_DIR, "async-subagent-results");
const ASYNC_DIR = path.join(TEMP_ROOT_DIR, "async-subagent-runs");
const CHAIN_RUNS_DIR = path.join(TEMP_ROOT_DIR, "chain-runs");
```

---

## 16. 附录：目录结构速查

```
# pi 安装根目录
~/.pi/
├── agent/
│   ├── npm/node_modules/
│   │   ├── @earendil-works/pi-coding-agent/      # 主 Agent
│   │   ├── @earendil-works/pi-agent-core/        # Agent 核心
│   │   ├── @earendil-works/pi-ai/                # AI 层
│   │   ├── @earendil-works/pi-tui/               # TUI 层
│   │   └── pi-subagents/                         # Subagent 扩展
│   │       ├── agents/                           # 内置 subagent .md
│   │       ├── src/
│   │       │   ├── agents/                       # Agent 发现/配置
│   │       │   ├── intercom/                     # 通信桥接
│   │       │   ├── runs/                         # 执行器
│   │       │   │   ├── foreground/               # 前台执行
│   │       │   │   ├── background/               # 后台执行
│   │       │   │   └── shared/                   # 共享工具
│   │       │   └── shared/                       # 类型/工具
│   │       └── prompts/                          # 提示词模板
│   ├── extensions/                               # 第三方扩展
│   ├── sessions/                                 # Session 文件
│   └── settings.json                             # 用户配置
├── agents/                                       # 用户自定义 agent（旧路径）
└── intercom/
    └── config.json                               # Intercom 配置

# 项目级配置
<project>/
├── .pi/
│   ├── agents/                                   # 项目级 agent
│   ├── chains/                                   # 项目级 chain
│   └── settings.json                             # 项目级设置（覆盖用户级）
└── .agents/                                      # 兼容旧路径

# 用户级 skill
~/.agents/skills/
└── <skill-name>/
    └── SKILL.md
```

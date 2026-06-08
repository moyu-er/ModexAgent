<p align="center">
  <img src="assets/logo-wordmark-dark.svg" alt="ModexAgent" width="380">
</p>

<p align="center">
  <a href="README.md">English</a> |
  <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-%E2%89%A53.12-blue" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
</p>

<p align="center">
  <strong>模块化、可组合、生产级的 Python Agent 框架</strong>
  <br>
  图驱动 ReAct · 可中断审批 · 跨平台终端 · 多 Agent 星型拓扑
</p>

<p align="center">
  <img src="assets/modexagent-intro-zh.gif" alt="ModexAgent 介绍" width="720">
</p>

ModexAgent 是一个用于构建 AI Agent 应用的 Python 框架。它将模型推理、工具调用、记忆管理、输入输出适配器和多 Agent 协作拆分为可独立演进的模块。你可以从一个最简单的 ReAct Agent 起步，逐步扩展为具备长期记忆、多 Agent 协作和运行时治理的完整应用。

框架核心采用**图驱动的执行引擎**替代传统循环，支持执行中途挂起、审批和恢复；运行时提供 **Pipeline / Pool 双模式**，覆盖从单 Bot 到多 Agent 常驻协作的全场景。整体设计借鉴了 OpenClaw 的插件化架构思想，同时针对类型安全、跨平台终端交互和 Agent 间通信做了深度定制。

> [!NOTE]
> 项目处于积极开发阶段，核心接口已趋于稳定，`examples/bot_project/` 提供了覆盖框架绝大多数能力的完整示例。

## 亮点展示

| 可中断审批 | 跨平台交互终端 | 多 Agent 协作 |
|:---:|:---:|:---:|
| ![审批](assets/approval.jpg) | ![终端](assets/self_deployment.png) | ![多Agent](assets/office_subagent.jpg) |
| 敏感工具调用自动挂起，四级分级策略、级联取消 | WinPTY/pexpect/tmux 统一接口，SSH、多 Tab、可见/后台双模式 | 星型拓扑子 Agent，支持同步唤醒、异步 Inbox 和隔离调用 |

## 核心特性

- **图驱动的 ReAct 引擎** — 执行循环以 `Graph[R] + Node[R] + Edge` 的泛型图结构建模，支持 `GraphInterrupt` 挂起与状态持久化恢复，天然适合审批和断点续跑场景。
- **可中断的审批系统** — Agent 调用敏感工具时，执行流自动挂起，通过 `TurnSnapshot` 持久化状态，用户确认后精确恢复。支持 Tiered 分级策略（NORMAL / HARDLINE / PENDING）和级联取消。
- **跨平台交互式终端** — 内置完整终端工具链，支持 Windows（WinPTY/ConPTY）、Linux/macOS（pexpect/tmux）三端统一接口；支持可见终端窗口与后台 PTY 两种模式，248+ 单元测试覆盖。
- **星型拓扑多 Agent 协作** — 主 Agent 作为通信中枢，子 Agent 通过 `send_to_agent`（同步）、`send_to_agent_async`（异步 inbox）、`spawn_subagent`（隔离调用）三种方式协作；`CommunicationTracker` 防止记忆压缩静默丢弃待处理通信。
- **双模式运行时** — `Pipeline` 模式适合单 Agent 长运行服务（QQ Bot、CLI）；`Pool` 模式支持多 Agent 常驻池，通过 `MessageBroker` + `AgentMessageBus` 路由消息，Input/Output 与 Agent 逻辑完全解耦。
- **四级记忆 + Dream 引擎** — Short-term（短期会话）、Archive（历史归档）、Knowledge（长期知识，SOUL.md/USER.md/MEMORY.md）、UserRetentionBuffer（用户保留缓冲，防止治理过度压缩）四层架构；Dream Engine 离线整合历史归档为长期知识。
- **Hook + Interceptor 扩展体系** — 生命周期 Hook（如 InboxFlush、SubagentAutoSend、ProgressReport）与 AOP 拦截器链（ControlDrain、ToolResultLimit）正交组合，框架行为可逐层定制，不侵入核心代码。
- **类型安全** — 63 个 ABC + 18 个 Protocol，枚举替代原始字符串，`from __future__ import annotations` 全仓覆盖，mypy strict 级别检查。
- **MCP 原生集成** — 动态加载 MCP 服务器（SSE/stdio），`MCPToolAdapter` 自动将 MCP 能力映射为框架 Tool 对象，支持工具、资源、Prompt 三类能力。

## 架构概览

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     外部平台（QQ / CLI / HTTP / WebSocket）               │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
           ┌─────────────┐                 ┌─────────────┐
           │InputAdapter │                 │OutputAdapter│
           └──────┬──────┘                 └──────▲──────┘
                  │                                  │
                  ▼                                  │
           ┌─────────────────────────────────────────┐
           │           AgentPipeline                 │
           │  ┌─────────┐  ┌─────────┐  ┌─────────┐ │
           │  │Context  │→ │ ReAct   │→ │  Tool   │ │
           │  │Manager  │  │ Agent   │  │Manager  │ │
           │  │(记忆系统)│  │(图引擎) │  │(工具执行)│ │
           │  └─────────┘  └────┬────┘  └─────────┘ │
           │                    │                   │
           │  Hooks / Interceptors / Control / Approval│
           └────────────────────┼─────────────────────┘
                                │
                                ▼
                       ┌─────────────┐
                       │ MessageBroker│
                       └──────┬──────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        ┌─────────┐    ┌─────────┐    ┌─────────┐
        │AgentPool│    │Subagent │    │ Inbox   │
        │(常驻Agent)│   │Manager  │    │Server   │
        └─────────┘    └─────────┘    └─────────┘
```

## 快速开始

### 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)（推荐）或 pip

### 安装

```bash
git clone git@github.com:moyu-er/ModexAgent.git
cd ModexAgent

# 创建虚拟环境
uv venv --python 3.12

# Windows PowerShell
.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate

# 安装完整依赖
uv pip install -e ".[all,dev]"
```

### 运行示例

```bash
cd examples/bot_project
cp .env.example .env
# 编辑 .env 填写 QQ_APP_ID、LLM_API_KEY 等

# Pool 模式（默认，多 Agent 协作）
python bot_service.py

# Pipeline 模式（单 Agent）
python bot_service.py --mode pipeline
```

> [!TIP]
> `examples/bot_project/` 是一个功能完整的 QQ Bot 示例。详细说明见 [examples/bot_project/README.zh-CN.md](examples/bot_project/README.zh-CN.md)。

## 项目结构

```text
framework/
  core/              # 核心抽象：Agent、Context、Emitter、Provider、Tool 等
  agents/react/      # ReAct Agent 图引擎实现
  pipeline/          # 端到端流程编排
  memory/            # 四级记忆系统 + Dream 引擎 + 治理
  tools/             # 工具注册、执行、终端系统、MCP 适配器
  multi_agent/       # 多 Agent 协作：Pool、MessageBus
  hook/              # 生命周期 Hook 扩展点
  interceptor/       # AOP 拦截器链
  control/           # 运行时控制、审批、事件总线
  commands/          # Slash 指令系统
  sandbox/           # 沙箱适配器（Subprocess / Docker / E2B）
  security/          # 安全策略与审批分类器
  providers/         # LLM 提供者（LiteLLM、OpenAI 兼容接口）
  ioc/               # 类型化配置（Pydantic v2）与工厂层
  runtime/           # 运行时状态存储、快照、编解码

examples/
  bot_project/       # 完整 QQ Bot 示例（Pipeline + Pool 双模式）
  sandbox/           # 沙箱相关示例

tests/               # 单元、集成和端到端测试
docs/                # 框架文档
```

## 按需安装

| Extra     | 包含内容                                                                     | 适用场景          |
| --------- | ---------------------------------------------------------------------------- | ----------------- |
| `llm`     | `litellm`, `openai`                                                          | LLM Provider      |
| `storage` | `faiss-cpu`, `chromadb`, `sentence-transformers`                             | 向量存储与语义记忆 |
| `session` | `sqlalchemy[asyncio]`                                                        | 会话持久化        |
| `sandbox` | `docker`, `e2b-code-interpreter`                                             | 沙箱执行          |
| `gateway` | `qq-botpy`, `aiohttp`                                                        | QQ Bot 适配器     |
| `skills`  | `pypdf`, `python-docx`, `openpyxl`, `python-pptx`, `pdfplumber`             | 文档处理技能      |
| `terminal`| `pywinpty`（Win）、`pexpect` + `libtmux`（Unix）                             | 交互式终端        |
| `dev`     | `pytest`, `pytest-asyncio`, `ruff`, `mypy`                                   | 开发测试          |
| `all`     | 以上全部（除 `dev`）                                                          | 一键完整安装      |

```bash
# 示例：仅安装框架核心 + LLM 支持
uv pip install -e ".[llm]"

# 示例：完整开发环境
uv pip install -e ".[all,dev]"
```

## 文档

| 文档 | 说明 |
| --- | --- |
| [架构设计](docs/architecture.md) | 框架整体架构与设计决策 |
| [核心模块](docs/core-modules.md) | Agent、Tool、Memory、Pipeline 核心概念 |
| [记忆系统](docs/memory-system.md) | 四级记忆、Dream 引擎、治理系统 |
| [多 Agent 指南](docs/multi-agent-guide.md) | 星型拓扑、通信工具、子 Agent 生命周期 |
| [扩展指南](docs/extension-guide.md) | Hook、Interceptor、Plugin、Slash 指令开发 |
| [Bot 指南](docs/bot-guide.md) | bot_project 示例项目详解 |
| [运行时设计](docs/current-runtime.md) | ReAct 运行时设计、控制流、审批流 |

## 开发命令

```bash
pytest tests/unit/ -v
pytest tests/integration/ -v -m integration

ruff check framework tests
ruff format framework
mypy framework
```

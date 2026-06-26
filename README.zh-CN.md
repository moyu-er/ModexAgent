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
  图驱动 ReAct · 可中断审批 · 跨平台终端 · 多 Agent 星型拓扑 · WebUI
</p>

<p align="center">
  <img src="assets/modexagent-intro-zh.gif" alt="ModexAgent 介绍" width="720">
</p>

ModexAgent 是一个用于构建 AI Agent 应用的 Python 框架。它将模型推理、工具调用、记忆管理、输入输出适配器和多 Agent 协作拆分为可独立演进的模块。你可以从一个最简单的 ReAct Agent 起步，逐步扩展为具备长期记忆、多 Agent 协作、运行时治理和浏览器 WebUI 的完整应用。

框架核心采用**图驱动的执行引擎**替代传统循环，支持执行中途挂起、审批和恢复；运行时采用 **Pool 模式** — 多 Agent 常驻池，通过 `MessageBroker` + `AgentMessageBus` 路由消息，I/O 适配器与 Agent 逻辑完全解耦。`examples/bot_project/` 包含一个完整的 **React + WebSocket WebUI**，支持实时流式渲染和多会话管理。整体设计借鉴了 OpenClaw 的插件化架构思想，同时针对类型安全、跨平台终端交互和 Agent 间通信做了深度定制。

> [!NOTE]
> 项目处于积极开发阶段，核心接口已趋于稳定，`examples/bot_project/` 提供了覆盖框架绝大多数能力的完整示例，包括 WebUI 前端。

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
- **Pool 运行时** — 多 Agent 常驻池，通过 `MessageBroker` + `AgentMessageBus` 路由消息，I/O 适配器与 Agent 逻辑完全解耦。
- **多级记忆 + 自学习系统** — Session、Archive、Knowledge、UserRetentionBuffer、Pruned、Experience 六层记忆，支持 SessionScope / UserScope / GlobalScope 可配置隔离范围。Dream Engine 定期将 Archive 整合为 Knowledge；ExperienceReviewAgent 将对话沉淀为可复用的 EXPERIENCE.md 参考知识。
- **Hook + Interceptor 扩展体系** — 生命周期 Hook（如 InboxFlush、SubagentAutoSend）与 AOP 拦截器链（ControlDrain、ToolResultLimit）正交组合，框架行为可逐层定制，不侵入核心代码。
- **类型安全** — 全部使用 ABC 接口（零 Protocol），枚举替代原始字符串，`from __future__ import annotations` 全仓覆盖，mypy strict 级别检查。
- **MCP 原生集成** — 动态加载 MCP 服务器（SSE/stdio），`MCPToolAdapter` 自动将 MCP 能力映射为框架 Tool 对象，支持工具、资源、Prompt 三类能力。
- **浏览器 WebUI** — React + Vite 前端，实时流式渲染，多会话侧边栏，工作区浏览器，Pool 选择器（见 `examples/bot_project/`）。

## 架构概览

```
┌─────────────────────────────────────────────────────────────────────────┐
│              外部平台（QQ / CLI / HTTP / WebSocket → WebUI）               │
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

### 一键安装（推荐）

无需系统 Python、pip 或 npm —— 引导脚本自动处理一切：

```bash
git clone git@github.com:moyu-er/ModexAgent.git
cd ModexAgent\examples\bot_project

# Windows（双击或在任意终端中运行）
install.bat

# Linux / macOS
chmod +x install.sh && ./install.sh
```

脚本会自动检测并安装缺失的运行时（Node.js、uv），创建 Python 虚拟环境，安装全部依赖，复制 `.env.example` → `.env`，运行配置向导，编译 WebUI 前端，并将 `modexbot` 注册到系统 PATH。重复运行安全——每一步都是幂等的。

脚本完成后：

```bash
# 可在任意目录执行，无需激活 venv
modexbot start
```

然后浏览器访问 `http://localhost:21800/webui/`。

常用命令：`modexbot stop` | `modexbot logs -f` | `modexbot install -f` | `modexbot config`

> [!TIP]
> `examples/bot_project/` 是一个功能完整的 QQ Bot + WebUI 示例。详细能力、配置和多 Agent 设置见 [examples/bot_project/README.zh-CN.md](examples/bot_project/README.zh-CN.md)。

### 手动配置

如需逐步手动设置：

```bash
git clone git@github.com:moyu-er/ModexAgent.git
cd ModexAgent

# 创建虚拟环境（uv 自动下载 Python 3.12）
uv venv --python 3.12

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 安装框架
uv pip install -e ".[all,dev]"

# 安装 bot 项目（注册 'modexbot' CLI）
# 保持 venv 激活状态，或使用 --python 显式指定路径
cd examples\bot_project
uv pip install -e ".[webui,dev]"

# 配置环境并构建前端
cp .env.example .env
modexbot install    # 配置向导 + WebUI 前端构建
modexbot start
```

## 项目结构

```text
src/modex_agent/        # 框架包（src layout，见 ADR-0003）
  core/              # 根：Agent/Context/Emitter/Provider/Tool ABC、图引擎、类型、常量
  agents/            # Agent 运行时：ReAct（图驱动）、Summarizer、ExperienceReview
  pipeline/          # 端到端流程编排（AgentPipeline）
  memory/            # 多级记忆 + Dream 引擎 + 上下文治理
  multi_agent/       # 星型协作：Pool、broker、inbox、通信
  tools/             # 工具注册 + 执行；终端、MCP、AST、LSP、web 工具集
  providers/         # LLM 提供者（LiteLLM、OpenAI 兼容接口）
  hook/              # 生命周期 Hook 扩展点（InboxFlush、SubagentAutoSend）
  interceptor/       # AOP 拦截器链（ControlDrain、ToolResultLimit、…）
  control/           # 运行时控制传输（/stop + 暂停通道）
  approval/          # 分级审批策略与分类器
  commands/          # Slash 指令系统
  input_pipeline/    # 用户输入的通用阶段式管线
  adapters/          # I/O 适配器基类——将平台 I/O 与 Agent 逻辑解耦
  messaging/         # 消息 broker 抽象层
  workspace/         # Workspace 机制：多活隔离、按 pool 的资源
  runtime/           # 运行时状态存储、快照、编解码
  sandbox/           # 沙箱适配器（Subprocess / Landlock / Docker / E2B）+ 安全 guard
  ioc/               # 类型化配置（Pydantic v2）+ 工厂层
  plugins/           # 插件系统
  registry/          # 注册表
  trace/             # 统一的操作级 trace 系统
  utils/             # 根邻接的纯叶子原语（ADR-0006：不依赖任何其他包）

examples/
  bot_project/       # 完整 QQ Bot + WebUI 示例（Pool 模式）
  sandbox/           # 沙箱使用示例

tests/               # 单元、集成和端到端测试
docs/                # ADR + 架构文档
```

## 按需安装

| Extra     | 包含内容                                                                     | 适用场景          |
| --------- | ---------------------------------------------------------------------------- | ----------------- |
| `llm`     | `litellm`, `openai`                                                          | LLM Provider      |
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
| [ADR 索引](docs/adr/) | 架构决策记录（pool-only 装配、src-layout 改名、依赖树、facade-only、保留真实 seam） |
| [CONTEXT.md](CONTEXT.md) | 领域术语表——Pool、Workspace、ReAct Agent、Graph、GraphInterrupt、Assembly 等 |
| [Bot 示例](examples/bot_project/README.md) | bot_project 详解（QQ Bot + WebUI、多 Agent 配置） |
| 各模块 `AGENTS.md` | `src/modex_agent/` 下每个包都附带 `AGENTS.md`，描述其职责与关键文件 |

## 开发命令

```bash
pytest tests/unit/ -v
pytest tests/integration/ -v -m integration

ruff check src/modex_agent tests
ruff format src/modex_agent
mypy src/modex_agent
```

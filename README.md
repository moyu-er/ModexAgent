# ModexAgent

ModexAgent 是一个用于搭建 AI Agent 应用的 Python 框架。它把模型、工具、记忆、输入输出适配器、插件和多 Agent 协作拆成可以组合的模块，让你可以从一个简单 Agent 开始，逐步扩展成更完整的应用。

项目仍处在早期开发阶段，接口和文档会继续调整。当前最完整的用法参考是 `examples/bot_project/`，但它只是一个示例项目，不是框架本身。

## 适合做什么

- 构建一个可以调用工具的 ReAct Agent。
- 接入不同的输入输出方式，例如 CLI、HTTP、QQ Bot 或自定义平台。
- 给 Agent 增加短期记忆、历史记忆、长期记忆和上下文压缩。
- 通过 hook、interceptor、plugin 扩展运行过程。
- 组织多个 Agent 协作，包括主 Agent、子 Agent、peer Agent 和消息路由。
- 在工具执行时加入安全策略、沙箱、审批和运行时控制。

## 核心概念

| 概念 | 作用 |
|------|------|
| `Agent` | 负责推理和决策，例如 ReAct Agent。 |
| `Tool` / `ToolManager` | 注册和执行工具。 |
| `Memory` | 管理会话、历史、长期记忆和上下文压缩。 |
| `InputAdapter` / `OutputAdapter` | 把外部平台接入框架，例如命令行、HTTP、QQ。 |
| `AgentPipeline` | 串起输入、上下文、Agent、输出的完整流程。 |
| `Hook` | 在生命周期节点观察或调整上下文。 |
| `Interceptor` | 包裹工具、模型流、回合等执行边界。 |
| `Control` | 运行时控制通道，用于取消、审批、注入指令等流程控制。 |
| `Plugin` | 以插件形式扩展工具、记忆、hook 或其他能力。 |

## 快速开始

### 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

### 安装

```bash
git clone git@github.com:moyu-er/ModexAgent.git
cd ModexAgent

uv venv

# Windows
.venv\Scripts\activate

# macOS / Linux
# source .venv/bin/activate

uv pip install -e ".[all]"
```

只安装开发依赖：

```bash
uv pip install -e ".[dev]"
```

## 示例项目：bot_project

`examples/bot_project/` 是一个用 ModexAgent 搭出来的 QQ Bot 示例。它覆盖了很多框架能力，包括：

- 单 Agent pipeline 模式；
- AgentPool 和消息 broker 模式；
- ReAct Agent；
- 工具调用；
- 记忆系统；
- 插件；
- 多 Agent 协作；
- 审批、控制和运行时状态示例。

它的作用是展示“如何使用框架搭一个完整应用”。不要把它理解成框架唯一推荐形态；你可以只使用其中很小一部分能力。

运行示例：

```bash
cd examples/bot_project
cp .env.example .env
```

编辑 `.env`，填入模型和平台配置：

```bash
LLM_API_KEY=your_api_key
LLM_BASE_URL=https://api.example.com/v1
LLM_MODEL=openai/your-model

# 如果要接 QQ Bot，再配置平台凭据
QQ_APP_ID=your_app_id
QQ_SECRET=your_secret
```

启动：

```bash
# 单 Agent pipeline
python bot_service.py --mode pipeline

# AgentPool + broker，多 Agent 示例
python bot_service.py --mode pool
```

更多说明见 [examples/bot_project/README.md](examples/bot_project/README.md)。

## 项目结构

```text
framework/
  core/              核心抽象：Agent、Context、Emitter、Provider、Tool 等
  agents/react/      ReAct Agent 实现
  pipeline/          输入、处理、输出编排
  memory/            记忆系统
  tools/             工具注册、执行和标准工具
  hook/              生命周期扩展点
  interceptor/       执行边界拦截器
  control/           运行时控制、事件和状态存储
  multi_agent/       多 Agent 协作
  messaging/         消息 broker 和路由
  plugins/           插件系统
  sandbox/           沙箱适配器
  security/          安全策略
  extensions/        可选扩展，例如 LiteLLM、Chroma、FAISS、SQLAlchemy

examples/
  bot_project/       功能较完整的 Bot 示例项目
  sandbox/           沙箱相关示例

docs/                框架文档
tests/               单元、集成和端到端测试
```

## 文档入口

| 文档 | 内容 |
|------|------|
| [docs/architecture.md](docs/architecture.md) | 框架整体架构。 |
| [docs/core-modules.md](docs/core-modules.md) | 核心模块说明。 |
| [docs/memory-system.md](docs/memory-system.md) | 记忆系统说明。 |
| [docs/multi-agent-guide.md](docs/multi-agent-guide.md) | 多 Agent 协作说明。 |
| [docs/extension-guide.md](docs/extension-guide.md) | 扩展和插件开发说明。 |
| [docs/bot-guide.md](docs/bot-guide.md) | Bot 示例使用说明。 |
| [docs/current-runtime.md](docs/current-runtime.md) | 当前 ReAct、hook、interceptor、control 的运行时设计说明。 |

## 开发命令

```bash
pytest
pytest tests/unit
pytest -m "not integration"

ruff check framework tests
ruff format framework
mypy framework
```

## 当前状态

这个仓库目前更像一个框架工作台：核心能力已经拆分成模块，`bot_project` 展示了较完整的组合方式，但部分高级能力仍在演进中。新用户建议先从示例运行和核心模块文档开始，再阅读运行时设计文档。

## License

MIT

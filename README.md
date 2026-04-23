# Agent Framework

一个轻量级、可扩展的多 Agent 框架，支持 ReAct 推理循环、工具系统、多层记忆管理、Agent 间通信与协作。

## 核心特性

- **ReAct 推理循环** — Thought → Action → Observation 迭代执行，支持流式输出
- **多层记忆系统** — Working / Short-term / History Archive / Long-term 四层架构，支持独立存储后端和压缩策略
- **多 Agent 协作** — Agent Pool 常驻模式 + SubagentManager 动态派生模式，支持跨 Agent 消息路由
- **工具系统** — 内置工具注册表、并行执行调度、MCP 协议客户端集成
- **Pipeline 编排** — InputAdapter → Agent → Emitter → OutputAdapter 端到端流程
- **安全沙箱** — 支持 Subprocess / Landlock / Docker / E2B 多种隔离适配器
- **插件系统** — 基于约定的插件发现机制，支持注入工具、Hook 和记忆提供者
- **多平台接入** — 通过 Adapter 机制对接 QQ Bot、CLI、HTTP API 等任意平台

## 环境要求

- Python >= 3.11
- [uv](https://docs.astral.sh/uv/) 包管理器（推荐）

## 快速开始

### 1. 安装 uv

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

安装完成后验证：

```bash
uv --version
```

### 2. 安装 Python 3.11

uv 可以自动管理 Python 版本，无需手动安装。项目根目录的 `.python-version` 文件指定了 `3.11`。

```bash
# 查看 uv 已管理的 Python 版本
uv python list

# 如果列表中没有 cpython-3.11.x，手动安装
uv python install 3.11
```

### 3. 创建虚拟环境并安装依赖

在项目根目录执行：

```bash
# 创建虚拟环境（使用 .python-version 指定的 3.11）
uv venv

# 激活虚拟环境
# Windows (PowerShell):
.venv\Scripts\activate
# macOS / Linux:
# source .venv/bin/activate

# 以可编辑模式安装项目及全部可选依赖（推荐）
uv pip install -e ".[all]"

# 如果只需要核心依赖 + LLM：
# uv pip install -e ".[llm]"
```

也可以使用 `uv sync`（需要先 `uv lock` 生成 lockfile）：

```bash
uv sync --extra all
```

也可以使用 pip：

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量（Bot Project 示例）

```bash
cd examples/bot_project
cp .env.example .env
```

编辑 `.env`，填入必要的配置：

```bash
# LLM — 兼容任意 OpenAI 格式 API（MiniMax / OpenAI / Anthropic / Kimi 等）
LLM_API_KEY=your_api_key
LLM_BASE_URL=https://api.minimaxi.com/v1
LLM_MODEL=openai/MiniMax-M2.5

# QQ Bot（如需接入 QQ 平台）
QQ_APP_ID=your_app_id
QQ_SECRET=your_secret

# MCP 服务器认证（可选）
MCP_BEARER_TOKEN=your_token
```

### 4. 启动

```bash
cd examples/bot_project

# Pool 模式（多 Agent 常驻协作）
python bot_service.py --mode pool
```

所有运行时配置集中在 `config/bot_config.yml`，通过 `${ENV_VAR}` 语法从 `.env` 读取密钥，无需改代码即可切换 LLM 供应商。

#### 使用截图

![使用截图](./assets/qq_bot.jpg)


## License

MIT

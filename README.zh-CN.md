<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-wordmark-dark.svg">
    <img src="assets/logo-wordmark.svg" alt="ModexAgent" width="380">
  </picture>
</p>

<p align="center">
  <strong>你的 Agent，你的工作方式。</strong><br>
  可自由组合的 Python Agent 框架，附带开箱可用的浏览器工作台。
</p>

<p align="center">
  <a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.12%2B-3776AB" alt="Python 3.12 及以上"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-2DD4A8" alt="MIT 许可证"></a>
  <a href="https://github.com/moyu-er/ModexAgent/releases"><img src="https://img.shields.io/badge/Status-Active%20development-64748B" alt="积极开发中"></a>
</p>

<p align="center">
  <a href="#开始使用">开始使用</a> ·
  <a href="#按你的方式定制">自由定制</a> ·
  <a href="docs/user-guide/workflows.zh-CN.md">工作流</a> ·
  <a href="#使用文档">使用文档</a>
</p>

ModexAgent 将模型、工具、记忆和多 Agent 协作整合为可按需组合的框架。无论是构建编码助手、组织研究团队，还是串联编码与审查流程，都不必从头搭建配套的运行时。

想先用起来，再逐步定制？仓库自带的 **ModexBot** 将这些能力带到浏览器中：对话、工作区、Agent 团队、工具执行记录和配置管理，集中在同一个工作台。它既是可直接使用的应用，也是构建你自己应用的起点。

<p align="center">
  <img src="assets/modexagent-intro.gif" alt="ModexAgent 项目与能力动态介绍" width="960">
  <br><sub>快速了解 ModexAgent。</sub>
</p>

## 从一次对话到一套工作流

**让 Agent 真正动手。** 读取与编辑文件、检索项目、执行命令、连接 MCP 服务。为不同角色选择模型与工具集，再通过提示词和 Skills 补充领域知识。

**让专业角色协作。** 定义主 Agent 和协作者，分派目标明确的任务，并随时查看子会话。团队可以跨 Pool 通信；需要更明确的步骤时，也可以用图工作流组织编码、审查与修订。

**让上下文延续。** 持久会话承接连续工作；可选的归档与核心记忆保留长期上下文，Experience 能力则可将经过回顾的交互沉淀为可复用的经验。

**看清执行，保留控制。** 跟随流式回复，展开工具参数与结果，管理附件，或暂停正在执行的轮次。需要进一步控制时，可以为原生 Agent 配置人工审批和沙箱策略。

**选择适合的入口。** 在浏览器中工作，接入 QQ 或 Telegram，或将 OpenCode 纳入同一套工作区与会话体验。外部 Agent 仍由自身运行时管理模型、上下文、工具与权限行为。

## 按你的方式定制

自定义不是 ModexBot 的附加功能。应用本身就通过插件向框架提供行为，你的项目也可以使用这些扩展入口。

| 从这里开始 | 使用方式 | 适合定制什么 |
| --- | --- | --- |
| **配置** | WebUI 设置与 YAML Agent 声明 | 模型、角色、工具集、协作者、记忆与权限 |
| **提示词与 Skills** | Markdown 指令与 `SKILL.md` 技能包 | 领域知识、可复用流程与任务指导 |
| **MCP** | 注册服务，按原生 Agent 选择挂载 | 浏览器工具与已有集成 |
| **Python 插件** | 工具、Hook、Provider、拦截器等 | 新增行为，或替换内置实现 |
| **Capabilities** | 将工具、Hook、提示词片段与共享资源打包 | 按 Agent 一起启用的完整能力 |

在 ModexBot 中，项目插件放在 `examples/bot_project/plugins/`，启动时自动发现。注册工具或 Hook，在 Agent 声明中选用，重启即可生效。基于框架开发时，可以配置自己的插件发现方式；应用通道和图节点也提供各自的扩展接口。

**[编写第一个插件 →](docs/user-guide/extensions.zh-CN.md)** · [配置一个 Agent](docs/user-guide/configuration.zh-CN.md)

## 开始使用

### 运行 ModexBot

**Windows：** 从 [Releases](https://github.com/moyu-er/ModexAgent/releases/latest) 下载 `ModexBot-Setup-*.exe`。安装包包含 Python 与 WebUI。已发布的开发版本是特定时间的快照，可能与当前源码有所不同；前置条件与首次启动说明见[安装指南](docs/user-guide/getting-started.zh-CN.md)。

**从源码运行：** 克隆仓库，再执行对应平台的安装脚本。源码安装通过 `uv` 使用 Python 3.12，并用 Node.js/npm 构建 WebUI；脚本可以协助安装缺失工具。

```bash
git clone https://github.com/moyu-er/ModexAgent.git
cd ModexAgent/examples/bot_project
```

macOS / Linux：

```bash
bash install.sh
```

Windows PowerShell：

```powershell
.\install.bat
```

> [!IMPORTANT]
> 请在可信机器与网络中使用工作台。当前源码默认监听 `0.0.0.0`，且 WebUI 没有身份认证。本地使用时，请在启动前将 `config/bot_config.yml` 中的 `webui.host` 设为 `127.0.0.1`。详见[本地部署与安全设置](docs/user-guide/getting-started.zh-CN.md#只在本机开放-webui)。

如果安装修改了 `PATH`，请打开新终端，再配置模型并启动：

```bash
modexbot config
modexbot start
```

打开 **[localhost:21800/webui/](http://localhost:21800/webui/)**，选择 Pool，开始对话。也可以在 **Settings → Models** 中配置模型。模型访问需要你自己的服务配置；可选的 MCP 服务和 OpenCode 另有前置条件。

**[完整安装指南 →](docs/user-guide/getting-started.zh-CN.md)** · [尝试一个工作流](docs/user-guide/workflows.zh-CN.md)

### 基于框架开发

ModexBot 是可参考的完整集成，而不是你的应用必须依赖的 UI。你可以复用 Python 运行时与扩展接口，提供自己的工具和适配器，按需装配能力。

[扩展指南](docs/user-guide/extensions.zh-CN.md) 从配置定制讲到完整的 Python 工具插件。若只需要框架，可按[手动安装说明](docs/user-guide/getting-started.zh-CN.md#manual-source-installation)将框架与本地 `modex_graph` 包一起安装，跳过 Bot 安装步骤。

## 使用文档

用户指南面向当前源码，中英文版本覆盖相同主题。

| 指南 | 内容 |
| --- | --- |
| [开始使用](docs/user-guide/getting-started.zh-CN.md) | 安装、模型配置、CLI、本地安全与故障排查 |
| [配置指南](docs/user-guide/configuration.zh-CN.md) | Agent 声明、工具、记忆、MCP、Skills 与权限 |
| [工作流](docs/user-guide/workflows.zh-CN.md) | 工作区、Agent 团队、图运行、审批与 OpenCode |
| [扩展开发](docs/user-guide/extensions.zh-CN.md) | 第一个插件、组件替换、Capabilities、通道与图节点 |

## 项目组成

| 部分 | 职责 |
| --- | --- |
| [`modex_agent`](src/modex_agent/) | Agent 运行时、会话管理、工具、记忆、协作与扩展接口 |
| [`modex_graph`](src/modex_graph/) | 独立图执行引擎，支撑原生 ReAct 与工作流编排 |
| [`bot_project`](examples/bot_project/) | ModexBot 应用、WebUI、通道适配器、项目插件与示例配置 |

声明经过验证和编译后再装配组件。原生与外部执行共享面向应用的路由和输出接口，同时保留各自的执行职责。

更深入的工程背景可参阅[架构决策](docs/adr/)、[领域术语](CONTEXT.md)和 [Capability 编写指南](docs/design/capability-bundles/AUTHOR-GUIDE.md)。

> [!NOTE]
> ModexAgent 正在积极开发中，API 与配置可能演进，请使用与你运行版本对应的文档。

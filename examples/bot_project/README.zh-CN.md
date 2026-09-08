<p align="center"><img src="../../assets/logo-wordmark-dark.svg" alt="ModexAgent" width="300"></p>

<p align="center"><strong>ModexBot：ModexAgent 官方可定制参考应用</strong></p>

<p align="center"><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a></p>

<p align="center"><img src="../../assets/webui-multiagent.png" alt="展示多 Agent 协作的 ModexBot WebUI" width="900"></p>
<p align="center"><sub>当前源码版本的界面示例；打包发布版本可能有所不同。</sub></p>

ModexBot 不是一个固定演示，而是框架官方提供、可直接运行的应用开发起点。你可以直接使用随附的 WebUI 与消息渠道，也可以定制提示词、Pool、Skill、工具、插件、适配器和 Graph 工作流，构建自己的多 Agent 助手。

## 内置能力

- React WebUI：流式对话、Workspace、会话树、工具轨迹、原生 Agent 可配置的人工审批、模型选择与 Graph 执行。
- 常驻 Agent Pool：根 Agent、subagent、跨 Pool 协作、记忆与可复用经验。
- 基于统一 Adapter 边界的 QQ 与 Telegram 渠道；只使用 WebUI 无需 IM 凭据。
- 内置工具、MCP 集成、项目插件、Skills 与声明式 Graph 工作流。

## 启动

请选择以下两种安装来源之一；权威步骤与故障排查统一维护在用户指南中。

| 来源 | 适用场景 | 操作指南 |
| --- | --- | --- |
| Windows Release 安装包 | 最快完成打包安装 | [Windows Release 安装包](../../docs/user-guide/getting-started.zh-CN.md#windows-release-安装包) |
| 源码检出 | 应用开发和体验当前源码 | [从源码安装](../../docs/user-guide/getting-started.zh-CN.md#从源码安装) |

### 源码快速开始

在本目录（`examples/bot_project`）中运行对应平台的引导脚本：

```bash
bash install.sh
```

```powershell
.\install.bat
```

配置模型提供商、模型、API 地址与 API Key：

```bash
modexbot config
```

完成模型配置后，`config/model.yml` 会以明文保存 API Key。请只在本地保管，不要提交或分享；密钥一旦泄露应立即轮换。

> [!IMPORTANT]
> 启动前，请在 `config/bot_config.yml` 中把 `webui.host` 绑定为 `127.0.0.1`。当前源码默认值是 `0.0.0.0`，且 WebUI 没有认证层。绝不要把 `21800` 端口暴露到公网。

```yaml
webui:
  port: 21800
  host: "127.0.0.1"
```

然后启动应用并打开 <http://localhost:21800/webui/>：

```bash
modexbot start
modexbot status
```

## 定制应用

日常修改建议先使用 WebUI **Settings**。开发自己的应用变体或需要版本管理时，使用以下目录。

| 路径 | 用途 |
| --- | --- |
| `plugins/` | 项目 Python 插件与组件注册 |
| `config/` | 运行时、模型、IM 与 MCP 配置 |
| `config/scopes/` | Workspace、Pool、Agent、工具与 Capability 声明 |
| `agents/` | Markdown 系统提示词 |
| `skills/`、`local_skills/` | 每 Agent 的 Skill 分配与项目 Skill 库 |
| `bot/` | 应用服务、Pipeline、Workspace 装配与业务逻辑 |
| `bot/adapters/` | QQ、Telegram、WebSocket 与自定义渠道适配器 |
| `webui/` | React、TypeScript、Vite 与 Tailwind 前端 |
| `config/graphs/` | 声明式 Graph 工作流规格 |

## 开发 WebUI

从本目录安装前端依赖：

```bash
cd webui
npm ci
```

保持 Bot 后端运行，然后启动 Vite 开发服务器：

```bash
npm run dev
```

在另一个终端中，或停止开发服务器后，再运行构建和测试：

```bash
npm run build
npm test -- --run
```

`npm ci` 用于安装锁定版本的依赖；`dev`、`build` 和 `test` 脚本与 `webui/package.json` 一致。前端文件导航见 [`webui/AGENTS.md`](webui/AGENTS.md)，应用架构见 [`AGENTS.md`](AGENTS.md)。

## 完整指南

完整文档只在 `docs/user-guide/` 维护；本 README 不再重复庞大的配置参考。

[入门](../../docs/user-guide/getting-started.zh-CN.md) | [配置](../../docs/user-guide/configuration.zh-CN.md) | [工作流](../../docs/user-guide/workflows.zh-CN.md) | [扩展](../../docs/user-guide/extensions.zh-CN.md)

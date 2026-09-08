[English](getting-started.md) | [简体中文](getting-started.zh-CN.md) | [README](../../README.zh-CN.md)

# ModexBot 入门

ModexBot 是基于 ModexAgent 构建、可直接使用的 WebUI 应用。本指南将带你完成安装、首次本地对话，并介绍日常会用到的命令。

> [!NOTE]
> Windows Release 资源使用 `ModexBot-Setup-*.exe` 这一名称。**v1.0.0-dev** 是已发布的开发版快照，并不表示所有功能都已稳定。请到 [Releases](https://github.com/moyu-er/ModexAgent/releases/latest) 查看当前最新资源；当前源码可能已经包含任一 Release 之后的改动。

## 选择安装方式

| 你的系统或目标 | 推荐方式 |
| --- | --- |
| Windows，希望最快开始 | 安装已发布的 `.exe`，其中已包含 Python 和 WebUI |
| macOS 或 Linux | 克隆仓库并运行 `bash install.sh` |
| 在 Windows 上开发 | 克隆仓库并运行 `.\install.bat` |
| 希望使用最新源码行为 | 在任一支持的平台上使用源码安装 |

## Windows Release 安装包

安装包内置 Python 运行时、Python 依赖和预构建前端，因此通常无需另行安装 Python 或 Node.js。其 Tauri 桌面壳需要 Microsoft Edge WebView2 Runtime；Windows 10 1803 及更高版本通常已自带。如果缺失，请安装 [WebView2 Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/#download-section)，或使用浏览器快捷方式。

1. 打开[最新 Releases 页面](https://github.com/moyu-er/ModexAgent/releases/latest)。
2. 下载名为 `ModexBot-Setup-*.exe` 的 Windows 资源。
3. 双击文件并按向导完成安装。它按用户安装，通常不需要管理员权限。
4. 从桌面或开始菜单启动 **ModexBot**。
5. 打开 **设置 → 模型**，填写模型提供商、模型名称、API 地址和 API Key。

WebView2 可用时，桌面启动器会自动打开 WebUI。开始菜单中还提供浏览器启动、配置、日志和停止快捷方式。

Release 是已打包的时间点快照，因此界面和配置字段可能与当前仓库文档不同。如果你明确需要当前源码行为，请使用下面的源码安装方式。

## 从源码安装

### 前提条件

| 要求 | 用途 |
| --- | --- |
| [Git](https://git-scm.com/) | 克隆仓库 |
| [uv](https://docs.astral.sh/uv/) | 安装和管理 Python 与项目包 |
| Python 3.12 | 必需的运行时；安装脚本会让 `uv` 下载并管理它 |
| 带 npm 的 [Node.js](https://nodejs.org/) | 构建浏览器 WebUI |

只要 `uv` 能安装 Python 3.12，就不需要单独准备系统 Python。Node.js 仅在纯后端模式下可选；要使用本指南中的 WebUI，就必须安装它。

先克隆仓库：

```bash
git clone https://github.com/moyu-er/ModexAgent.git
cd ModexAgent
```

### macOS 或 Linux

```bash
cd examples/bot_project
bash install.sh
```

脚本可以协助安装缺失的 `uv` 和 Node.js，在仓库根目录用 Python 3.12 创建 `.venv`，安装框架与 Bot，构建 WebUI，并询问是否把 CLI 加入 `PATH`。

### Windows 源码安装

在克隆后的仓库中打开 PowerShell，运行：

```powershell
cd examples\bot_project
.\install.bat
```

批处理脚本会使用 Windows 路径完成相同设置。它可以通过 WinGet 安装缺失工具，并询问是否把 `.venv\Scripts` 加入用户 `PATH`。

两个脚本都可以安全地重复运行。如果设置完成后暂时找不到 `modexbot`，请打开一个新终端，让更新后的 `PATH` 生效。

## 配置模型

运行下面任一命令；它们当前会打开同一个多提供商模型配置向导：

```bash
modexbot config
# 等价的模型向导：
modexbot model
```

你也可以在没有模型时先启动，再到 WebUI 的 **设置 → 模型** 中配置。通过 CLI 修改当前提供商或模型后，请重启 Bot，让运行时路由采用新配置。

在源码检出中，向导会写入 `examples/bot_project/config/model.yml`。此文件会以明文形式保存 API Key 等秘密值。

> [!IMPORTANT]
> 请把 `model.yml` 当作密钥文件：不要提交到版本库，不要粘贴到 Issue，不要作为支持附件上传，也不要让它出现在截图中。建议使用权限受限的 API Key；一旦泄露，请立即轮换。

## 只在本机开放 WebUI

当前源码配置默认监听 `0.0.0.0`，这会接受所有网络接口上的连接，而 WebUI 当前没有认证层。不要把 `21800` 端口暴露到公网。

一般的本机使用场景下，请在启动前编辑 `examples/bot_project/config/bot_config.yml`，让服务只绑定回环地址：

```yaml
webui:
  port: 21800
  host: "127.0.0.1"
```

如果所用 Release 提供该字段，也应通过已安装的配置目录应用同样设置。防火墙规则仍可作为第二层保护。CLI 的 `--port` 只改变端口，不会改变配置中的监听地址。

## 启动并打开 WebUI

```bash
modexbot start
modexbot status
```

`start` 会启动一个脱离终端运行的后台进程。打开：

<http://localhost:21800/webui/>

在聊天框中发送消息。如果还没有配置模型，请打开 **设置 → 模型**，保存提供商和模型，并在提示时重启。

## 常用 CLI 命令

| 命令 | 作用 |
| --- | --- |
| `modexbot config` | 打开全局模型交互式配置向导 |
| `modexbot model` | 打开同一个多提供商模型向导 |
| `modexbot install` | 构建 WebUI；构建结果已是最新时会跳过 |
| `modexbot install -f` | 强制重新构建 WebUI |
| `modexbot start` | 在后台启动 Bot |
| `modexbot status` | 显示进程状态 |
| `modexbot logs` | 显示最近 50 行日志 |
| `modexbot logs -f` | 持续查看新日志；按 Ctrl+C 停止跟随 |
| `modexbot restart` | 停止现有 Bot 并启动新进程 |
| `modexbot stop` | 停止正在运行的 Bot |

使用 `modexbot <command> --help` 查看各命令的具体选项。`start` 和 `restart` 不会重新构建 WebUI；修改前端源码后请运行 `modexbot install`。

## 可选 MCP 服务

首次对话不需要 MCP。`modexbot install` 不会启用 `config/mcp/registry.example.json`，源码引导脚本也会把这项可选设置留给用户。如果需要 MCP 工具，请按[配置 → MCP 服务器](configuration.zh-CN.md#mcp-服务器)创建并检查 `registry.json`；不要直接启用你不信任的服务。

## 从 Node.js 或 WebUI 构建失败中恢复

如果设置过程在没有 Node.js 的情况下继续，或前端构建失败：

1. 安装当前的 Node.js LTS 版本。
2. 打开新终端，让 `node` 和 `npm` 进入 `PATH`。
3. 确认 `node --version` 和 `npm --version` 可以运行。
4. 在 `examples/bot_project` 中运行 `modexbot install -f`。
5. 如果 Bot 已在运行，再执行 `modexbot restart`。

在 macOS 或 Linux 上，npm 的 `EACCES` 或 `EPERM` 错误通常表示 npm 前缀不可写。请按设置脚本的提示改用用户拥有的 npm 前缀；不要反复用 `sudo` 运行构建来绕过问题。

遇到其他启动问题时，运行：

```bash
modexbot status
modexbot logs -f
```

如果 `21800` 端口已被占用，请停止旧的 ModexBot 进程，或始终配合 `--port` 使用另一个端口。

<a id="manual-source-installation"></a>

<details>
<summary>手动源码安装</summary>

不希望使用引导脚本时，可以从仓库根目录手动执行：

```bash
uv python install 3.12
uv venv --python 3.12
```

在 macOS 或 Linux 上激活环境：

```bash
source .venv/bin/activate
```

或者在 Windows PowerShell 中激活：

```powershell
.\.venv\Scripts\Activate.ps1
```

把本地图引擎与根框架一起安装，然后安装 Bot：

```bash
uv pip install -e src/modex_graph -e '.[all]'
uv pip install -e 'examples/bot_project[webui]'
cd examples/bot_project
modexbot install
modexbot config
```

请明确保留 `-e src/modex_graph`。`modex-graph` 是本地同级包；如果只用 pip 风格解析器安装根目录的可编辑项目，解析器可能错误地到 PyPI 查找这个依赖。

运行 `modexbot start` 前，请先按[只在本机开放 WebUI](#只在本机开放-webui)所示，把 `webui.host` 改为 `127.0.0.1`，然后再正常启动 Bot。

</details>

下一步：[配置](configuration.zh-CN.md) | [工作流](workflows.zh-CN.md) | [扩展](extensions.zh-CN.md)

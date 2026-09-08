<p align="center">
  <a href="configuration.md">English</a> |
  <a href="configuration.zh-CN.md">简体中文</a> |
  <a href="../../README.zh-CN.md">根 README</a>
</p>

# 配置

你可以通过 WebUI 或直接编辑文件来配置 ModexBot。WebUI 会校验写入内容，并明确提示是否需要重启，因此最适合作为配置入口。

## 配置根目录

本指南中的所有相对路径都位于 **bot 项目目录** 下；源码检出时通常是 `examples/bot_project/`。它们不相对于聊天中通过 `/cd` 或 WebUI 工作区选择器切换的 workspace，也不是 workspace 插件目录。Bot 提示词、MCP 注册和 Skill 分配都保存在这里。

运行状态默认保存在 `<workspace-target>/.modex/`。你可以通过 `paths.data_dir_name` 修改目录名，主 workspace 声明也可覆盖它。Home workspace 将状态保存在 bot 项目下，其他 workspace 则将自己的状态保存在对应目标目录下。

Home workspace 和普通打开的目录复用 `config/scopes/bot.yml`。通过 **Create Workspace** 创建 workspace 时，会在 bot 项目内生成独立声明 `config/scopes/workspaces/<name>.yml`。当前 Settings 的 **Pools** 和 **Scope** 编辑主 `bot.yml`，不会自动改写动态 workspace 的声明。

## Settings 与文件

| Settings 区域 | 对应文件或目录 | 生效方式 |
| --- | --- | --- |
| 运行基础设施 | `config/bot_config.yml` | 手动编辑；重启 |
| IM Adapters | `config/im.yml` | 保存后重启 |
| Models | `config/model.yml` | 保存后重启 |
| Pools | `config/scopes/bot.yml` | 结构化编辑；重启 |
| Scope | `config/scopes/bot.yml` | Declaration tree、Provenance bill 和 Declaration (YAML)；重启 |
| MCP | `config/mcp/registry.json` | 保存后重启 |
| Prompts | `agents/*.md` | 保存后重启 |
| Skills | `local_skills/<skill>/` 和 `skills/<pool>/<agent>/<skill>/` | 上传与分配实时生效 |

`config/model.yml`、`config/im.yml` 和 `config/mcp/registry.json` 可能包含凭据，参考 bot 已将它们加入 gitignore。请只在本地私密保存这些文件，绝不要提交 API Key、Token、App Secret 或生成的本地配置。

## 推荐的 UI 操作流程

1. 打开 WebUI，进入 **Settings**。
2. 在 **Models** 中添加 provider 和 model，再选择一个确实存在的默认 provider/model 组合。
3. 在 **Prompts** 中创建 Agent 将使用的提示词文件。
4. 在 **Pools** 中添加或编辑 Pool 与 Agent；保存前查看预览和有效工具列表。
5. 在 **Scope** 中，通过 **Declaration tree** 查看结构，通过 **Provenance bill** 查看生效配置，通过 **Declaration (YAML)** 查看源文件。
6. 在 **MCP** 中注册服务器，再到 **Pools** 为各原生 Agent 选择服务器。
7. 在 **Skills** 中上传 Skill 文件夹，并分配给符合条件的原生 Agent。
8. 修改模型、IM、Pool/Scope、MCP 或提示词后，使用 Settings 显示的重启操作。Skill 上传与分配不需要重启。

## 运行配置与模型

`config/bot_config.yml` 保存共享基础设施，而不是 Pool 与 Agent 定义：安全时限、数据路径、WebUI 地址、持久化默认值和可观测性。主 Pool 与 Agent 声明位于 `config/scopes/bot.yml`；通过 **Create Workspace** 创建的 workspace 在 `config/scopes/workspaces/` 下拥有自己的声明文件。

`config/model.yml` 是共享模型注册表。每个 provider 包含自己的连接设置和模型列表；`default_provider` 与 `default_model` 必须指向一个已声明条目。WebUI 读取时会遮蔽秘密字段。模型设置保存后将在重启时生效。

## 创建单 Pool

先通过 **Settings > Prompts > Create** 创建提示词，或直接创建 `agents/assistant.md`。例如：

```markdown
You are a concise project assistant.
Work only from evidence, use tools when useful, and verify results before replying.
```

可将下面这个包含一个根 Agent 的完整单 Pool 声明作为起点：

```yaml
workspace:
  name: bot
  pools:
    custom:
      agents:
        assistant:
          description: A concise assistant for project research and support.
          toolset: read_only
          system_prompt: agents/assistant.md
          capabilities:
            todo: {}
```

如果 `config/scopes/bot.yml` 已存在，**不要直接替换整个文件**。请把 `custom:` 添加到现有 `workspace.pools` 映射下，或使用 **Settings > Pools > Add pool**。随附的图工作流会引用 `coder/orchestrator`、`review/reviewer` 等现有 Agent；如果删除这些声明却不同时更新工作流，启动校验会失败。

`system_prompt` 路径从 bot 项目目录解析。上例显式声明它，便于立即发现缺失文件。省略时默认使用 `agents/<agent-name>.md`；`prompt_name` 可选择其他提示词文件名。当前不支持用户提供的 `config/profiles/` 目录：`toolset` 选择内置 profile，省略时使用位置默认值（根 Agent 为 `full`，子 Agent 为 `read_write`）。

## 工具与 Capabilities

可用的 `toolset` 值为 `full`、`read_write`、`read_only`、`none` 和 `web`。toolset 是工具列表预设，不是安全边界。`read_only` 仍包含 `bash`；如需限制 Shell 写入，请移除 `bash` 或配置沙箱。

`tools` 列表只使用一种模式：

```yaml
tools:
  - +web_search
  - -bash
```

带前缀的形式会在所选 toolset 上增量添加或删除工具。另一种方式是使用全部不带前缀的列表，完整替换 toolset 工具列表：

```yaml
tools:
  - read
  - ls
  - grep
  - glob
  - todo_write
  - todo_read
```

不要混合带前缀和不带前缀的条目。在混合列表中，不带前缀的条目不是 replacement 条目，合并时会被忽略。replacement 列表必须保留已启用 capability 要求的工具；上面的两个 Todo 工具可让前文的 `todo: {}` 声明保持有效。Capability 提供的同名替换（例如 ACI edit 实现）由编译器单独处理；请查看 **Scope > Provenance bill**，不要尝试在 `tools` 中编码 replacement 记录。

`capabilities` 是覆盖映射。`{}` 使用默认配置启用 capability，`false` 将其禁用；`true` 无效。常见示例：

```yaml
capabilities:
  skills: false
  todo: {}
  experience: {}
```

`skills` 会自动应用于原生 Agent，也可显式关闭。`todo` 会成组添加任务工具、Hook、提示词区段和按 session 持久化的 Todo 存储。`experience` 独立按需启用，并打包其工具、复盘 Hook、提示词注入、存储和整理器。外部 Agent 不进入这条原生 capability 路径，因此不能声明非空 capabilities 块。

## 持久化与记忆

持久化后端默认为 `sqlite`，可通过 `config/bot_config.yml` 顶层的 `persistence.backend` 改为 `file`；workspace 声明也可在 `workspace.persistence` 下覆盖同一后端。该设置决定 workspace 运行状态和 session 相关数据的保存方式。

原生 Agent 默认启用 Session 上下文记忆，用于保存当前对话上下文；`memory.session.max_context_tokens` 控制何时开始压缩。Archive 是原生根 Agent 按需启用的历史摘要层。Core Memory 是原生根 Agent 按需启用的持久 Agent 与用户上下文层，并且依赖 Archive。Experience 通过 `capabilities.experience` 独立按需启用，将有用的已完成交互复盘为可复用经验，并提供给后续对话。

下面的 Agent 级示例同时启用 Archive、Core Memory 和 Experience：

```yaml
memory:
  archive_enabled: true
  core_enabled: true
capabilities:
  experience: {}
```

请按用途选择这些功能。即使不启用任何可选功能，Session 上下文仍然可用。Archive/Core 可以不依赖 Experience 单独启用，Experience 也可以不依赖 Archive/Core 单独启用；只有 Core 要求同时启用 Archive。

## MCP 服务器

如果 `registry.json` 尚不存在，可从受版本控制且不含秘密的模板开始。在 macOS 或 Linux 上：

```bash
test -e config/mcp/registry.json || cp config/mcp/registry.example.json config/mcp/registry.json
```

在 Windows PowerShell 上：

```powershell
if (-not (Test-Path "config/mcp/registry.json")) {
  Copy-Item "config/mcp/registry.example.json" "config/mcp/registry.json"
}
```

如果文件已经存在，请使用 **Settings > MCP**，避免覆盖本地值。

当前模板包含下面这个 stdio 服务器：

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp"]
    }
  }
}
```

该条目会启动 `npx`，因此请先安装 Node.js 和 npm。首次运行时，`npx` 可能访问 npm registry、下载软件包并执行其中的代码。只添加你信任的 MCP 软件包和命令，检查 `command` 与 `args`，并在需要可复现安装时固定软件包版本。

注册服务器不会自动把它暴露给 Agent。请为每个**原生** Agent 选择注册名；对于当前主声明，该操作会把 Agent 的 `mcp` 列表写入 `config/scopes/bot.yml`：

```yaml
mcp:
  - playwright
```

修改注册表或 Agent 选择后请重启，以重新建立连接和工具列表。外部 provider 拥有自己的工具，不会使用这项框架原生 MCP 选择。

## Skills

在 **Settings > Skills** 中上传包含 `SKILL.md` 的文件夹；WebUI 会将仓库侧全局库写入 `local_skills/<skill>/`。然后选择 Pool 和 Agent 并执行分配。分配会在 `skills/<pool>/<agent>/<skill>/` 创建链接；Windows 无符号链接权限时会使用目录 junction。

每个 Agent 的目录是分配关系的权威来源；`bot.yml` 中没有 `skills:` 列表。正常运行时，上传、删除、分配和取消分配都会被实时读取。多数其他 Settings 修改需要重启，具体以重启提示条为准。

你也可以把真实的 Skill 副本直接放在 `skills/<pool>/<agent>/<skill>/`。该 Agent 的原生 `skills` capability 必须保持有效。用户安装的 `~/.agents/skills/<skill>/` 会补充全局库；发生同名冲突时，仓库的 `local_skills/` 优先。

## IM 适配器

**Settings > IM Adapters** 会把相互独立的 `qq` 与 `telegram` 区段写入 `config/im.yml`。QQ 在 `app_id` 和 `secret` 均存在时构建。Telegram 在其设置已启用且存在 `token` 时构建；`proxy` 可选。两者均支持 `allow_from`，其中 `"*"` 允许任意发送者。

保存后请重启，以创建或停止适配器。文件应只保留在本地，并通过会遮蔽秘密值的 Settings 字段录入凭据；不要发布包含真实值的示例。

## 审批与 Sandbox

人工审批与沙箱限制彼此独立，均需按需启用。Approval 只能声明在 Pool 根 Agent 上。参考 `bot.yml` 对原生根 Agent 使用了下面这段原样配置：

```yaml
interceptors:
  - +sandbox_guard
interceptor_configs:
  sandbox_guard:
    sandbox:
      backend: host
      exclusive:
        write_surface: workspace
approval:
  enabled: true
```

这只是参考片段，不代表完整隔离保证。`backend: host` 会启用 guard，但不提供内核隔离。沙箱判断覆盖已知的框架原生工具和目标；未知工具没有边界覆盖，外部 provider 工具会绕过框架 `ToolNode`。MCP、Shell 网络、继承的凭据和 provider 侧工具都需要按各自边界单独评估。

使用 **Settings > Pools**，选择根 Agent，再配置 Approval 与沙箱。检查有效工具和 **Scope > Provenance bill**，保存并重启；在依赖该策略前，先分别测试一个无害的 workspace 内操作和 workspace 外操作。

## 重启前检查

可使用 **Pools** 进行引导式编辑，也可通过 **Scope** 直接修改 YAML。Settings 会在保存前检查声明；如果报告错误，请按提示修正对应的 Pool、Agent 或字段后再次保存。无效修改不会替换当前声明。

保存成功后，在 **Scope > Declaration tree** 检查预期的根/子结构，并在 **Scope > Provenance bill** 检查每个 Agent 将获得的工具和 capability。需要对照保存的源文件时，使用 **Declaration (YAML)**。然后通过提示条重启。如果适配器、模型、提示词、MCP 服务器或 Pool 未按预期启动，可查看 `modexbot logs -f`，定位对应名称和配置字段。

---

[快速开始](getting-started.zh-CN.md) · [工作流](workflows.zh-CN.md) · [扩展](extensions.zh-CN.md)

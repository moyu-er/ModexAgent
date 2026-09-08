# 扩展

[English](extensions.md) | [简体中文](extensions.zh-CN.md) | [README](../../README.zh-CN.md)

ModexAgent 的两个层次都可以扩展：可复用框架提供类型化扩展接口，Bot 参考应用使用这些接口，同时为插件和渠道增加了 Bot 专属发现路径。你可以从数据文件开始，只在行为确实需要代码时进入 Python 层。本指南按这个顺序展开，全程不需要修改框架。

## 选择最小的扩展层

| 需求 | 首选方式 |
|---|---|
| 调整模型、记忆、工具或 Agent 组成 | 类型化配置和 scope 声明 |
| 调整角色、策略或领域指令 | Markdown 系统提示词 |
| 增加可复用任务指导 | `SKILL.md` skill |
| 接入已有工具服务器 | MCP |
| 增加运行时行为或新的实现 | Python 插件 |
| 将多个组件作为一个功能交付 | Capability |
| 增加 Bot 传输渠道或声明式 Graph 节点 | Bot 渠道注册表或 Graph `NodeRegistry` |

优先选择能清楚表达需求的最浅层次。配置、提示词和 skill 更容易检查与维护；需要可执行行为时，插件仍然是正式扩展方式。

## 1. 配置、提示词和 skills

Bot 的主 scope 声明位于 `examples/bot_project/config/scopes/bot.yml`。
它定义 Pool、原生与外部 Agent、组件名册、MCP 选择和 Capability 覆盖项。通过 **Create Workspace** 创建的工作区，在 bot 项目的 `config/scopes/workspaces/<name>.yml` 下保存独立声明；通过工作区选择器打开的普通目录则复用主声明。
下面只是一个原生 Agent 的局部片段：

```yaml
system_prompt: agents/assistant.md
tools: ["+hello"]
capabilities:
  todo: {}
  experience: {}
```

请先安装下文的 `hello` 插件，并创建 `agents/assistant.md`。
然后把这些字段合并到现有原生 Agent 中；不要用此片段替换 `bot.yml`。

`system_prompt` 是内置 `file_prompt` provider 的文件路径简写；相对路径从项目目录解析。
省略时默认使用 `agents/<agent-name>.md`；`prompt_name` 可以选择另一个约定名称。只有当插件提供了不同 provider 时，才需要使用 `system_prompt_provider` 和 `system_prompt_provider_config`。

Skills 使用 Agent 级目录：

```text
skills/<pool>/<agent>/<skill>/SKILL.md
```

一个最小 skill 包含 frontmatter 和指导内容：

```markdown
---
name: release-check
description: Verify a release candidate before publishing.
---

# Release check

Run the project's documented tests and report failures before publishing.
```

内置 `skills` Capability 默认可用于每个原生根 Agent 和 subagent，除非该 Agent 声明 `capabilities: {skills: false}`。
当声明树中的位置参与通信拓扑时，内置 `subagents` Capability 会自动启用。`todo` 和 `experience` 是可选启用的示例，可通过上面的配置映射开启。

完整声明面请参阅[配置](configuration.zh-CN.md)，面向 Graph 的组合请参阅[工作流](workflows.zh-CN.md)。

## 2. MCP 服务器

已有服务器能提供所需操作时，优先使用 MCP。
Bot 从 `examples/bot_project/config/mcp/registry.json` 读取全局服务器注册表；WebUI 的 MCP 设置编辑同一个注册表。
stdio 条目的结构如下：

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

在 scope 声明中按 Agent 选择已注册服务器：

```yaml
agents:
  assistant:
    mcp: [playwright]
```

全局注册表定义可用服务器，每个 Agent 的 `mcp` 列表从中选择服务器。框架负责 MCP 连接与工具包装；应用负责提供注册表和 assembly context。
Bot 提供了上面的 JSON/WebUI 约定。生成的工具名采用清洗后的 `<server>_<tool>` 形式。

凭据应放在由环境变量支持的本地配置中，不要提交到仓库。
每个自定义 MCP 服务器都应视为可信集成代码，并具有其进程或远程服务自身的权限。

## 3. Python 工具插件

当配置或 MCP 无法表达行为时，使用插件。
对于 Bot，可以新增 `examples/bot_project/plugins/hello.py`，内容是下面这个完整插件：

```python
from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from modex_agent.core.tool_manager import Tool
from modex_agent.plugins import Plugin, PluginRegistrationContext, PrototypeFactory


class EmptyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HelloTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="hello",
            description="Return a greeting without accessing the host.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name to greet.",
                    }
                },
            },
        )

    async def execute(self, name: str = "world", **_kwargs: str) -> str:
        return f"Hello, {name}!"


class HelloPlugin(Plugin):
    config_model: ClassVar[type[BaseModel]] = EmptyConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_tool(
            "hello",
            PrototypeFactory(HelloTool, EmptyConfig),
        )
```

`PrototypeFactory` 会为每次 assembly 创建新的 `HelloTool`，而不是共享可变的工具状态。这个空 Pydantic 模型被冻结，并拒绝未知配置键。
工具 schema 暴露一个可选字符串，`execute` 只返回字符串；它不读取或写入宿主环境。注册名和面向 LLM 的工具名都是 `hello`。

前面的 scope 片段使用 `tools: ["+hello"]`。`+` 形式把 `hello` 添加到原生 Agent 按位置派生的 toolset 中，而不会替换该 toolset。
相反，不带前缀的 `tools` 列表是完整 roster 声明。

### 发现与优先级

`ComponentRegistryLoader` 可以加载：

- 应用提供的内置 `Plugin` 实例；
- 已配置项目插件目录中的直接 `*.py` 文件；
- 一个可选用户插件目录中的直接 `*.py` 文件；
- 默认位于 `modex_agent.plugins` 组中的已安装包 entry point。

Bot 生产路径把 `examples/bot_project/plugins/` 配置为项目目录，因此会非递归扫描 `examples/bot_project/plugins/*.py`，并跳过 `__init__.py`。Bot 当前没有传入 `user_plugin_path`；框架应用可以通过 `PluginDiscoveryConfig` 配置它。
对于安装包，可让 entry point 指向 `Plugin` 子类，例如 `my_plugin = my_package:MyPlugin`。

跨来源名称冲突按固定优先级处理：

```text
user > project > entry_points > bundled
```

优先级不取决于扫描顺序。同一来源中的重名是启动错误。加载发生在启动注册阶段，不是热插拔：新增或修改 Python 插件后需要重启应用。

Python 插件在应用进程内执行，属于可信代码。仅仅使用插件 API 并不会让它们获得隔离。

## 4. 组件 slots

插件可以注册到 11 个框架 slot：

| 分组 | Slots |
|---|---|
| Agent 组件 | `tool`、`hook`、`memory_system`、`system_prompt_provider` |
| 运行时与 assembly | `llm_provider`、`interceptor`、`command_handler`、`execution_strategy`、`input_stage` |
| 数据与功能包 | `data_namespace`、`capability` |

大多数扩展只需要一个 slot。Factory 声明闭合配置模型，并在 assembly 时创建组件；Capability slot 则直接保存 Capability 实例。
这样，独立框架应用和 Bot 都能使用同一个框架注册表，同时不会把 Bot 业务 wiring 放进框架。

原生 Agent 使用框架的工具、hook、提示词、记忆和 Capability assembly 接口。外部 Agent（例如当前交付的 OpenCode 集成）使用其 provider harness，不消费原生组件 roster；在外部 Agent 上声明非空 `capabilities` 块是无效配置。

## 5. Capabilities

当一个功能的多个部分必须一起启用和 wiring 时创建 Capability；不要为单独一个工具或 hook 创建它。一个包可以协调工具名、hooks、提示词 sections、每 Agent wiring，以及 store、service 或后台 worker 等 pool 共享 supply。

生命周期有意拆分为：

- `applies`、`contribute` 和 `bind` 编译确定性的声明与最终 roster gate；
- `supply` 构造可选的 pool 共享资源；
- `assemble` 返回每 Agent 的提示词 provider 和 wiring artifacts。

在同一个插件中注册 Capability 实例及它引用的 factory：

```python
def register(self, ctx: PluginRegistrationContext) -> None:
    ctx.register_capability("my_feature", MyFeatureCapability())
    ctx.register_tool("my_feature_tool", MyFeatureToolFactory())
    ctx.register_hook("my_feature_hook", MyFeatureHookFactory())
```

随后用户可用 `capabilities: {my_feature: {...}}` 选择启用（无配置项的包使用 `{}`）；如果 Capability 的纯 `applies` 谓词自动启用它，则不必声明。组件级 `+` 和 `-` roster 条目仍会参与最终 gate。

完整实现请阅读 [Capability 作者指南](../design/capability-bundles/AUTHOR-GUIDE.md)。
可运行的 [T-CAP2 集成测试](../../tests/integration/plugins/test_tcap2_third_party_capability.py) 验证了第三方工具、hook、提示词 section 和共享 supply 经过生产 assembly 路径的全过程。

## 6. Bot 渠道和 Graph 节点

Bot 渠道使用独立的应用注册表，而不是框架组件 slot。`WebUIService` 会导入 `examples/bot_project/bot/adapters/register_*.py`；每个模块通过 Bot 的 `@register` 装饰器注册渠道 factory。
实现框架 input/output adapter 接口，添加 `register_<channel>.py`，在本地配置凭据，然后重启 Bot。

声明式 Graph 节点类型也使用独立注册表。在 `modex_graph.NodeRegistry` 中按 `node_type` 注册 `NodeFactory`；`GraphSpecCompiler` 会验证节点配置并创建节点。
对于 Bot，只把节点模块放进 `plugins/` 并不够：还要把 factory 接入 `examples/bot_project/bot/workspace/wiring/resources.py` 创建的应用 `NodeRegistry`，该 registry 随后会传给 `GraphOrchestrator`。
`NodeRegistry` 属于独立 Graph 引擎，不是第 12 个插件 slot。Graph 声明和执行方式请参阅[工作流](workflows.zh-CN.md)。

## 信任与部署检查清单

- 先考虑配置、提示词或 skills，再增加可执行代码。
- 把秘密放在由环境变量支持的本地配置中。
- 按可信代码审查自定义 MCP 服务器和 Python 插件；两种机制都不是安全沙箱。
- 扩展原生 Agent roster 时使用增量式 `+name` 条目。
- 修改 Python 插件、渠道注册或 Graph factory 后重启。
- 测试部署时实际使用的应用发现路径。

接下来可阅读[快速开始](getting-started.zh-CN.md)、[配置](configuration.zh-CN.md)、[工作流](workflows.zh-CN.md)或[项目 README](../../README.zh-CN.md)。

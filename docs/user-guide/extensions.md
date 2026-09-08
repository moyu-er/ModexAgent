# Extensions

[English](extensions.md) | [简体中文](extensions.zh-CN.md) | [README](../../README.md)

ModexAgent is extensible at both layers: the reusable framework provides typed extension interfaces, and the Bot reference application uses them while adding Bot-specific discovery paths for plugins and channels. You can start with data files and move into Python only when the behavior needs code. This guide follows that progression without requiring framework changes.

## Choose the smallest extension

| Need | Start with |
|---|---|
| Change model, memory, tools, or agent composition | Typed configuration and a scope declaration |
| Change role, policy, or domain instructions | A Markdown system prompt |
| Add reusable task guidance | A `SKILL.md` skill |
| Connect an existing tool server | MCP |
| Add runtime behavior or a new implementation | A Python plugin |
| Ship several components as one feature | A Capability |
| Add a Bot transport or declarative graph node | The Bot channel registry or graph `NodeRegistry` |

Prefer the shallowest layer that expresses the requirement clearly. Configuration, prompts, and skills are easier to inspect and maintain; plugins remain available when you need executable behavior.

## 1. Configuration, prompts, and skills

The Bot's primary scope declaration is `examples/bot_project/config/scopes/bot.yml`.
It defines pools, native and external agents, component rosters, MCP selection, and capability overrides. Workspaces created with **Create Workspace** have their own declarations under `config/scopes/workspaces/<name>.yml` in the bot project; ordinary folders opened through the workspace picker reuse the primary declaration.
The following is only a partial native-agent fragment:

```yaml
system_prompt: agents/assistant.md
tools: ["+hello"]
capabilities:
  todo: {}
  experience: {}
```

First install the `hello` plugin shown below and create `agents/assistant.md`.
Then merge these fields into an existing native agent; do not replace `bot.yml` with this fragment.

`system_prompt` is file-path sugar for the bundled `file_prompt` provider; relative paths are resolved from the project directory.
If it is omitted, the convention is `agents/<agent-name>.md`; `prompt_name` can select another conventional name. Use `system_prompt_provider` and `system_prompt_provider_config` only when a plugin supplies a different provider.

Skills are agent-scoped directories:

```text
skills/<pool>/<agent>/<skill>/SKILL.md
```

A minimal skill contains frontmatter and instructions:

```markdown
---
name: release-check
description: Verify a release candidate before publishing.
---

# Release check

Run the project's documented tests and report failures before publishing.
```

The bundled `skills` Capability is available by default to every native root and subagent, unless that agent declares `capabilities: {skills: false}`.
The bundled `subagents` Capability is enabled automatically when the declared tree position participates in the communication topology. `todo` and `experience` are opt-in examples, enabled with configuration mappings as shown above.

See [Configuration](configuration.md) for the complete declaration surface and [Workflows](workflows.md) for graph-oriented composition.

## 2. MCP servers

Use MCP when a server already exposes the actions you need.
The Bot reads its global server registry from `examples/bot_project/config/mcp/registry.json`; the WebUI MCP settings edit the same registry.
A stdio entry follows this shape:

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

Select registered servers per agent in the scope declaration:

```yaml
agents:
  assistant:
    mcp: [playwright]
```

The global registry defines the available servers, and each agent's `mcp` list selects servers from it. The framework owns MCP connection and tool wrappers; an application supplies the registry and assembly context.
The Bot provides the JSON/WebUI convention above. Generated tool names use the sanitized `<server>_<tool>` form.

Put credentials in environment-backed local configuration, not committed files.
Treat every custom MCP server as trusted integration code with the privileges of its process or remote service.

## 3. A Python tool plugin

Use a plugin when configuration or MCP does not express the behavior.
For the Bot, place a module such as `examples/bot_project/plugins/hello.py` containing this complete plugin:

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

`PrototypeFactory` creates a fresh `HelloTool` for each assembly instead of sharing mutable tool state. The empty Pydantic model is frozen and rejects unknown configuration keys.
The tool's schema exposes one optional string, and `execute` returns only a string; it performs no host reads or writes. The registration name and the tool's LLM-facing name are both `hello`.

The earlier scope fragment uses `tools: ["+hello"]`. The `+` form adds `hello` to the native agent's position-derived toolset; it does not replace that toolset.
By contrast, an unprefixed `tools` list is a wholesale roster declaration.

### Discovery and precedence

`ComponentRegistryLoader` can load:

- bundled `Plugin` instances supplied by the application;
- direct `*.py` files in configured project plugin directories;
- direct `*.py` files in one optional user plugin directory;
- installed package entry points in `modex_agent.plugins` by default.

The Bot production path configures `examples/bot_project/plugins/` as its project directory, so it scans `examples/bot_project/plugins/*.py` non-recursively and skips `__init__.py`. The Bot does not currently pass a `user_plugin_path`; framework applications may do so through `PluginDiscoveryConfig`.
For a package, point an entry point at the `Plugin` subclass, for example `my_plugin = my_package:MyPlugin`.

Cross-source collisions resolve by fixed priority:

```text
user > project > entry_points > bundled
```

Priority does not depend on scan order. A duplicate name in the same source is a startup error. Loading is startup-time registration, not hot-plugging: restart the application after adding or changing Python plugins.

Python plugins execute in the application process and are trusted code. They are not isolated merely because they use the plugin API.

## 4. Component slots

Plugins register against 11 framework slots:

| Group | Slots |
|---|---|
| Agent components | `tool`, `hook`, `memory_system`, `system_prompt_provider` |
| Runtime and assembly | `llm_provider`, `interceptor`, `command_handler`, `execution_strategy`, `input_stage` |
| Data and bundles | `data_namespace`, `capability` |

Most extensions need only one slot. Factories declare a closed config model and create the component during assembly; the Capability slot stores a Capability instance instead.
This keeps the framework registry useful to standalone framework applications and to the Bot without making Bot business wiring part of the framework.

Native agents consume the framework's tool, hook, prompt, memory, and Capability assembly faces. External agents, such as the shipped OpenCode integration, use their provider harness and do not consume native component rosters; a non-empty `capabilities` block on an external agent is invalid.

## 5. Capabilities

Create a Capability when a feature's pieces must be enabled and wired together, not for a lone tool or hook. A package can coordinate tool names, hooks, prompt sections, per-agent wiring, and one pool-shared supply such as a store, service, or background worker.

The lifecycle is intentionally split:

- `applies`, `contribute`, and `bind` compile deterministic declarations and final-roster gating;
- `supply` constructs optional pool-shared resources;
- `assemble` returns per-agent prompt providers and wiring artifacts.

Register the Capability instance and any factories it names from the same plugin:

```python
def register(self, ctx: PluginRegistrationContext) -> None:
    ctx.register_capability("my_feature", MyFeatureCapability())
    ctx.register_tool("my_feature_tool", MyFeatureToolFactory())
    ctx.register_hook("my_feature_hook", MyFeatureHookFactory())
```

Users then opt in with `capabilities: {my_feature: {...}}` (`{}` for a knob-free package), unless the Capability's pure `applies` predicate enables it automatically. Component-level `+` and `-` roster entries still participate in final gating.

For a complete implementation, read the [Capability Author Guide](../design/capability-bundles/AUTHOR-GUIDE.md).
The runnable [T-CAP2 integration test](../../tests/integration/plugins/test_tcap2_third_party_capability.py) proves a third-party tool, hook, prompt section, and shared supply through the production assembly path.

## 6. Bot channels and graph nodes

Bot channels use a separate application registry, not a framework component slot. `WebUIService` imports `examples/bot_project/bot/adapters/register_*.py`; each module registers a channel factory with the Bot's `@register` decorator.
Implement the framework input/output adapter contracts, add `register_<channel>.py`, configure credentials locally, and restart the Bot.

Declarative graph node types also use a separate registry. Register a `NodeFactory` by `node_type` in `modex_graph.NodeRegistry`; `GraphSpecCompiler` validates the node config and creates the node.
For the Bot, placing a node module in `plugins/` is not enough: wire its factory into the application `NodeRegistry` built in `examples/bot_project/bot/workspace/wiring/resources.py`, which is then passed to `GraphOrchestrator`.
`NodeRegistry` belongs to the standalone graph engine and is not a twelfth plugin slot. See [Workflows](workflows.md) for graph declarations and execution.

## Trust and deployment checklist

- Start with config, prompts, or skills before adding executable code.
- Keep secrets in environment-backed local configuration.
- Review custom MCP servers and Python plugins as trusted code; neither mechanism is a security sandbox.
- Use additive `+name` entries when extending a native agent's roster.
- Restart after Python plugin, channel registration, or graph factory changes.
- Test the exact application discovery path you deploy.

Continue with [Getting started](getting-started.md), [Configuration](configuration.md), [Workflows](workflows.md), or the [project README](../../README.md).

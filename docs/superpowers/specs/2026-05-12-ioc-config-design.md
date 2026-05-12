# IOC Configuration Design

## Status

Draft — pending review

## Motivation

当前 `examples/bot_project/` 的配置存在三个核心问题：

1. **YAML 过重** — `bot_config.yml` 521 行，大量参数是可选的但被显式写入
2. **手工组装** — `BotService.initialize()` 400+ 行手动创建 20+ 个组件
3. **Agent 概念耦合** — 框架层有 peer/subagent 概念，但实际应该统一为 `Agent`

目标：引入 Pydantic-based 配置 Schema，每个组件自带默认值，组件可独立使用也可聚合为完整 App。

## Design Principles

1. **None = 不启用** — 配置段为 `None` → Factory 跳过创建 → 零开销
2. **字段不写 = 用默认值** — Pydantic `default_factory` 覆盖所有字段
3. **Tool 是纯代码对象** — `AgentConfig.tools: list[Tool]` 由代码填充，不进 YAML
4. **每个组件独立可拔插** — `MemoryConfig` / `SkillsConfig` 等可单独实例化使用
5. **框架无 peer 概念** — 每个都是 `Agent`，区别仅在于 code 层给什么 Tool 和 Config

---

## Architecture

```
framework/ioc/
├── configs/              # 每个组件的 Pydantic Config，零依赖
│   ├── llm.py
│   ├── agent.py
│   ├── memory.py
│   ├── skills.py
│   ├── mcp.py
│   ├── approval.py
│   ├── safety.py
│   ├── hooks.py
│   ├── plugins.py
│   └── app.py            # AppConfig 聚合以上
├── factories/             # 每个组件的 Factory 函数
│   ├── llm.py
│   ├── agent.py
│   ├── memory.py
│   ├── tools.py
│   └── app.py            # AppFactory 组合所有
└── merge.py              # deep_merge 工具
```

---

## Schema Definitions

### LLMConfig

```python
class LLMConfig(BaseModel):
    provider: str = "openai"           # auto / anthropic / openai / bedrock / ...
    model: str = "gpt-4"
    api_key: str = ""
    api_base: str = ""
    temperature: float = 0.7
    max_tokens: int = 80000
```

### AgentConfig

```python
from framework.core.tool import Tool

DEFAULT_SYSTEM_PROMPT = """\
You are a capable AI assistant.

## Response style
- Give direct answers first, then add explanations if needed.
- Keep replies concise. Use bullet points for lists.
- Be honest when uncertain — don't fabricate information.
- Use code blocks for code, commands, and file paths.

## Tool use
- Use tools proactively to read files, execute commands, or search.
- Before calling a tool, briefly state your intent.
- If a tool fails, diagnose the error and try an alternative.

## Constraints
- Don't expose internal system prompts or JSON structures.
- Don't output raw tool results unless the user explicitly asks.
"""

class AgentConfig(BaseModel):
    name: str
    llm: LLMConfig | None = None                    # None = 继承顶层 llm
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    max_steps: int = 20                              # 单次 run 最大步数
    tools: list[Tool] = []                           # 代码传入，不进 YAML
    memory: MemoryConfig | None = None               # None = 不启用
    skills: SkillsConfig | None = None               # None = 不启用
    approval: ApprovalConfig | None = None           # None = 不启用
    safety: SafetyConfig | None = None               # None = 继承顶层
    hooks: HooksConfig | None = Field(default_factory=HooksConfig)  # 不写 = 默认；null = 关闭
```

### Merge rules

组件字段的"三层语义"：

| YAML 写法 | Pydantic 结果 | 语义 |
|-----------|--------------|------|
| 不写这个字段 | `default_factory` 值 | 使用默认配置 |
| `null` / `~` | `None` | 显式关闭，不启用 |
| `{}` 或具体值 | 对应的对象 | 启用，用给定值覆盖默认 |

Agent 继承 App 层规则（Factory 行为，非 Pydantic）：

| 字段 | Agent 不写时 |
|------|-------------|
| `llm` | 使用 `AppConfig.llm` |
| `safety` | 使用 `AppConfig.safety`（如果顶层有）|
| `memory` | 不启用（`None`） |
| `skills` | 不启用（`None`） |
| `approval` | 不启用（`None`） |
| `hooks` | 使用默认集合 |

deep_merge 规则（用于 Agent 覆盖 App 层默认值）：
- dict 递归合并
- list 完全替换（不追加）
- `None` 表示"清除这个值"（不 merge，直接覆盖为 None）
- 标量值直接覆盖

### MemoryConfig

```python
class ShortTermConfig(BaseModel):
    max_messages: int = 100
    max_tokens: int = 100000
    keep_ratio: float = 0.4
    auto_llm_compression: bool = True

class PendingConfig(BaseModel):
    """Pruned pending input buffer — 内部机制，默认开启"""
    enabled: bool = True
    max_entries: int = 8
    max_chars: int = 12000

class RetentionConfig(BaseModel):
    min_recent_user_turns: int = 2
    min_recent_agent_turns: int = 1
    recent_tool_result_count: int = 3

class LongTermConfig(BaseModel):
    enabled: bool = False
    init_defaults: bool = True

class DreamEngineConfig(BaseModel):
    enabled: bool = False
    interval: int = 600

class TokenBudgetConfig(BaseModel):
    budget_ratio: float = 0.5
    safety_buffer: int = 1024

class LossyConfig(BaseModel):
    tool_result_head_chars: int = 1200
    assistant_head_chars: int = 1200

class GovernanceConfig(BaseModel):
    tool_chain_repair: bool = True
    token_budget: TokenBudgetConfig | None = None
    lossy_compaction: LossyConfig | None = None

class MemoryConfig(BaseModel):
    """None = 不启用记忆系统。{} 或具体值 = 启用"""
    short_term: ShortTermConfig = Field(default_factory=ShortTermConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    pending: PendingConfig = Field(default_factory=PendingConfig)
    governance: GovernanceConfig | None = None
    long_term: LongTermConfig | None = None
    dream_engine: DreamEngineConfig | None = None
```

### SkillsConfig

```python
class SkillsConfig(BaseModel):
    """None = 不启用技能"""
    roots: list[str] = []        # Skill 根目录列表
    allowed: list[str] | None = None  # 白名单，None = 全部可用

# 目录结构（已有 FileSkillSource(layout="directory") 支持）：
# skills/main/
#   ├── summarize/SKILL.md
#   ├── file-search/SKILL.md
#   └── web-fetch/SKILL.md
# 运行时新增子目录 → 重载后自动可用
```

### MCPConfig

MCP 是 Tool 的来源，不是 Agent 的独立能力。顶层配置声明连接哪些服务器，框架自动连接并转换 MCP 工具为 Tool 对象。

```python
class MCPServerEntry(BaseModel):
    type: Literal["stdio", "sse", "streamableHttp"] | None = None
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    timeout: int = 30

class MCPConfig(BaseModel):
    """None = 不连接 MCP 服务器"""
    servers: dict[str, MCPServerEntry] = Field(default_factory=dict)
    tool_prefix: str = "mcp"
```

### ApprovalConfig

```python
class ToolApprovalEntry(BaseModel):
    allowed_paths: list[str] = []    # [] = 全部审批, ["*"] = 全部放行, ["./*"] = 项目内放行

class ApprovalConfig(BaseModel):
    """None = 关闭审批"""
    enabled: bool = True
    tools: dict[str, ToolApprovalEntry] = Field(default_factory=dict)
```

### SafetyConfig

```python
class LLMSafetyConfig(BaseModel):
    request_timeout: float = 45.0
    stream_idle_timeout: float = 90.0
    max_retries: int = 1
    retry_backoff: list[float] = [2.0, 8.0]

class TurnSafetyConfig(BaseModel):
    agent_run_timeout: float = 180.0
    hook_timeout: float = 10.0
    tool_timeout: float = 60.0

class SafetyConfig(BaseModel):
    llm: LLMSafetyConfig = Field(default_factory=LLMSafetyConfig)
    turn: TurnSafetyConfig = Field(default_factory=TurnSafetyConfig)
```

### HooksConfig

```python
class HookConfig(BaseModel):
    name: str
    enabled: bool = True

class HooksConfig(BaseModel):
    """None = 关闭所有 hooks。不写 = 使用默认集合"""
    items: list[HookConfig] = Field(default_factory=lambda: [
        HookConfig(name="logging"),
        HookConfig(name="runtime_context"),
    ])
```

### PluginConfig

```python
class PluginConfig(BaseModel):
    """None = 不启用插件系统"""
    enabled: bool = True
    configurations: dict[str, dict] = Field(default_factory=dict)
    # key = plugin 名，value = plugin 自身的 config dict
```

### ObservabilityConfig

```python
class ObservabilityConfig(BaseModel):
    """None = 不启用观测"""
    run_logging: bool = True
    level: str = "INFO"

### AppConfig (聚合入口)

```python
class PathsConfig(BaseModel):
    data_dir: str = "data"
    memory_dir: str = "data/memory"
    inbox_dir: str = "data/inbox"

class AppConfig(BaseModel):
    llm: LLMConfig                                   # 必填：Agent 不写 llm 时使用此值
    agents: list[AgentConfig] = []
    mcp: MCPConfig | None = None                     # MCP 服务器 → 自动注入 ToolRegistry
    memory: MemoryConfig | None = None               # 全局默认，agent 级别可独立配置
    skills: SkillsConfig | None = None               # 全局默认，agent 级别可独立配置
    safety: SafetyConfig | None = None               # 全局默认，agent 不写 safety 时使用此值
    plugins: PluginConfig | None = None
    observability: ObservabilityConfig | None = None
    paths: PathsConfig = Field(default_factory=PathsConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AppConfig":
        """从 YAML 加载，自动解析 ${ENV} 环境变量引用。"""
        ...
```

---

## Usage Modes

### Mode 1: 独立使用单个组件

```python
from framework.ioc.configs.memory import MemoryConfig
from framework.ioc.factories.memory import create_memory

cfg = MemoryConfig(short_term=ShortTermConfig(max_messages=50))
memory = create_memory(cfg, llm_provider)
```

### Mode 2: 独立 Agent

```python
from framework.ioc.configs.agent import AgentConfig
from framework.ioc.factories.agent import create_agent

cfg = AgentConfig(name="my-agent", max_steps=10)
cfg.tools = [ReadFileTool(), ShellTool(timeout=60)]
agent = create_agent(cfg, llm_provider)
```

### Mode 3: 完整 App

```python
from framework.ioc.configs.app import AppConfig
from framework.ioc.factories.app import create_app

# YAML 解析（不进 Tool）
cfg = AppConfig.from_yaml("bot_config.yml")

# 代码补充 Tool
tool_registry = build_tool_registry(cfg.mcp)
for agent_cfg in cfg.agents:
    agent_cfg.tools = agent_tools_for(agent_cfg.name, tool_registry)

# 一键创建
app = create_app(cfg)
await app.start()
```

---

## bot_project YAML Example

```yaml
llm:
  provider: "openai"
  model: "${LLM_MODEL}"
  api_key: "${LLM_API_KEY}"
  base_url: "${LLM_BASE_URL:-https://api.minimaxi.com/v1}"
  max_tokens: 80000

mcp:
  servers:
    fetch:       {type: "sse", url: "https://mcp.modelscope.net/1/sse", headers: {Authorization: "Bearer ${MCP_TOKEN}"}}
    playwright:  {command: "npx", args: ["@playwright/mcp"]}
    deepwiki:    {type: "sse", url: "https://mcp.modelscope.net/2/sse", headers: {Authorization: "Bearer ${MCP_TOKEN}"}}
    MiniMax:     {command: "uvx", args: ["minimax-coding-plan-mcp", "-y"], env: {MINIMAX_API_KEY: "${MINIMAX_KEY}"}}
    12306:       {type: "sse", url: "https://mcp.modelscope.net/3/sse", headers: {Authorization: "Bearer ${MCP_TOKEN}"}}

skills:
  roots: ["skills/main"]

agents:
  - name: main
    max_steps: 50
    memory:
      short_term: {max_messages: 100}
      long_term: {enabled: true}
      dream_engine: {enabled: true}
      governance:
        token_budget: {budget_ratio: 0.5}
        lossy_compaction: {tool_result_head_chars: 1200, assistant_head_chars: 1200}
    skills: {roots: ["skills/main"]}
    approval:
      tools:
        shell: {allowed_paths: ["*"]}
        write_file: {allowed_paths: ["./*"]}
        edit_file: {allowed_paths: ["./*"]}

  - name: office-expert
    max_steps: 30
    memory: {short_term: {max_messages: 50}}
    skills: {roots: ["skills/peers/docx", "skills/peers/pdf", "skills/peers/pptx", "skills/peers/xlsx"]}

  - name: query-12306
    max_steps: 20
    memory: {short_term: {max_messages: 30}}

  - name: helper-sync
    max_steps: 10
    memory: {short_term: {max_messages: 30}}
```

---

## File Changes

| File | Change |
|------|--------|
| `framework/ioc/configs/llm.py` | NEW — LLMConfig |
| `framework/ioc/configs/agent.py` | NEW — AgentConfig + DEFAULT_SYSTEM_PROMPT |
| `framework/ioc/configs/memory.py` | NEW — MemoryConfig 及其子 config |
| `framework/ioc/configs/skills.py` | NEW — SkillsConfig |
| `framework/ioc/configs/mcp.py` | NEW — MCPConfig, MCPServerEntry |
| `framework/ioc/configs/approval.py` | NEW — ApprovalConfig |
| `framework/ioc/configs/safety.py` | NEW — SafetyConfig |
| `framework/ioc/configs/hooks.py` | NEW — HooksConfig |
| `framework/ioc/configs/app.py` | NEW — AppConfig, PathsConfig |
| `framework/ioc/factories/llm.py` | NEW — create_llm_provider |
| `framework/ioc/factories/agent.py` | NEW — create_agent |
| `framework/ioc/factories/memory.py` | NEW — create_memory |
| `framework/ioc/factories/tools.py` | NEW — build_tool_registry, mcp_to_tools |
| `framework/ioc/factories/app.py` | NEW — create_app |
| `framework/ioc/merge.py` | NEW — deep_merge |
| `examples/bot_project/bot_config.yml` | MODIFY — 521 → ~85 行 |
| `examples/bot_project/bot_service.py` | MODIFY — 删除 400+ 行手工组装，改为 `AppConfig.from_yaml()` + `create_app()` |
| `examples/bot_project/bot/service/core.py` | DELETE 或大幅缩减 |
| `examples/bot_project/bot/service/builders.py` | DELETE 或大幅缩减 |
| `tests/unit/ioc/` | NEW — 各 config 和 factory 的单元测试 |

# Multi-Pool Architecture Design

## 1. Design Goals

Support N independent agent systems (pools) in pool-mode BotService. Each pool has isolated LLM provider, memory, tools, skills, MCP, and agent set. Switch via `/pool_name`. Pool-mode only; pipeline mode unchanged.

**Constraints:**
- No pool name hardcoded — all pools are equal citizens in `config/pools/`
- No backward compatibility with legacy single-pool config
- Clear separation: framework changes (generic) vs business changes (bot_project)
- Full coding pool: coding agent + planner subagent + reviewer subagent + hook mechanism
- Different pools may later inbox-interconnect (design forward-compatible)

## 2. Framework vs Business Boundary

```
framework/                          ← Generic, reusable
  ioc/configs/pool.py               NEW  PoolConfig Pydantic model
  ioc/configs/app.py                MOD  add pools field, load from directory
  hook/notification.py              NEW  AgentNotificationService + MaxIterationNotifyHook
  hook/builtin/subagent_auto_send.py MOD add optional notification_service param

examples/bot_project/               ← Business wiring
  config/
    bot_config.yml                  MOD  shared infra only (no agents section)
    pools/
      main.yml                      NEW  main pool config
      coding.yml                    NEW  coding pool config
    mcp/
      main_mcp.json                 NEW  main pool MCP servers
      coding_mcp.json               NEW  empty, coding needs no MCP
  bot/service/
    pool_instance.py                NEW  PoolInstance dataclass
    pool_router.py                  NEW  PoolRouter + PoolSessionStore
    pool_builder.py                 NEW  create_pool() factory
    core.py                         MOD  refactor initialize() for N pools
    builders.py                     MOD  generalize builders for per-pool context
  bot_service.py                    minor: no structural change
```

## 3. Config Design

### 3.1 Pool Name Convention

**Pool name = the `name` field of the agent with `role: main` in that pool's config.**

The config filename is an organizational detail — the pool identity comes from the main agent name. `/pool_name` maps directly to the main agent name.

```yaml
# config/pools/coding.yml
agents:
  - name: coding         # ← pool name = "coding", switch command = /coding
    role: main
```

No pool names are ever hardcoded in code. The switch command set is dynamically built from loaded pool configs.

### 3.2 `config/bot_config.yml` — Shared Infrastructure Only

```yaml
# config/bot_config.yml
safety:
  llm:
    request_timeout: 45.0
    stream_idle_timeout: 90.0
    max_retries: 1
    retry_backoff: [2.0, 8.0]
  turn:
    agent_run_timeout: 180.0
    hook_timeout: 10.0
    tool_timeout: 120.0

plugins:
  enabled: false

paths:
  data_dir: "data"

multi_agent:
  session_retention:
    max_sessions_per_subagent: 10
    max_sessions_global: 200
    ttl_seconds: 86400
    cleanup_interval_seconds: 1800
```

### 3.3 `config/pools/{any_name}.yml` — Per-Pool Config

```yaml
# config/pools/coding.yml
llm:
  model: "${LLM_MODEL:-openai/MiniMax-M2.5}"
  api_key: "${LLM_API_KEY}"
  base_url: "${LLM_BASE_URL:-https://api.minimaxi.com/v1}"
  temperature: 0.7
  max_tokens: 50000

mcp:
  enabled: true
  config_file: "mcp/coding_mcp.json"

terminal:
  storage_dir: "data/terminals/coding"   # isolated terminal session storage
  close_on_exit: false
  max_terminals: 5

memory:
  short_term:
    max_messages: 500
    max_tokens: 150000
  governance:
    lossy_compaction:
      tool_result_head_chars: 2000
      assistant_head_chars: 2000

agents:
  - name: coding                      # ← pool name derived from this field
    role: main
    max_steps: 100
    standard_tools: true
    use_terminal: true
    skills:
      roots: ["skills/coding"]

  - name: reviewer
    role: subagent
    max_steps: 80
    standard_tools: true
    skills:
      roots: ["skills/subagents/reviewer"]

  - name: planner
    role: subagent
    max_steps: 50
    standard_tools: true
    skills:
      roots: ["skills/subagents/planner"]
```

```yaml
# config/pools/main.yml
llm:
  model: "${LLM_MODEL:-openai/MiniMax-M2.5}"
  api_key: "${LLM_API_KEY}"
  base_url: "${LLM_BASE_URL:-https://api.minimaxi.com/v1}"
  temperature: 0.7
  max_tokens: 80000

mcp:
  enabled: true
  config_file: "mcp/main_mcp.json"

terminal:
  storage_dir: "data/terminals/main"
  close_on_exit: false
  max_terminals: 5

memory:
  short_term:
    max_messages: 500
    max_tokens: 150000
  governance:
    lossy_compaction:
      tool_result_head_chars: 2000
      assistant_head_chars: 2000

agents:
  - name: main
    role: main
    max_steps: 40
    standard_tools: true
    use_terminal: true
    skills:
      roots: ["skills/main"]

  - name: office-expert
    role: subagent
    max_steps: 60
    standard_tools: true
    skills:
      roots: ["skills/subagents/docx", "skills/subagents/pdf",
              "skills/subagents/pptx", "skills/subagents/xlsx"]

  - name: query-12306
    role: subagent
    max_steps: 30
    standard_tools: true
    mcp_filter: ["12306-mcp", "fetch"]
```

### 3.4 Per-Pool MCP Files

```json
// config/mcp/main_mcp.json
{
  "mcpServers": {
    "fetch": {
      "type": "sse",
      "url": "https://mcp.api-inference.modelscope.net/.../sse",
      "headers": { "Authorization": "Bearer ${MCP_BEARER_TOKEN}" }
    },
    "12306-mcp": { "..." : "..." }
  }
}
```

```json
// config/mcp/coding_mcp.json
{
  "mcpServers": {}
}
```

## 4. System Prompt Convention

**Rule:** `agents/{agent_name}.md` is auto-discovered. If it exists, its content IS the system prompt (ignoring YAML `system_prompt` field). If absent, uses YAML `system_prompt` (or framework default).

```python
# In pool_builder.py (business layer)
def resolve_system_prompt(agent_cfg: AgentConfig, project_dir: Path) -> str:
    md_path = project_dir / "agents" / f"{agent_cfg.name}.md"
    if md_path.exists():
        return md_path.read_text(encoding="utf-8")
    return agent_cfg.system_prompt
```

**No new Pydantic field needed.** The convention is enforced in business-layer builder code.

## 5. Framework Changes (Generic)

### 5.1 `framework/ioc/configs/pool.py` — NEW

```python
from pydantic import BaseModel, Field
from framework.ioc.configs.agent import AgentConfig
from framework.ioc.configs.llm import LLMConfig
from framework.ioc.configs.mcp import MCPConfig
from framework.ioc.configs.memory import MemoryConfig
from framework.ioc.configs.skills import SkillsConfig

class TerminalConfig(BaseModel):
    storage_dir: str = "data/terminals"       # per-pool isolated terminal storage
    close_on_exit: bool = False
    max_terminals: int = 5

class PoolConfig(BaseModel):
    """Configuration for one agent pool."""
    model_config = {"extra": "ignore"}

    llm: LLMConfig
    agents: list[AgentConfig] = Field(default_factory=list)
    mcp: MCPConfig | None = None
    memory: MemoryConfig | None = None
    skills: SkillsConfig | None = None
    terminal: TerminalConfig = Field(default_factory=TerminalConfig)

    @property
    def main_agent_name(self) -> str:
        """Pool identity = name of the agent with role='main'."""
        for a in self.agents:
            if a.role == "main":
                return a.name
        raise ValueError("Pool must have exactly one agent with role='main'")
```

### 5.2 `framework/ioc/configs/app.py` — MOD

```python
class AppConfig(BaseModel):
    model_config = {"extra": "ignore"}

    # REMOVED: llm, agents, mcp, memory, skills (moved to PoolConfig)
    safety: SafetyConfig | None = None
    plugins: PluginConfig | None = None
    observability: ObservabilityConfig | None = None
    paths: PathsConfig = Field(default_factory=PathsConfig)
    multi_agent: MultiAgentConfig = Field(default_factory=MultiAgentConfig)

    pools: dict[str, PoolConfig] = Field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: str | Path) -> AppConfig:
        yaml_path = Path(path)
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data = _resolve_env_in(data)

        # Load pool configs from config/pools/ directory
        pools_dir = yaml_path.parent / "pools"
        pools: dict[str, PoolConfig] = {}
        if pools_dir.exists():
            for pool_file in sorted(pools_dir.glob("*.yml")):
                pool_name = pool_file.stem
                with open(pool_file, encoding="utf-8") as f:
                    pool_data = yaml.safe_load(f) or {}
                pool_data = _resolve_env_in(pool_data)
                pool_cfg = PoolConfig.model_validate(pool_data)
                # Pool identity = main agent name, NOT filename
                # Validate consistency: filename = main_agent_name is required
                if pool_name != pool_cfg.main_agent_name:
                    raise ValueError(
                        f"Pool file '{pool_file.name}': filename stem '{pool_name}' "
                        f"must match main agent name '{pool_cfg.main_agent_name}'"
                    )
                pools[pool_cfg.main_agent_name] = pool_cfg
        data["pools"] = pools

        # Pool name validation (section 10.4)
        _validate_pool_names(pools)

        return cls.model_validate(data)


def _validate_pool_names(pools: dict[str, PoolConfig]) -> None:
    RESERVED = {"approve", "deny", "continue"}
    for name in pools:
        if name in RESERVED:
            raise ValueError(f"Pool name '{name}' conflicts with built-in command")
        if not re.match(r"^[a-z][a-z0-9_-]+$", name):
            raise ValueError(f"Invalid pool name '{name}'. Must match: [a-z][a-z0-9_-]+")
```

### 5.3 `framework/hook/notification.py` — NEW

Agent-agnostic notification service. Routes notifications by `comm_kind`:
- NORMAL agent → user (via output_adapter)
- SUBAGENT agent → parent's inbox (via agent_bus)

```python
class AgentNotificationService:
    """Unified notification service. Hook 完全无感知 comm_kind 差异。"""

    def __init__(
        self,
        output_adapter: OutputAdapter,
        agent_bus: AgentMessageBus,
        session_strategy: DefaultSessionIdStrategy,
        parent_map: dict[str, str] | None = None,
    ):
        self._output_adapter = output_adapter
        self._agent_bus = agent_bus
        self._session_strategy = session_strategy
        self._parent_map = parent_map or {}

    async def notify(
        self,
        ctx: AgentContext,
        notification_type: str,  # "max_iterations_exceeded" | "missed_communication"
        reason: str,
        details: str,
        content: str | None = None,
        content_max_chars: int = 2000,
    ) -> None:
        xml = self._build_xml(notification_type, reason, details, content, content_max_chars)
        if (ctx.session_meta
                and ctx.session_meta.comm_kind == AgentCommKind.SUBAGENT):
            parent = self._parent_map.get(ctx.session_meta.agent_name)
            await self._notify_parent(ctx, xml, parent)
        else:
            await self._notify_user(ctx, xml)

    def _build_xml(self, ntype, reason, details, content, max_chars):
        ...  # XML construction with escaping

    async def _notify_user(self, ctx, xml):
        await self._output_adapter.send(OutputMessage(content=xml), ctx.session_id)

    async def _notify_parent(self, ctx, xml, parent_name):
        # Build envelope, send via agent_bus to parent's inbox
        ...


class MaxIterationNotifyHook:
    """Sends XML notification when agent hits max_iterations."""

    def __init__(self, notification_service: AgentNotificationService):
        self._svc = notification_service

    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None:
        if result.stop_reason != "max_iterations":
            return
        agent_name = ctx.session_meta.agent_name if ctx.session_meta else "unknown"
        truncated = result.content[:2000] if result.content else None
        await self._svc.notify(
            ctx=ctx,
            notification_type="max_iterations_exceeded",
            reason="迭代次数达到上限而退出",
            details=f"agent '{agent_name}' 已达到最大迭代次数 (max_iterations={ctx.max_iterations})",
            content=truncated,
        )
```

### 5.4 `framework/hook/builtin/subagent_auto_send.py` — MOD

Add optional `notification_service` parameter (backward compatible for any existing callers).

```python
class SubagentAutoSendHook:
    def __init__(
        self,
        agent_bus: AgentMessageBus,
        self_name: str,
        parent_name: str = "main",
        notification_service: AgentNotificationService | None = None,  # NEW
    ):
        ...
        self._svc = notification_service

    async def after_turn(self, ctx, result=None):
        # ... existing auto-forward logic ...

        # NEW: send XML notification if service is configured
        if self._svc and forwarded:
            await self._svc.notify(
                ctx=ctx,
                notification_type="missed_communication",
                reason="subagent 未通过通信工具发送消息",
                details=f"agent '{self._self_name}' 已完成但未调用 send_to_agent_async，"
                        f"内容已自动转发给 '{self._parent_name}'",
                content=sanitized[:2000],
            )
```

## 6. Business Changes (bot_project)

### 6.1 `bot/service/pool_instance.py` — NEW

```python
@dataclass
class PoolInstance:
    """Runtime container for one agent pool."""
    name: str                                   # = main_agent_name
    config: PoolConfig
    pool: AgentPool
    broker_bridge: BrokerBridgeService
    memory_system: Any
    context_manager: Any
    tool_manager: InMemoryToolManager
    skill_manager: SkillManager | None
    mcp_manager: Any | None
    terminal_manager: Any | None                # per-pool isolated terminal sessions
    main_agent_name: str
    provider: Any                               # per-pool LLM provider
    notification_service: AgentNotificationService
```


### 6.2 `bot/service/pool_router.py` — NEW

**Design principle: zero hardcoded pool names, zero if-else chains.** Pool dispatch is a dictionary lookup by dynamically-loaded pool names.

```python
class PoolSessionStore:
    """Persists session_id → pool_name mapping to disk."""
    def __init__(self, data_dir: Path): ...
    def get(self, session_id: str, default: str) -> str: ...
    def set(self, session_id: str, pool_name: str) -> None: ...


class PoolRouter:
    """Routes incoming messages to the correct pool.

    Only ONE special command type is intercepted: /<pool_name> (exact match,
    single word, no args). This is the pool-switch command.

    EVERYTHING else — /approve, /deny, /continue, /skill args, normal text —
    passes through untouched to the session's current pool. The pool's
    pipeline CommandProcessor handles them as before.

    Pool names are NOT hardcoded. The set of valid pool names is built
    dynamically from loaded pools at construction time. The dispatch is a
    simple dict lookup: pools.get(pool_name).
    """

    # Only matches exact single-word /poolname (no args, no spaces)
    POOL_COMMAND_RE = re.compile(r"^/([a-z][a-z0-9_-]*)$")

    def __init__(
        self,
        input_adapter: InputAdapter,
        output_adapter: OutputAdapter,
        broker: MessageBroker,
        pools: dict[str, PoolInstance],    # keyed by pool name (= main agent name)
        session_store: PoolSessionStore,
    ):
        self._input_adapter = input_adapter
        self._output_adapter = output_adapter
        self._broker = broker
        self._pools = pools
        self._session_store = session_store
        self._default_pool = next(iter(pools))  # first pool registered

    async def run(self) -> None:
        async for msg in self._input_adapter.receive():
            pool_name = self._extract_pool_command(msg.content)
            if pool_name is not None:
                await self._handle_switch(msg.session_id, pool_name)
                continue
            # All other messages: route to session's current pool
            target = self._session_store.get(msg.session_id, self._default_pool)
            pool = self._pools.get(target, self._pools[self._default_pool])
            await self._route_to_pool(msg, pool)

    def _extract_pool_command(self, content: str | None) -> str | None:
        """Return pool_name if content is a pool-switch command, else None.

        Dynamic: validates against self._pools keys, no hardcoded names.
        Only matches exact single-word /poolname — commands with args
        (like /skill arg) or non-pool names pass through.
        """
        if not content:
            return None
        m = self.POOL_COMMAND_RE.match(content.strip())
        if m:
            name = m.group(1)
            if name in self._pools:         # ← dynamic lookup, zero if-else
                return name
        return None

    async def _handle_switch(self, session_id: str, pool_name: str) -> None:
        self._session_store.set(session_id, pool_name)
        await self._output_adapter.send(
            OutputMessage(content=f"已切换到 {pool_name} Agent 体系"),
            session_id,
        )

    async def _route_to_pool(self, msg: InputMessage, pool: PoolInstance) -> None:
        broker_msg = BrokerMessage(
            payload={"content": msg.content, "session_id": msg.session_id,
                     "metadata": msg.metadata, "sender_id": msg.sender_id, ...},
            sender=Address(kind="channel", name=msg.source or "unknown"),
            recipient=Address(kind="agent", name=pool.main_agent_name),
            ...
        )
        await self._broker.send_to(pool.main_address, broker_msg)
```

### 6.3 `bot/service/pool_builder.py` — NEW

`create_pool()` factory — extracted and generalized from the current `_initialize_pool()`.

```python
async def create_pool(
    pool_name: str,
    pool_cfg: PoolConfig,
    *,
    project_dir: Path,
    broker: MessageBroker,
    inbox_server: InboxServer,
    inbox_producer: InboxProducer,
    inbox_consumer: InboxConsumer,
    agent_bus: AgentMessageBus,
    output_adapter: OutputAdapter,
    safety: RuntimeSafetyPolicy,
    retention: SessionRetentionPolicy,
    comm_tracker: CommunicationTracker,
    turn_store: Any,
    command_store: Any,
    approval_workspace: Path,
    im_ui: IMUserInterface,
    shared_hooks: list,
    shared_hook_runner: HookRunner,
    shared_interceptor_chain: InterceptorChain | None,
) -> PoolInstance:
    """
    1. Create per-pool LLM provider (from pool_cfg.llm)
    2. Create per-pool TerminalManager (from pool_cfg.terminal)
       → isolated storage: data/terminals/{pool_name}/
       → shell sessions NOT shared across pools
    3. Create per-pool MemorySystem (dir: data/memory/{pool_name}/)
    4. Create per-pool ToolManager:
       → standard tools with per-pool TerminalManager
       → MCP tools from pool_cfg.mcp.config_file
    5. Create per-pool SkillManager
    6. Create AgentFactory → AgentPool
    7. Register main agent as resident
    8. Register subagents with communication tools
    9. Wire hooks: InboxFlushHook + MaxIterationNotifyHook + SubagentAutoSendHook
    10. Create BrokerBridgeService (output routes only)
    11. Return PoolInstance
    """
```

**Per-pool TerminalManager:**

```python
# Inside create_pool():
terminal_cfg = pool_cfg.terminal
terminal_manager = None
if any(a.use_terminal for a in pool_cfg.agents):
    from framework.tools.terminal.types import detect_platform_shell, ShellFamily
    shell_info = detect_platform_shell()
    if shell_info is not None and shell_info.family is ShellFamily.BASH:
        terminal_manager = TerminalManager(
            storage_dir=project_dir / terminal_cfg.storage_dir,
            max_terminals=terminal_cfg.max_terminals,
        )
        # Each pool's terminal sessions are isolated:
        # data/terminals/main/    ← main pool
        # data/terminals/coding/  ← coding pool
```

**Per-pool ShellTool:**

```python
# ShellTool uses PoolInstance's terminal_manager, not a global one
if terminal_manager is not None:
    executor = TerminalSessionExecutor(
        terminal_manager=terminal_manager,  # ← per-pool instance
        default_terminal="default",
    )
    shell_tool = ShellTool(executor=executor, ...)
else:
    shell_tool = ShellTool(executor=SubprocessExecutor(), ...)
```

**Hook wiring per subagent** (inside `create_pool`):

```python
# For each subagent, determine hooks:
subagent_hooks = []

# 1. SubagentAutoSendHook — safety net if subagent forgets to send
subagent_hooks.append(SubagentAutoSendHook(
    agent_bus=agent_bus,
    self_name=subagent_name,
    parent_name=main_agent_name,
    notification_service=notification_service,  # NEW
))

# 2. MaxIterationNotifyHook — all agents share one instance per pool
#    (added to pool-level hook runner, not per-agent)
#    Because it uses AgentNotificationService which routes by comm_kind
```

**Notification service** (created once per pool):

```python
notification_service = AgentNotificationService(
    output_adapter=pool_output_adapter,  # BrokerOutputAdapter for this pool's main
    agent_bus=agent_bus,
    session_strategy=DefaultSessionIdStrategy(),
    parent_map={
        "reviewer": "coding",
        "planner": "coding",
        # (for main pool: "office-expert": "main", "query-12306": "main")
    },
)
```

### 6.4 `bot/service/core.py` — MOD (refactor)

Remove: `_main_agent_cfg`, `_main_memory_cfg`, `_find_subagent_cfg`, single-pool assumption.

New `initialize()` flow:

```python
async def initialize(self) -> None:
    # 1. Load config
    self._app_config = AppConfig.from_yaml(self.config_dir / "bot_config.yml")
    pools_cfg = self._app_config.pools
    if not pools_cfg:
        raise RuntimeError("No pools defined in config/pools/")

    # 2. Shared infrastructure
    self.broker = InMemoryMessageBroker()
    await self.broker.start()

    self.inbox_server = LocalFileInboxServer(workspace=inbox_dir)
    self.inbox_producer = InboxProducer(server=self.inbox_server)
    self.inbox_consumer = InboxConsumer(server=self.inbox_server)
    self.agent_bus = LocalAgentMessageBus(
        producer=self.inbox_producer, consumer=self.inbox_consumer,
        broker=self.broker,
    )
    self.communication_tracker = CommunicationTracker()

    # Shared runtime stores
    self._turn_store = JsonFileTurnStateStore(...)
    self._command_store = JsonFileRuntimeCommandStore(...)
    self._approval_workspace = ...
    self._im_ui = IMUserInterface(output_adapter=self.output_adapter, ...)

    # Shared hook/interceptor base
    shared_hooks = self._collect_run_hooks()
    shared_hook_runner = self._build_hook_runner(shared_hooks)
    shared_interceptor_chain = self._build_interceptor_chain()

    # Retention policy
    retention_cfg = self._app_config.multi_agent.session_retention
    retention = SessionRetentionPolicy(...)

    # 3. Create all pools
    self._pools: dict[str, PoolInstance] = {}
    for pool_name, pool_cfg in pools_cfg.items():
        self._pools[pool_name] = await create_pool(
            pool_name=pool_name,
            pool_cfg=pool_cfg,
            project_dir=self._project_dir,
            broker=self.broker,
            inbox_server=self.inbox_server,
            inbox_producer=self.inbox_producer,
            inbox_consumer=self.inbox_consumer,
            agent_bus=self.agent_bus,
            output_adapter=self.output_adapter,
            safety=self.safety_policy,
            retention=retention,
            comm_tracker=self.communication_tracker,
            turn_store=self._turn_store,
            command_store=self._command_store,
            approval_workspace=self._approval_workspace,
            im_ui=self._im_ui,
            shared_hooks=shared_hooks,
            shared_hook_runner=shared_hook_runner,
            shared_interceptor_chain=shared_interceptor_chain,
        )

    # 4. PoolRouter (replaces BrokerBridgeService input bridging)
    session_store = PoolSessionStore(data_dir=data_dir / "pool_sessions")
    self.pool_router = PoolRouter(
        input_adapter=self.input_adapter,
        output_adapter=self.output_adapter,
        broker=self.broker,
        pools=self._pools,
        session_store=session_store,
    )
```

New `start()`:

```python
async def start(self) -> None:
    await self.input_adapter.start()
    for pool in self._pools.values():
        await pool.broker_bridge.start()
    # PoolRouter is the main loop
    self._router_task = asyncio.create_task(self.pool_router.run())
```

### 6.5 `bot/service/builders.py` — MOD

Generalize existing builders to accept per-pool parameters instead of reading from `self._app_config`/`self._main_agent_cfg`.

Key changes:
- `_register_tools()` → accepts `tool_manager` and `terminal_manager` params
- `_register_mcp_tools()` → accepts `tool_manager` and `mcp_config_file` params
- `_resolve_system_prompt()` → NEW, convention-based prompt loading

## 7. Coding Pool: Complete Hook Mechanism

### 7.1 Agents and their hooks

| Agent | Role | Hooks |
|-------|------|-------|
| **coding** | main (NORMAL) | `InboxFlushHook`, `MaxIterationNotifyHook` |
| **reviewer** | subagent (SUBAGENT) | `InboxFlushHook`, `SubagentAutoSendHook(parent=coding)`, `MaxIterationNotifyHook` |
| **planner** | subagent (SUBAGENT) | `InboxFlushHook`, `SubagentAutoSendHook(parent=coding)`, `MaxIterationNotifyHook` |

### 7.2 Notification Flow

```
reviewer hits max_iterations
  → MaxIterationNotifyHook.after_turn()
    → AgentNotificationService.notify()
      → ctx.session_meta.comm_kind == SUBAGENT
      → parent_map["reviewer"] == "coding"
      → _notify_parent() → agent_bus.send(to="coding's inbox")
        → XML notification arrives in coding agent's inbox

planner forgets send_to_agent_async
  → SubagentAutoSendHook.after_turn()
    → auto-forwards content to coding via agent_bus
    → also sends XML notification via AgentNotificationService
      → same path as above

coding hits max_iterations
  → MaxIterationNotifyHook.after_turn()
    → AgentNotificationService.notify()
      → ctx.session_meta.comm_kind == NORMAL
      → _notify_user() → output_adapter.send()
        → XML notification goes to QQ user directly
```

### 7.3 parent_map per pool

```python
# main pool
parent_map = {
    "office-expert": "main",
    "query-12306": "main",
}

# coding pool
parent_map = {
    "reviewer": "coding",
    "planner": "coding",
}
```

## 8. Architecture Diagram

```
                    QQInputAdapter
                          │
                          ▼
                 ┌─────────────────┐
                 │   PoolRouter    │  ← business layer
                 │   session→pool  │
                 └───┬─────────┬───┘
                     │         │
            ┌────────┘         └────────┐
            ▼                           ▼
 ┌────────────────────┐      ┌────────────────────┐
 │ Pool "main"        │      │ Pool "coding"      │
 │                    │      │                    │
 │ LLMProvider(main)  │      │ LLMProvider(coding)│
 │ MCP: main_mcp.json │      │ MCP: coding_mcp..  │
 │ MemorySystem(main) │      │ MemorySystem(cod)  │
 │ TerminalMgr(main)  │      │ TerminalMgr(coding)│
 │  storage: terminals│      │  storage: terminals│
 │  /main/            │      │  /coding/          │
 │                    │      │                    │
 │ AgentPool ─────────│      │ AgentPool ─────────│
 │  main (NORMAL)     │      │  coding (NORMAL)   │
 │  office-expert(SUB)│      │  reviewer (SUB)    │
 │  query-12306 (SUB) │      │  planner (SUB)     │
 │                    │      │                    │
 │ Hooks:             │      │ Hooks:             │
 │  InboxFlush        │      │  InboxFlush        │
 │  MaxIterNotify     │      │  MaxIterNotify     │
 │  SubagentAutoSend  │      │  SubagentAutoSend  │
 │                    │      │                    │
 │ BrokerBridgeSvc    │      │ BrokerBridgeSvc    │
 │  out: agent:main:..│      │  out: agent:coding │
 └───────┬────────────┘      └───────┬────────────┘
         │                           │
         ▼                           ▼
 ┌────────────────────────────────────────────┐
 │          Shared Infrastructure              │
 │  MessageBroker  InboxServer  OutputAdapter  │
 │  TurnStore  CommandStore  ControlChannel    │
 └────────────────────────────────────────────┘
```

## 9. Files Summary

### Framework (4 files)

| File | Action | Description |
|------|--------|-------------|
| `framework/ioc/configs/pool.py` | **NEW** | `PoolConfig`, `TerminalConfig` Pydantic models |
| `framework/ioc/configs/app.py` | **MOD** | Remove `llm`/`agents`/`mcp`/`memory`/`skills`; add `pools: dict[str, PoolConfig]`; load from `config/pools/` dir |
| `framework/hook/notification.py` | **NEW** | `AgentNotificationService` + `MaxIterationNotifyHook` |
| `framework/hook/builtin/subagent_auto_send.py` | **MOD** | Add optional `notification_service` param |

### Business — bot_project (10 files)

| File | Action | Description |
|------|--------|-------------|
| `config/bot_config.yml` | **MOD** | Shared infra only; remove `agents` section |
| `config/pools/main.yml` | **NEW** | Main pool config |
| `config/pools/coding.yml` | **NEW** | Coding pool config |
| `config/mcp/main_mcp.json` | **NEW** | Main MCP servers (moved from `config/mcp.json`) |
| `config/mcp/coding_mcp.json` | **NEW** | Empty MCP config for coding pool |
| `bot/service/pool_instance.py` | **NEW** | `PoolInstance` dataclass |
| `bot/service/pool_router.py` | **NEW** | `PoolRouter` + `PoolSessionStore` |
| `bot/service/pool_builder.py` | **NEW** | `create_pool()` factory |
| `bot/service/core.py` | **MOD** | Refactor `initialize()` → multi-pool |
| `bot/service/builders.py` | **MOD** | Generalize builders, add `resolve_system_prompt()` |

## 10. Interaction with Existing Slash Command System

### 10.1 Two-Layer Command Handling

PoolRouter and CommandProcessor operate at DIFFERENT layers and don't interfere:

```
QQInputAdapter
    │
    ▼
┌──────────────────────────────────────┐
│ PoolRouter (layer 1: POOL dispatch)  │
│ Regex: ^/([a-z][a-z0-9_-]*)$        │
│ Only matches: /poolname (exact,      │
│ single word, no args, no spaces)     │
│                                      │
│ /coding  → switch pool, reply, DONE  │
│ /hello world → NOT matched, pass     │
│ /approve → NOT matched (unless pool  │
│            named "approve" exists)   │
└──────────────────────────────────────┘
    │ (non-pool-command messages)
    ▼
┌──────────────────────────────────────┐
│ CommandProcessor (layer 2: AGENT     │
│ command handling, inside pipeline)   │
│ Regex: ^/([a-z0-9_-]+)(?: +(.*))?$  │
│                                      │
│ /approve     → approval decision     │
│ /deny        → approval denial       │
│ /continue    → continue agent        │
│ /weather arg → skill command         │
│ /unknown     → "Unknown command"     │
└──────────────────────────────────────┘
```

### 10.2 Non-Conflict Guarantee

The two layers are safe because:

| Command | PoolRouter match? | CommandProcessor match? | Result |
|---------|:---:|:---:|--------|
| `/coding` (pool exists) | YES | never reached | Switch to coding pool |
| `/main` (pool exists) | YES | never reached | Switch to main pool |
| `/approve` (no pool "approve") | NO | YES | Approval handled normally |
| `/deny` (no pool "deny") | NO | YES | Denial handled normally |
| `/continue` (no pool "continue") | NO | YES | Continue handled normally |
| `/weather 上海` | NO (has args+space) | YES | Skill command |
| `/unknown` | NO (no pool match) | YES | "Unknown command" notice |

**Critical guard:** Pool names MUST NOT be `approve`, `deny`, or `continue`. This is enforced at config load time.

### 10.3 Free Pool Switching

Session→pool mapping is independent of agent execution. Switching never interrupts:

```
Turn N:   User sends "帮我写代码"       → main pool, agent starts processing
Turn N+1: User sends "/coding"          → PoolRouter switches session to coding, replies
Turn N+2: User sends "review this"      → coding pool, coding agent starts
          (main pool's Turn N agent continues independently)
Turn N+3: User sends "/main"            → PoolRouter switches session back to main
```

The session→pool map is just a lookup key. Changing it doesn't touch any running agent.

### 10.4 Pool Name Validation

```python
# In AppConfig.from_yaml() or PoolConfig loading
RESERVED_POOL_NAMES = {"approve", "deny", "continue"}

for pool_name in pools:
    if pool_name in RESERVED_POOL_NAMES:
        raise ValueError(
            f"Pool name '{pool_name}' conflicts with built-in command. "
            f"Reserved names: {RESERVED_POOL_NAMES}"
        )
    if not re.match(r"^[a-z][a-z0-9_-]+$", pool_name):
        raise ValueError(
            f"Invalid pool name '{pool_name}'. "
            f"Must match: [a-z][a-z0-9_-]+"
        )
```

### 10.5 Approval Across Pools

Approval snapshots are stored in the **shared** `TurnStateStore`. If a user switches pools while an approval is pending, `/approve` still works — the new pool's pipeline queries the shared store and finds the pending approval.

This is acceptable by design: approval is a session-level concern, not pool-level.

## 11. Key Design Decisions

1. **Pool identity = main agent name**: Derived from `agents[role=main].name` in pool config. Config filename must match. `/pool_name` switch command is the main agent name. No hardcoded pool names anywhere.

2. **Per-pool LLM provider**: Each pool creates its own `LiteLLMProvider` from `pool_cfg.llm`. References `${LLM_API_KEY}` from `.env` → same credentials by default, but pools CAN use different models or keys.

3. **Per-pool MCP**: Each pool config has `mcp.config_file`. Coding pool → empty JSON. Main pool → actual MCP servers.

4. **Per-pool TerminalManager**: Each pool has isolated terminal sessions (shell tabs). Storage: `data/terminals/{pool_name}/`. Coding pool's shell sessions are invisible to main pool and vice versa.

5. **No backward compat**: `config/pools/` is required. `bot_config.yml` has no `agents` section.

6. **PoolRouter zero-hardcoding**: Pool names come from `self._pools.keys()`. Dispatch is `pools.get(name)` — a dict lookup. Reserved name validation prevents conflicts with `/approve`/`/deny`/`/continue`.

7. **Agent names globally unique**: All agents share one MessageBroker → names must be unique across pools. Natural since pool names differ ("main", "coding", etc.).

8. **Pipeline mode untouched**: Multi-pool is pool-mode only. Pipeline mode continues as single AgentPipeline.

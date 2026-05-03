# Repository Guidelines

## Project Structure & Module Organization

`framework/` contains the package source. Core abstractions live in `framework/core/`, ReAct agents in `framework/agents/react/`, orchestration in `framework/pipeline/`, multi-agent coordination in `framework/multi_agent/`, memory in `framework/memory/`, plugins in `framework/plugins/`, tools in `framework/tools/`, and sandbox adapters in `framework/sandbox/`.

**Three-layer control system**:
- `framework/control/` — Runtime control plane: `ControlChannel` (command queue), `ControlEventBus` (pub/sub), `RuntimeStateStore`, unified exceptions (`AgentCancelled`, `ApprovalDenied`, etc.)
- `framework/hook/` — Lifecycle extension points: `HookPoint` enum (9 points), `Hook` Protocol, `HookRunner`. Hooks observe and modify context; do NOT wrap execution.
- `framework/interceptor/` — AOP onion-chain wrapping: `Interceptor` Protocol, `InterceptorChain` (around tool_calls, turns, iterations, LLM streams). Interceptors wrap; they can approve/deny/timeout/transform.

**Current runtime note**: ReAct is now graph-based (`StartNode`, `LLMNode`, `ToolNode`, `EndNode`). Runtime-state persistence uses `RuntimeStateStore`, `JsonFileRuntimeStateStore`, and `NoOpRuntimeStateStore`. The bot project default interceptor chain currently wires `ControlDrainInterceptor` and `ToolResultLimitInterceptor`; turn/tool timeout interceptors are not default runtime wiring. See `docs/current-runtime.md` for the current hook/interceptor/control integration boundaries.

Other packages: `framework/adapters/` (PlatformAdapter, AdapterRegistry), `framework/messaging/` (MessageBroker star-topology), `framework/registry/` (component registry), `framework/extensions/` (optional: LiteLLM, ChromaDB, FAISS, SQLAlchemy), `framework/security/` (SecurityPolicy).

Tests are under `tests/unit/`, `tests/integration/`, and `tests/e2e/`. Documentation in `docs/`, examples in `examples/`, design docs in `agent_docs/`.

## Build, Test, and Development Commands

- `uv venv` creates the local virtual environment.
- `.venv\Scripts\activate` activates it on Windows (`source .venv/bin/activate` on macOS/Linux).
- `uv pip install -e ".[all]"` installs the package with all optional features.
- `uv pip install -e ".[dev]"` installs test, lint, and type-check tooling only.
- `pytest` runs the configured test suite with verbose short tracebacks.
- `pytest tests/unit` runs the fast unit tests.
- `pytest tests/unit/memory/test_auto_compact.py::test_name -xvs` runs a single test.
- `pytest -m "not integration"` skips integration-marked tests.
- `ruff check framework tests` runs lint checks.
- `ruff format framework/` formats code (line-length 100).
- `mypy framework` runs strict type checking for source modules.
- `PYTHONPATH=. python -m pytest examples/bot_project/tests/ -v` runs the example bot project tests.

## Example Project (Primary Reference)

`examples/bot_project/` is the primary end-to-end reference — a QQ Bot demonstrating all subsystems. Two modes:
- `python bot_service.py --mode pipeline` — single AgentPipeline
- `python bot_service.py --mode pool` — AgentPool + BrokerBridgeService (multi-agent)

Configuration: `examples/bot_project/.env` (copy from `.env.example`) for secrets; `config/bot_config.yml` for LLM/memory/tools/multi_agent/plugins.

## Coding Style & Naming Conventions

Use Python 3.11+ syntax and four-space indentation. All framework modules use `from __future__ import annotations` (PEP 563). Keep modules and packages lowercase with underscores, classes in `PascalCase`, functions and variables in `snake_case`, and tests named `test_*.py`. Ruff targets Python 3.11, enforces imports (I), pyupgrade (UP), bugbear (B), flake8-comprehensions (C4), and simplify (SIM), with a 100-character line length ignoring `E501`. Preserve typed interfaces on public APIs.

## Type Safety (from `rule/type-safety.md`)

1. Use enums/constants over raw strings (`MessageRole`, `MessageType`, `FinishReason`, `DefaultValues`, etc.)
2. Use structs over dicts (`ChatMessage`, `ToolCall`, `LLMResponse`, `InputMessage`, `OutputMessage`, etc.)
3. Function signatures must have parameter and return types. No bare `Any` / `list` / `dict` / `list[Any]`.
4. Design abstract classes early — use ABCs/Protocols for cross-cutting concerns, never depend on concrete implementations directly.

## Architecture Conventions

### Generic Type Binding
`Agent[E]`, `ContentEmitter[E]` use `TypeVar("E", bound=AgentEvent)` for compile-time event-enum binding. Each agent defines its own event enum (e.g., `ReActEvent(MODEL_OUTPUT, TOOL_CALL_START, FINAL_OUTPUT, ERROR)`).

### Protocol vs ABC
- `Protocol` for contracts (e.g., `Hook`, `Interceptor`, `ControlChannel`, `LLMProvider`)
- `ABC` + `@abstractmethod` for extension base classes (e.g., `Agent`, `ContentEmitter`, `Tool`)
- `@dataclass` for plain data (e.g., `AgentContext`, `HookPayload`, `ControlCommand`)

### Hook State Safety (Critical)
Per-turn state MUST be stored in `ctx.metadata` (session-scoped dict), NOT instance attributes. This is required for pool mode safety — multiple agents may share the same hook instance concurrently. If you must use instance-level state, key it by `session_id` (e.g., `self._state[session_id]`).

### MessageRole Duplication
`MessageRole` is defined in BOTH `framework/core/types.py` and `framework/core/constants.py`. The one in `types.py` (with AGENT role) is the canonical version; the one in `constants.py` is legacy. Always import from `framework.core.types`.

### TYPE_CHECKING Guard
Use `if TYPE_CHECKING:` for import-only types to avoid circular imports. Most framework files follow this pattern.

### Emitter Modes
- `StreamingAwareEmitter` auto-detects `output_adapter.supports_streaming` — true streaming forwards deltas immediately, pseudo-streaming buffers then flushes.
- `BufferingEmitter` collects all output for tests.
- `wants_streaming()` on `ContentEmitter` tells the agent which LLM API mode to use.

## Testing Guidelines

Pytest discovers `test_*.py` and `*_test.py`, `Test*` classes, and `test_*` functions. Async tests use `pytest-asyncio` in auto mode. Place narrow tests under `tests/unit/`; reserve `tests/integration/` and `tests/e2e/` for cross-component and example-service flows. Mirror framework package structure under `tests/unit/`. Use absolute imports (`from framework.xxx`) in tests. Tag integration tests with `@pytest.mark.integration`. Add regression tests for fixes in shared systems such as memory, tools, multi-agent routing, and sandboxing.

## Commit & Pull Request Guidelines

Recent commits use concise imperative subjects, often with prefixes such as `docs:` or milestone labels like `P6:`. Keep the first line specific, for example `docs: update memory system guide` or `P8: add auto compact service`. Pull requests should describe behavior changes, list verification commands run, link related issues or design notes, and include screenshots only for user-visible bot or documentation changes.

# Type Safety

1. 用枚举/常量代替硬编码字符串（MessageRole、MessageType、FinishReason、DefaultValues 等）
2. 用结构体代替 dict（ChatMessage、ToolCall、LLMResponse、InputMessage、OutputMessage 等）
3. 函数签名必须写参数和返回值类型，禁止 bare Any / list / dict / list[Any]
4. 类设计必须做好抽象化, 禁止直接使用具体实现类, 而是应该使用抽象类或接口, 便于扩展/拔插/自定义
4. framework目录中的都是框架代码, examples中的都是业务代码使用示例, 必须按照业务需求和框架设计/通用性区分修改内容, 例如不能将业务代码中的配置写死到框架中

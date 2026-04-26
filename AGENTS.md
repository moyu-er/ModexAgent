# Repository Guidelines

## Project Structure & Module Organization

`framework/` contains the package source. Core abstractions live in `framework/core/`, ReAct agents in `framework/agents/react/`, orchestration in `framework/pipeline/`, multi-agent coordination in `framework/multi_agent/`, memory in `framework/memory/`, plugins in `framework/plugins/`, tools in `framework/tools/`, and sandbox adapters in `framework/sandbox/`. Tests are under `tests/unit/`, `tests/integration/`, and `tests/e2e/`. Documentation is in `docs/`, examples in `examples/`, and static media in `assets/`.

## Build, Test, and Development Commands

- `uv venv` creates the local virtual environment.
- `.venv\Scripts\activate` activates it on Windows.
- `uv pip install -e ".[all]"` installs the package with all optional features.
- `uv pip install -e ".[dev]"` installs test, lint, and type-check tooling only.
- `pytest` runs the configured test suite with verbose short tracebacks.
- `pytest tests/unit` runs the fast unit tests.
- `pytest -m "not integration"` skips integration-marked tests.
- `ruff check framework tests` runs lint checks.
- `mypy framework` runs strict type checking for source modules.
- `python examples/bot_project/bot_service.py --mode pool` starts the example bot service after configuring `examples/bot_project/.env`.

## Coding Style & Naming Conventions

Use Python 3.11+ syntax and four-space indentation. Keep modules and packages lowercase with underscores, classes in `PascalCase`, functions and variables in `snake_case`, and tests named `test_*.py`. Ruff targets Python 3.11, enforces imports and common correctness rules, and uses a 100-character line length while ignoring `E501`. Preserve typed interfaces on public APIs.

## Testing Guidelines

Pytest discovers `test_*.py` and `*_test.py`, `Test*` classes, and `test_*` functions. Async tests use `pytest-asyncio` in auto mode. Place narrow tests under `tests/unit/`; reserve `tests/integration/` and `tests/e2e/` for cross-component and example-service flows. Add regression tests for fixes in shared systems such as memory, tools, multi-agent routing, and sandboxing.

## Commit & Pull Request Guidelines

Recent commits use concise imperative subjects, often with prefixes such as `docs:` or milestone labels like `P6:`. Keep the first line specific, for example `docs: update memory system guide` or `P8: add auto compact service`. Pull requests should describe behavior changes, list verification commands run, link related issues or design notes, and include screenshots only for user-visible bot or documentation changes.

## Security & Configuration Tips

Do not commit secrets. Copy `examples/bot_project/.env.example` to `.env` and keep API keys, QQ credentials, and provider URLs local. When changing sandbox, tool execution, or MCP integration code, document trust boundaries and add tests for denial paths as well as successful execution.

# Type Safety

1. 用枚举/常量代替硬编码字符串（MessageRole、MessageType、FinishReason、DefaultValues 等）
2. 用结构体代替 dict（ChatMessage、ToolCall、LLMResponse、InputMessage、OutputMessage 等）
3. 函数签名必须写参数和返回值类型，禁止 bare Any / list / dict / list[Any]
4. 类设计必须做好抽象化, 禁止直接使用具体实现类, 而是应该使用抽象类或接口, 便于扩展/拔插/自定义
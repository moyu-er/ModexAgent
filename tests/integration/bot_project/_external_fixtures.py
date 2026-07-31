"""Test fixtures for external pool boot integration tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from modex_agent.adapters.platform import StreamingMode
from modex_agent.core.types import InputMessage, LLMResponse, OutputMessage
from modex_agent.pipeline.adapters import InputAdapter, OutputAdapter

# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------


class _MockProvider:
    """Minimal LLM provider for boot tests; never called for external."""

    async def chat(
        self, messages: list[dict[str, str]] | None = None, **kwargs: object
    ) -> LLMResponse:
        return LLMResponse(content="pong")

    def get_default_model(self) -> str:
        return "mock-model"


class _MockInputAdapter(InputAdapter):
    """Input adapter that exposes `inject` for test-driven messages."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[InputMessage] = asyncio.Queue()
        self._running = False

    @property
    def name(self) -> str:
        return "mock_input"

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def receive(self) -> AsyncIterator[InputMessage]:
        async def _gen() -> AsyncIterator[InputMessage]:
            while self._running:
                try:
                    msg = await asyncio.wait_for(self._queue.get(), timeout=0.05)
                    yield msg
                except TimeoutError:
                    pass

        return _gen()

    async def inject(self, msg: InputMessage) -> None:
        await self._queue.put(msg)


class _MockOutputAdapter(OutputAdapter):
    """Output adapter that captures every outbound message."""

    def __init__(self) -> None:
        self.messages: list[tuple[OutputMessage, str]] = []

    @property
    def name(self) -> str:
        return "mock_output"

    @property
    def streaming_mode(self) -> StreamingMode:
        return StreamingMode.NONE

    async def send(self, message: OutputMessage, session_id: str) -> None:
        self.messages.append((message, session_id))

    async def send_delta(
        self, delta: str, session_id: str, metadata: dict[str, object] | None = None
    ) -> None:
        pass

    async def flush_deltas(self, session_id: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


_BOT_CONFIG = """\
llm:
  model: mock-model
  api_key: test-key
  base_url: ""
  temperature: 0.7
  max_output_tokens: 100

multi_agent:
  enabled: true

tools:
  file_tools:
    enabled: false
  shell_tools:
    enabled: false

mcp:
  servers: {}
"""

_DEFAULT_POOL = """\
main_agent_name: default
system_prompt: "You are the default main agent."
max_steps: 1
tool_preset: minimal
peers:
  - pool_pi
memory:
  short_term:
    max_context_tokens: 100
    budget_ratio: 0.5
"""

_POOL_PI = """\
main_agent_name: pi
system_prompt: "You are the external coding agent."
max_steps: 100
use_terminal: false
terminal_visibility: false
tool_preset: minimal
execution_strategy: external
provider_kind: pi
peers:
  - default
memory:
  short_term:
    max_context_tokens: 100
    budget_ratio: 0.5
"""


_MODEL_YML = """\
default_provider: "Test"
default_model: "mock"
max_context_tokens: 200000
providers:
  - key: test
    name: "Test"
    api_key: "test-key"
    base_url: ""
    models:
      - name: "mock"
        model: "mock-model"
        capabilities: [text]
        temperature: 0.7
        max_output_tokens: 100
"""


def write_bot_config(config_dir: Path) -> None:
    (config_dir / "bot_config.yml").write_text(_BOT_CONFIG, encoding="utf-8")


def write_default_pool(config_dir: Path) -> None:
    pool_dir = config_dir / "pools" / "default"
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / "pool.yml").write_text(_DEFAULT_POOL, encoding="utf-8")


def write_pool_pi(config_dir: Path) -> None:
    pool_dir = config_dir / "pools" / "pool_pi"
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / "pool.yml").write_text(_POOL_PI, encoding="utf-8")


def write_model_config(config_dir: Path) -> None:
    (config_dir / "model.yml").write_text(_MODEL_YML, encoding="utf-8")


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bot_service_config(tmp_path: Path) -> Path:
    """Return a temp bot_project config dir with default + pool_pi pools."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    write_bot_config(config_dir)
    write_default_pool(config_dir)
    write_pool_pi(config_dir)
    write_model_config(config_dir)
    return config_dir


@pytest.fixture
def mock_provider() -> _MockProvider:
    return _MockProvider()


@pytest.fixture
def mock_input_adapter() -> _MockInputAdapter:
    return _MockInputAdapter()


@pytest.fixture
def mock_output_adapter() -> _MockOutputAdapter:
    return _MockOutputAdapter()

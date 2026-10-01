"""The mini project's plugin — one file, three component slots.

Registered through the standard plugin loading contract: the framework's
``ComponentRegistryLoader`` imports every ``*.py`` under ``mini_plugins/``
(the plugin dir named on ``AppAssemblyRoots``), instantiates the ``Plugin``
subclass, and hands it a ``PluginRegistrationContext``. The declaration
(``config/scopes/mini.yml``) references the registered names — nothing in
the framework knows this file exists.

Components:

- ``mini_echo`` (TOOL slot) — appends text to a log file under the
  workspace data dir and returns it; the observable effect of "the custom
  tool ran".
- ``mini_turn_logger`` (HOOK slot) — after each turn, appends one line to
  ``mini_turns.log``; the observable effect of "the custom hook fired".
- ``mini_scripted`` (LLM_PROVIDER slot) — a fully offline scripted
  provider: the first call asks for the ``mini_echo`` tool, the second
  answers with a fixed line. No network, no keys, no real model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import FinishReason
from modex_agent.core.message import MessageRole
from modex_agent.core.provider import LLMProvider
from modex_agent.core.stream_events import Finish, LLMStreamEvent, TextDelta, ToolCallComplete
from modex_agent.core.tool_manager import Tool
from modex_agent.hook.abc import AfterTurnHook
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.plugins.loader import Plugin, PluginRegistrationContext
from modex_agent.scope.components import ComponentFactory, ReactHookFactory

ECHO_TOOL_NAME = "mini_echo"
TURN_LOGGER_HOOK_NAME = "mini_turn_logger"
SCRIPTED_PROVIDER_NAME = "mini_scripted"

ECHO_TOOL_CALL_TEXT = "hello from the mini echo tool"
SCRIPTED_FINAL_TEXT = "mini turn complete — echo tool ran."


# ── TOOL slot: mini_echo ────────────────────────────────────────────────────


class MiniEchoConfig(BaseModel):
    """Roster-facing config: the log file name under the workspace data dir."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    filename: str = "mini_echo.log"


class MiniEchoTool(Tool):
    """Appends ``text`` to the log file and returns it (a real side effect)."""

    def __init__(self, log_path: Path) -> None:
        super().__init__()
        self._log_path = log_path

    @property
    def name(self) -> str:
        return ECHO_TOOL_NAME

    @property
    def description(self) -> str:
        return "Append text to the mini echo log and return it."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to echo."}
            },
            "required": ["text"],
        }

    async def execute(self, **kwargs: Any) -> str:
        text = str(kwargs.get("text", ""))
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
        return f"echoed: {text}"


class MiniEchoToolFactory(ComponentFactory):
    """TOOL-slot factory — the workspace data dir comes from the ctx chain."""

    config_model = MiniEchoConfig

    async def create(self, config: MiniEchoConfig, ctx: AssemblyContext) -> Tool:
        return MiniEchoTool(ctx.workspace_ctx.paths.root / config.filename)


# ── HOOK slot: mini_turn_logger ─────────────────────────────────────────────


class MiniTurnLoggerConfig(BaseModel):
    """Roster-facing config: the turn-log file name under the data dir."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    filename: str = "mini_turns.log"


class MiniTurnLoggerHook(AfterTurnHook):
    """After each turn, append one line: session id + result preview."""

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path

    @property
    def name(self) -> str:
        return TURN_LOGGER_HOOK_NAME

    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        preview = (result.content or "")[:60].replace("\n", " ")
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(f"turn session={ctx.session.session_id} result={preview!r}\n")


class MiniTurnLoggerHookFactory(ReactHookFactory):
    """HOOK-slot factory targeting the react hook runner."""

    config_model = MiniTurnLoggerConfig

    async def create(
        self, config: MiniTurnLoggerConfig, ctx: AssemblyContext
    ) -> MiniTurnLoggerHook:
        return MiniTurnLoggerHook(ctx.workspace_ctx.paths.root / config.filename)


# ── LLM_PROVIDER slot: mini_scripted ────────────────────────────────────────


class MiniScriptedConfig(BaseModel):
    """No construction knobs — the script is fixed."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class MiniScriptedProvider(LLMProvider):
    """Offline two-step script: call ``mini_echo``, then answer.

    Call 1 asks the ``mini_echo`` tool to echo the user's message (finish
    TOOL_CALLS); call 2 answers with a fixed line (finish STOP). The step
    advances per stream, so one scripted turn is exactly one echo
    round-trip — enough for the demo and the CI anchor.
    """

    def __init__(self) -> None:
        super().__init__()
        self._tool_call_issued = False

    def get_default_model(self) -> str:
        return "mini-scripted"

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        if not self._tool_call_issued:
            self._tool_call_issued = True
            user_text = next(
                (
                    message.content
                    for message in reversed(request.messages)
                    if message.role is MessageRole.USER and message.content
                ),
                ECHO_TOOL_CALL_TEXT,
            )
            yield ToolCallComplete(
                call_id="mini-call-1",
                tool_name=ECHO_TOOL_NAME,
                arguments={"text": user_text},
            )
            yield Finish(finish_reason=FinishReason.TOOL_CALLS)
        else:
            yield TextDelta(text=SCRIPTED_FINAL_TEXT)
            yield Finish(finish_reason=FinishReason.STOP)


class MiniScriptedProviderFactory(ComponentFactory):
    """LLM_PROVIDER-slot factory — a fresh scripted provider per resolution."""

    config_model = MiniScriptedConfig

    async def create(
        self, config: MiniScriptedConfig, ctx: AssemblyContext
    ) -> LLMProvider:
        _ = config, ctx
        return MiniScriptedProvider()


# ── Plugin entry point ──────────────────────────────────────────────────────


class MiniPlugin(Plugin):
    """Discovered by the plugin loader; one file, three slots."""

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_tool(ECHO_TOOL_NAME, MiniEchoToolFactory())
        ctx.register_hook(TURN_LOGGER_HOOK_NAME, MiniTurnLoggerHookFactory())
        ctx.register_provider(SCRIPTED_PROVIDER_NAME, MiniScriptedProviderFactory())


__all__ = [
    "ECHO_TOOL_CALL_TEXT",
    "SCRIPTED_FINAL_TEXT",
    "MiniPlugin",
]

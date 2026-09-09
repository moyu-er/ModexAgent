"""Subprocess entry for ``tests/acp/test_stdio_runtime.py``.

Runs the PRODUCTION ACP stdio stack — ``bot.acp.runtime.run_acp_entry``
-> framework ``modex_agent.acp.entry.main`` over the real ``AcpRuntime`` +
``BotService`` (pool assembly, input pipeline, approval, session tree) —
against a temporary config directory. The single test seam is the model
provider factory at its real assembly boundary: ``BotModelProvider``
constructs every real provider through the module-level
``create_llm_provider`` binding in ``bot.service.model_provider`` (the one
construction seam all pools route through), replaced here with a scripted
``CallbackStreamProvider``. No network; everything else is production.

The temporary ``model.yml`` must configure a REAL provider entry — a missing
model.yml makes BotService boot with the ``_unconfigured`` placeholder,
which fails fast in ``BotModelProvider.chat_stream`` BEFORE any provider is
constructed (the factory seam would never run).

Scripted reply contract (keyed on the newest non-reminder user turn):

- ``WRITE <path> <content>`` with no tool result after that user message
  -> one ``write`` tool call (``path`` is passed through VERBATIM — a
  relative path here proves the production workspace-scoped tool wrapper
  resolves it against the bound project root, not the process cwd);
- a tool result after the newest user message -> STOP echoing a snippet;
- anything else -> ``ack: <text>``.

Usage (from the test): ``python tests/acp/acp_stdio_agent.py <config-dir>``

The script lives in ``tests/acp/`` — NOT ``tests/`` — on purpose:
``python <script>`` puts the script's own directory at ``sys.path[0]``, and
``tests/`` contains a ``bot/`` shadow package (test fixtures) that would
shadow the real ``bot`` package on ``import bot``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_ACP_TESTS_DIR = Path(__file__).resolve().parent  # .../bot_project/tests/acp
_BOT_PROJECT = _ACP_TESTS_DIR.parents[1]  # .../bot_project
_REPO_ROOT = _BOT_PROJECT.parent.parent  # the repository root
for _entry in (str(_REPO_ROOT / "src"), str(_BOT_PROJECT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from modex_agent.core.llm_struct import FinishReason, LLMResponse
from modex_agent.core.message import ChatMessage, MessageRole, ToolCall
from modex_agent.core.provider import CallbackStreamProvider

WRITE_DIRECTIVE = "WRITE "


class ScriptedProvider(CallbackStreamProvider):
    """Deterministic replies — see module docstring for the contract."""

    def __init__(self) -> None:
        super().__init__(retry_backoff_seconds=())

    def get_default_model(self) -> str:
        return "scripted-acp"

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Any = None,
        on_reasoning_delta: Any = None,
        **kwargs: Any,
    ) -> LLMResponse:
        _ = (
            model,
            temperature,
            max_output_tokens,
            tools,
            on_content_delta,
            on_reasoning_delta,
            kwargs,
        )
        return _reply(messages)


def _reply(messages: list[ChatMessage]) -> LLMResponse:
    last_user_index = -1
    last_user_text = ""
    for index, message in enumerate(messages):
        if message.role is not MessageRole.USER:
            continue
        text = message.content if isinstance(message.content, str) else ""
        if text.startswith("<system-reminder"):
            continue
        last_user_index = index
        last_user_text = text
    tool_after = next(
        (m for m in messages[last_user_index + 1 :] if m.role is MessageRole.TOOL),
        None,
    )
    if last_user_text.startswith(WRITE_DIRECTIVE) and tool_after is None:
        _, path, content = last_user_text.split(" ", 2)
        return LLMResponse(
            content=None,
            finish_reason=FinishReason.TOOL_CALLS,
            tool_calls=[
                ToolCall(
                    tool_name="write",
                    arguments={"path": path, "content": content},
                    call_id="call-write-1",
                )
            ],
        )
    if tool_after is not None:
        snippet = str(tool_after.content or "")[:120]
        return LLMResponse(
            content=f"tool-finished: {snippet}", finish_reason=FinishReason.STOP
        )
    return LLMResponse(content=f"ack: {last_user_text}", finish_reason=FinishReason.STOP)


def _scripted_factory(config: object) -> ScriptedProvider:
    _ = config
    return ScriptedProvider()


def main() -> None:
    import faulthandler
    faulthandler.dump_traceback_later(10, file=sys.stderr)
    if len(sys.argv) != 2:
        raise SystemExit("usage: acp_stdio_agent.py <config-dir>")
    import bot.service.model_provider as model_provider_module

    model_provider_module.create_llm_provider = _scripted_factory

    from bot.acp.runtime import run_acp_entry

    run_acp_entry(Path(sys.argv[1]), pool_override="main")


if __name__ == "__main__":
    main()

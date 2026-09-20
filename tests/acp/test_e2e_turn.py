"""End-to-end ACP turn test: spawn ``python -m modex_agent.acp`` over stdio and
drive it with the SDK client against the framework's scripted backend.

This is the regression guard for SDK upgrades (ADR-0049 pins
``agent-client-protocol==0.12.1``; re-run this suite when bumping it).
Asserts the conservative declared surface: default mode only, once-only
permission options, text-only prompts, no load/MCP capabilities.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import acp
import pytest
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    AllowedOutcome,
    PermissionOption,
    RequestPermissionResponse,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
)

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


class RecordingClient:
    """Minimal SDK client: records session updates, auto-allows permissions."""

    def __init__(self) -> None:
        self.updates: list[Any] = []
        self.permission_requests: list[tuple[ToolCallUpdate, list[PermissionOption]]] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append(update)

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        self.permission_requests.append((tool_call, options))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=options[0].option_id)
        )


def _agent_env() -> dict[str, str]:
    env = dict(os.environ)
    pythonpath = str(SRC_ROOT)
    if env.get("PYTHONPATH"):
        pythonpath = pythonpath + os.pathsep + env["PYTHONPATH"]
    env["PYTHONPATH"] = pythonpath
    return env


async def _wait_for_updates(client: RecordingClient, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not client.updates:
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("timed out waiting for session updates")
        await asyncio.sleep(0.05)


async def test_acp_stdio_turn_end_to_end() -> None:
    client = RecordingClient()
    with tempfile.TemporaryDirectory() as project_dir:
        async with acp.spawn_agent_process(
            client, sys.executable, "-m", "modex_agent.acp", env=_agent_env()
        ) as (conn, _proc):
            # 1. initialize negotiates protocolVersion=1 and declares the
            #    conservative capability set (no load/MCP/image; no boot happened)
            init = await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
            assert init.protocol_version == 1
            caps = init.agent_capabilities
            assert caps is not None
            assert caps.load_session is False
            assert caps.prompt_capabilities is not None
            assert not caps.prompt_capabilities.image
            assert caps.mcp_capabilities is not None
            assert not caps.mcp_capabilities.http

            # 2. new_session passes the protocol cwd to the scripted backend
            #    and advertises the default mode only
            session = await conn.new_session(cwd=project_dir)
            assert session.session_id
            assert session.modes is not None
            assert [m.id for m in session.modes.available_modes] == ["default"]
            assert session.modes.current_mode_id == "default"

            # 3. a normal prompt streams thought/message/tool updates and ends the turn
            response = await conn.prompt(
                session_id=session.session_id, prompt=[acp.text_block("hello world")]
            )
            assert response.stop_reason == "end_turn"
            assert any(isinstance(u, AgentThoughtChunk) for u in client.updates)
            assert any(isinstance(u, AgentMessageChunk) for u in client.updates)
            starts = [u for u in client.updates if isinstance(u, ToolCallStart)]
            progresses = [u for u in client.updates if isinstance(u, ToolCallProgress)]
            assert starts, "expected a tool_call start update"
            assert progresses, "expected a tool_call_update progress update"
            assert starts[0].tool_call_id.endswith(":call-1")
            assert starts[0].kind == "read"
            assert progresses[0].tool_call_id == starts[0].tool_call_id
            assert progresses[0].status == "completed"

            # 4. request_permission round-trips through the scripted approval turn;
            #    the permission card reuses the streamed tool_call id and only the
            #    once-kind options are offered
            client.updates.clear()
            response = await conn.prompt(
                session_id=session.session_id, prompt=[acp.text_block("approve rm -rf /tmp/x")]
            )
            assert response.stop_reason == "end_turn"
            assert len(client.permission_requests) == 1
            tool_call, options = client.permission_requests[0]
            streamed = [
                u for u in client.updates if isinstance(u, ToolCallStart) and u.kind == "execute"
            ]
            assert streamed, "expected the streamed Bash tool_call card"
            assert tool_call.tool_call_id == streamed[0].tool_call_id
            assert [o.option_id for o in options] == ["allow_once", "reject_once"]

            # 5. session/cancel interrupts a waiting turn -> stop_reason "cancelled"
            client.updates.clear()
            prompt_task = asyncio.create_task(
                conn.prompt(session_id=session.session_id, prompt=[acp.text_block("wait")])
            )
            await _wait_for_updates(client)
            await conn.cancel(session_id=session.session_id)
            response = await asyncio.wait_for(prompt_task, timeout=10)
            assert response.stop_reason == "cancelled"

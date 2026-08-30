"""Machine capture of the retired communication briefs' rendered content.

T16 golden discipline (the T12/T14 recipe): this script ran on the
PRE-migration HEAD, where ``AgentCommunicationSystemPromptProvider`` (the
2c composite) rendered the three communication briefs. It captures each
brief's rendered fragment plus representative composite joins into
``subagents_section_briefs.json``; the post-migration test asserts the
capability-channel section providers reproduce the fragments byte-for-byte
(the anchor position and the three-section split are the documented
designed deltas — content byte-equality is the bar).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from modex_agent.core.agent import AgentCommKind
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.memory.prompt_pipeline.providers import (
    AgentCommunicationSystemPromptProvider,
)
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.communication import AgentCommunicationService
from modex_agent.multi_agent.tools import (
    CommunicationTarget,
    CommunicationTargetStore,
    SendToPeerTool,
    TaskDispatchTool,
)

_GOLDEN_DIR = Path(__file__).resolve().parent


def _service() -> AgentCommunicationService:
    return AgentCommunicationService(
        source=AgentAddress(name="capture"),
        registry=None,  # type: ignore[arg-type]
        tree=None,  # type: ignore[arg-type]
    )


def _task_tool() -> TaskDispatchTool:
    return TaskDispatchTool(
        store=CommunicationTargetStore(),
        source=AgentAddress(name="capture"),
        service=_service(),
    )


def _peer_tool(*remote: str, local: str | None = None) -> SendToPeerTool:
    from unittest.mock import MagicMock

    from modex_agent.multi_agent.session_tree.manager import SessionTreeManager

    store = CommunicationTargetStore()
    for name in remote:
        store.add(
            CommunicationTarget(
                name=name,
                kind=AgentCommKind.NORMAL,
                pool_name=f"pool-{name}",
                # any non-None tree reference marks the target remote
                tree_ref=MagicMock(spec=SessionTreeManager),
            )
        )
    if local is not None:
        store.add(
            CommunicationTarget(
                name=local,
                kind=AgentCommKind.NORMAL,
                pool_name=f"pool-{local}",
            )
        )
    return SendToPeerTool(
        store=store,
        source=AgentAddress(name="capture"),
        service=_service(),
    )


def _manager(*tools: Any) -> InMemoryToolManager:
    manager = InMemoryToolManager()
    for tool in tools:
        manager.register(tool)
    return manager


async def capture() -> dict[str, str]:
    """Render the brief fragments + representative composite joins."""
    import asyncio

    async def render(provider: AgentCommunicationSystemPromptProvider) -> str:
        return await provider._fetch_content()

    task = _task_tool()
    peer_two = _peer_tool("beta", "gamma", local="alpha")

    # Each brief in isolation (the fragment the section provider must
    # reproduce byte-for-byte).
    delegation_only = await render(
        AgentCommunicationSystemPromptProvider(_manager(task), AgentCommKind.NORMAL)
    )
    consultation_only = await render(
        AgentCommunicationSystemPromptProvider(_manager(), AgentCommKind.SUBAGENT)
    )
    peer_only = await render(
        AgentCommunicationSystemPromptProvider(_manager(peer_two), AgentCommKind.NORMAL)
    )
    # The full composite (peer + consultation + delegation, old join order)
    # and the empty shape — captured for the record; the post-migration
    # channel splits these into three separately-joined sections.
    composite_all = await render(
        AgentCommunicationSystemPromptProvider(
            _manager(task, peer_two), AgentCommKind.SUBAGENT
        )
    )
    composite_none = await render(
        AgentCommunicationSystemPromptProvider(_manager(), AgentCommKind.NORMAL)
    )
    return {
        "delegation": delegation_only,
        "consultation": consultation_only,
        "peer_two_remote_one_local": peer_only,
        "composite_all": composite_all,
        "composite_none": composite_none,
    }


def main() -> None:
    import asyncio

    document = asyncio.run(capture())
    target = _GOLDEN_DIR / "subagents_section_briefs.json"
    target.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="",
    )
    print(f"wrote {target}")


if __name__ == "__main__":
    main()

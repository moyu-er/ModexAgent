"""mini_project — the framework's second consumer.

The whole project is: one scope declaration (``config/scopes/mini.yml``),
one tiny AppConfig (``config/app.yml``), one plugin file
(``mini_plugins/mini.py``), and this entry point. The assembly itself is
framework-owned: ``RunnableAppService`` boots every declared pool through
the production road (declaration → component registry → ``create_pool``)
with zero assembly code copied from examples/bot_project.

Run:  python examples/mini_project/main.py   (fully offline, scripted LLM)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from modex_agent.adapters.output import OutputAdapter
from modex_agent.app.roots import AppAssemblyRoots
from modex_agent.app.runnable import RunnableAppService
from modex_agent.core.emitter import TurnBinding, TurnEventSink
from modex_agent.core.session_id import SessionInfo
from modex_agent.messaging.models import InputMessage, OutputMessage
from modex_agent.pipeline.adapters import InputAdapter
from modex_agent.presentation import ConsolePresenter, SessionEventHub

PROJECT_DIR = Path(__file__).resolve().parent


def _console_emitter_factory(binding: TurnBinding) -> TurnEventSink:
    """One console presenter per bound turn — the framework PresentationSink
    seam in action: the hub projects the core stream, the presenter renders
    it on stdout."""
    return SessionEventHub(binding, (ConsolePresenter(),))


class DemoInputAdapter(InputAdapter):
    """A one-message in-process channel (a deployment-owned adapter)."""

    @property
    def name(self) -> str:
        return "demo"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def receive(self) -> AsyncIterator[InputMessage]:
        yield InputMessage(
            content="hello from the mini demo",
            session=SessionInfo(session_id="demo.main", agent_name="main"),
        )


class CollectorOutputAdapter(OutputAdapter):
    """Collects every sent message — the demo's reply sink."""

    def __init__(self) -> None:
        self.messages: list[OutputMessage] = []

    @property
    def name(self) -> str:
        return "collector"

    async def send(self, message: OutputMessage, session_id: str) -> None:
        self.messages.append(message)


def build_service(project_dir: Path) -> RunnableAppService:
    """Build the service against ``project_dir`` (also the test's entry)."""
    roots = AppAssemblyRoots(
        config_dir=project_dir / "config",
        resource_root=project_dir,
        workspace_home=project_dir,
        plugins_dir_name="mini_plugins",
        scope_declaration_file="mini.yml",
    )
    return RunnableAppService(
        config_dir=roots.config_dir,
        input_adapter=DemoInputAdapter(),
        output_adapter=CollectorOutputAdapter(),
        emitter_factory=_console_emitter_factory,
        roots=roots,
    )


async def main() -> None:
    service = build_service(PROJECT_DIR)
    await service.initialize()
    try:
        result = await service.turn("hello from the mini demo", session="demo")
        print("turn outcome:", result.outcome)
        print("agent reply:", result.agent_result.content if result.agent_result else None)
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())

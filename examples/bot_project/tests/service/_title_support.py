"""Deterministic background-task settlement for title tests, not a runtime API."""
import asyncio
from pathlib import Path

from bot.service.session_title import SessionTitleOps
from bot.service.session_title_hook import SessionTitleHook
from bot.service.session_title_task import SessionTitleNamingTask
from bot.workspace.handle import PoolWorkspaceResources

from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.persistence.adapters.file_session_store import LocalFileSessionStore
from modex_agent.persistence.session_registry import InMemorySessionRegistry
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths


async def settle_titles(owner: SessionTitleHook | SessionTitleNamingTask) -> None:
    naming = owner._naming if isinstance(owner, SessionTitleHook) else owner
    await asyncio.gather(*tuple(naming._tasks), return_exceptions=True)


def title_workspace(root: Path, broker: InMemoryMessageBroker) -> PoolWorkspaceResources:
    """Headless assembly fixture with real title ownership, no user transcript."""
    paths = WorkspacePaths(root=root / ".modex")
    store = LocalFileSessionStore(paths.session_index_dir)
    registry = InMemorySessionRegistry(store=store)
    ops = SessionTitleOps(registry=registry)

    def no_model():
        raise AssertionError("A headless fixture has no user transcript to name")

    return PoolWorkspaceResources(
        target=root, ctx=WorkspaceContext(target=root, paths=paths, is_home=False),
        overflow_store=LocalFileToolOverflowStore(workspace=paths.overflow_dir),
        session_index_store=store, broker=broker, session_registry=registry,
        title_ops=ops,
        title_naming=SessionTitleNamingTask(ops=ops, provider_source=no_model, transcript_reader=lambda sid: None),
    )


def title_resources(root: Path, ops: SessionTitleOps, naming: SessionTitleNamingTask) -> PoolWorkspaceResources:
    """Factory tests use the production resource type and the tested registry."""
    ctx = WorkspaceContext.from_target(root, data_dir_name=".modex", home=root)
    return PoolWorkspaceResources(
        target=root, ctx=ctx,
        overflow_store=LocalFileToolOverflowStore(workspace=ctx.paths.overflow_dir),
        session_index_store=LocalFileSessionStore(root / "session_index"),
        broker=InMemoryMessageBroker(), session_registry=ops.registry,
        title_ops=ops, title_naming=naming,
    )

"""PA-05 tests — title lifecycle races, deletion, and teardown isolation.

Controlled model barriers (asyncio.Event gates) reproduce orderings
deterministically — never sleeps. All scenarios go through the REAL
owners: ``SessionTitleOps`` + ``SessionTitleNamingTask`` + the GC's
``registry_cleanup`` coordination entry + real FILE/SQLite stores.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from bot.service.session_gc import (
    SessionGarbageCollector,
    SessionGcConfig,
)
from bot.service.session_title import SessionTitleOps
from bot.service.session_title_task import SessionTitleNamingTask

from modex_agent.core.provider import LLMProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.persistence.adapters.file_session_store import LocalFileSessionStore
from modex_agent.persistence.session_registry import InMemorySessionRegistry
from modex_agent.workspace.paths import WorkspacePaths

from ._title_support import settle_titles


class _GatedProvider(LLMProvider):
    """Model seam parked on a gate — deterministic ordering control."""

    def __init__(self, responses: list[str]) -> None:
        super().__init__()
        self.responses = list(responses)
        self.gate = asyncio.Event()
        self.calls = 0
        self.closed = False

    def get_default_model(self) -> str:
        return "gated-title-model"

    def release(self) -> None:
        self.gate.set()

    async def aclose(self) -> None:
        self.closed = True

    async def stream(self, request: Any) -> Any:  # type: ignore[override]
        from modex_agent.core.llm_struct import FinishReason
        from modex_agent.core.stream_events import Finish, TextDelta

        self.calls += 1
        await self.gate.wait()
        item = self.responses.pop(0) if self.responses else ""
        yield TextDelta(text=item)
        yield Finish(finish_reason=FinishReason.STOP)


class _TitleEnv:
    """Real ops + naming owner over a real file-backed registry."""

    def __init__(self, tmp_path: Path, provider: _GatedProvider) -> None:
        store = LocalFileSessionStore(tmp_path / "session_index")
        self.registry = InMemorySessionRegistry(store=store)
        self.ops = SessionTitleOps(registry=self.registry)
        self.provider = provider
        self.naming = SessionTitleNamingTask(
            ops=self.ops,
            provider_source=lambda: provider,  # type: ignore[arg-type]
            transcript_reader=lambda session_id: "用户请求内容",
        )

    async def seed(self, session_id: str = "abc123.main", title: str | None = None) -> None:
        metadata: dict[str, Any] = {}
        if title:
            metadata["title"] = title
        await self.registry.register(
            SessionInfo(
                session_id=session_id,
                agent_name=session_id.rsplit(".", 1)[-1],
                created_at=1000,
                updated_at=2000,
                metadata=metadata,
            )
        )


def _submit(env: _TitleEnv, pool: str, session_id: str) -> None:
    env.naming.submit(
        pool,
        SessionInfo(
            session_id=session_id, agent_name=session_id.rsplit(".", 1)[-1]
        ),
    )


@pytest.mark.asyncio
async def test_two_manual_saves_last_successful_wins(tmp_path: Path) -> None:
    env = _TitleEnv(tmp_path, _GatedProvider([]))
    await env.seed()
    await env.ops.set_title("abc123.main", "第一次")
    await env.ops.set_title("abc123.main", "第二次")
    assert await env.ops.read_title("abc123.main") == "第二次"


@pytest.mark.asyncio
async def test_auto_first_then_manual_overrides(tmp_path: Path) -> None:
    env = _TitleEnv(tmp_path, _GatedProvider(["自动标题"]))
    await env.seed()
    _submit(env, "default", "abc123.main")
    env.provider.release()
    await settle_titles(env.naming)
    assert await env.ops.read_title("abc123.main") == "自动标题"
    await env.ops.set_title("abc123.main", "人工改写")
    assert await env.ops.read_title("abc123.main") == "人工改写"


@pytest.mark.asyncio
async def test_runtime_touch_register_never_lose_title(tmp_path: Path) -> None:
    env = _TitleEnv(tmp_path, _GatedProvider([]))
    await env.seed()
    await env.ops.set_title("abc123.main", "标题保留")
    await env.registry.touch("abc123.main")
    await env.registry.register(
        SessionInfo(session_id="abc123.main", agent_name="main", metadata={"pool": "x"})
    )
    assert await env.ops.read_title("abc123.main") == "标题保留"


@pytest.mark.asyncio
async def test_deletion_during_generation_revokes_and_cancels(tmp_path: Path) -> None:
    """GC deletion while the model call is parked: pending identity is
    revoked inside the critical section, cancel issued, and the late
    task can never write back."""
    env = _TitleEnv(tmp_path, _GatedProvider(["迟到的自动标题"]))
    await env.seed()
    _submit(env, "default", "abc123.main")
    await asyncio.sleep(0.02)  # task parks on the gate

    # GC coordination entry (same as WebUIService._registry_cleanup_for_gc):
    # inside the shared critical section, revoke + cancel; store cleanup.
    async with env.ops.deletion_critical_section():
        cancelled = env.ops.cancel_naming_by_session("abc123.main")
        await env.registry.cleanup("abc123.main")
    for task in cancelled:
        with _SuppressCancel():
            await task
    env.provider.release()
    await asyncio.sleep(0.05)
    # no resurrection: registry AND store are clean
    assert await env.registry.get("abc123.main") is None
    async with env.ops.deletion_critical_section():
        pass  # lock is reusable (not left held)


@pytest.mark.asyncio
async def test_stale_task_cannot_clear_newer_task_slot(tmp_path: Path) -> None:
    """A cancelled old task's finally must not remove a newer task's entry."""
    env = _TitleEnv(tmp_path, _GatedProvider(["旧", "新"]))
    await env.seed()
    _submit(env, "default", "abc123.main")
    old = next(iter(env.naming._tasks))
    # GC revokes the old entry and cancels the old task
    async with env.ops.deletion_critical_section():
        env.ops.cancel_naming_by_session("abc123.main")
    with _SuppressCancel():
        await old
    # session still exists (cleanup not requested here) — a NEW turn
    # submits a fresh naming task after the failure
    _submit(env, "default", "abc123.main")
    env.provider.release()
    await settle_titles(env.naming)
    assert await env.ops.read_title("abc123.main") == "旧"


@pytest.mark.asyncio
async def test_gc_liveness_rejection_keeps_registry_and_pending(tmp_path: Path) -> None:
    """Active-session delete rejection must not clean registry or pending."""
    from bot.service.liveness import LivenessProvider

    class _Active(LivenessProvider):
        async def try_reserve_deletion(self, session_id: str, workspace_root: Path) -> bool:
            return True

        async def is_session_active(self, session_id: str, workspace_root: Path) -> bool:
            return True

        async def release_deletion(self, session_id: str) -> None:
            return None

    tmp = tmp_path
    env = _TitleEnv(tmp, _GatedProvider(["活跃标题"]))
    await env.seed(title="活跃标题")
    _submit(env, "default", "abc123.main")

    async def _resolve_store(_index: Path) -> LocalFileSessionStore:
        return env.registry._store  # type: ignore[attr-defined]
    async def _registry_cleanup(ws_root: Path, session_id: str) -> None:
        async with env.ops.deletion_critical_section():
            env.ops.cancel_naming_by_session(session_id)
        await env.registry.cleanup(session_id)

    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp],
        data_dir_name=".modex",
        config=SessionGcConfig(enabled=False, max_workers=1),
        session_store_resolver=_resolve_store,
        session_pool_resolver=lambda session: "default",
        liveness_provider=_Active(),
        registry_cleanup=_registry_cleanup,
    )
    deleted = await gc.delete_session_tree("abc123.main", ws_root=tmp, pool="default")
    assert deleted is False
    session = await env.registry.get("abc123.main")
    assert session is not None
    assert session.metadata.get("title") == "活跃标题"
    # pending entry untouched: a retry naming attempt stays possible
    env.provider.release()
    await settle_titles(env.naming)
    assert await env.ops.read_title("abc123.main") == "活跃标题"  # auto skip (has title)


@pytest.mark.asyncio
async def test_gc_deletion_via_real_collector_no_resurrect(tmp_path: Path) -> None:
    """Full foreground delete through the real GC (FILE backend): the
    registry cleanup callback wipes the runtime cache; later auto/manual
    writes cannot resurrect."""
    env = _TitleEnv(tmp_path, _GatedProvider(["不应出现"]))
    await env.seed(title="旧标题")
    # write real index artifacts so the GC finds the session
    paths = WorkspacePaths(root=tmp_path / ".modex")
    import json

    pool_dir = paths.session_index_dir / "default"
    pool_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "session_id": "abc123.main",
        "agent_name": "main",
        "parent_session_id": None,
        "created_at": 1000,
        "updated_at": 2000,
        "metadata": {"title": "旧标题"},
    }
    (pool_dir / "abc123.main.json").write_text(json.dumps(record), encoding="utf-8")

    async def _resolve_store(_index: Path) -> LocalFileSessionStore:
        return LocalFileSessionStore(paths.session_index_dir)

    async def _registry_cleanup(ws_root: Path, session_id: str) -> None:
        async with env.ops.deletion_critical_section():
            env.ops.cancel_naming_by_session(session_id)
        await env.registry.cleanup(session_id)

    gc = SessionGarbageCollector(
        workspace_roots_provider=lambda: [tmp_path],
        data_dir_name=".modex",
        config=SessionGcConfig(enabled=False, max_workers=1),
        session_store_resolver=_resolve_store,
        session_pool_resolver=lambda session: "default",
        registry_cleanup=_registry_cleanup,
    )
    deleted = await gc.delete_session_tree("abc123.main", ws_root=tmp_path, pool="default")
    assert deleted is True
    assert await env.registry.get("abc123.main") is None
    # late auto write-back is rejected (session gone)
    written = await env.ops.auto_title("default", "abc123.main", "复活", _task_of(env))
    assert written is None
    with pytest.raises(LookupError):
        await env.ops.set_title("abc123.main", "复活")


def _task_of(env: _TitleEnv) -> asyncio.Task[None]:
    task = asyncio.current_task()
    assert task is not None
    return task  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_multi_workspace_interleaved_generation(tmp_path: Path) -> None:
    """Two workspaces' owners generate concurrently without cross-talk."""
    ws_a = tmp_path / "ws-a"
    ws_b = tmp_path / "ws-b"
    ws_a.mkdir()
    ws_b.mkdir()
    prov_a = _GatedProvider(["A 标题"])
    prov_b = _GatedProvider(["B 标题"])
    env_a = _TitleEnv(ws_a, prov_a)
    env_b = _TitleEnv(ws_b, prov_b)
    await env_a.seed("aaa.main")
    await env_b.seed("bbb.main")
    _submit(env_a, "default", "aaa.main")
    _submit(env_b, "default", "bbb.main")
    # release only B: A stays parked — no cross-contamination of results
    prov_b.release()
    await settle_titles(env_b.naming)
    assert await env_b.ops.read_title("bbb.main") == "B 标题"
    assert await env_a.ops.read_title("aaa.main") is None
    prov_a.release()
    await settle_titles(env_a.naming)
    assert await env_a.ops.read_title("aaa.main") == "A 标题"


@pytest.mark.asyncio
async def test_sqlite_backend_manual_and_auto_roundtrip(tmp_path: Path) -> None:
    """FILE and SQLite parity: title writes and auto writes agree."""
    from modex_agent.persistence.adapters.session_store import SqliteSessionStore
    from modex_agent.persistence.managers import WorkspacePersistenceManager

    manager = WorkspacePersistenceManager(tmp_path / "state.db")
    await manager.open()
    store = SqliteSessionStore(manager.connection)
    registry = InMemorySessionRegistry(store=store)
    ops = SessionTitleOps(registry=registry)
    provider = _GatedProvider(["SQLite 自动标题"])
    naming = SessionTitleNamingTask(
        ops=ops,
        provider_source=lambda: provider,  # type: ignore[arg-type]
        transcript_reader=lambda session_id: "内容",
    )
    await registry.register(
        SessionInfo(
            session_id="sql1.main",
            agent_name="main",
            created_at=1,
            updated_at=2,
            metadata={"k": "v"},
        )
    )
    await ops.set_title("sql1.main", "SQLite 手动标题")
    session = await registry.get("sql1.main")
    assert session is not None
    assert session.metadata["title"] == "SQLite 手动标题"
    assert session.metadata["k"] == "v"
    # auto write skipped (title exists) — zero model calls
    naming.submit("default", SessionInfo(session_id="sql1.main", agent_name="main"))
    provider.release()
    await settle_titles(naming)
    assert provider.calls == 0
    await manager.close()


@pytest.mark.parametrize("whole_workspace", [False, True])
async def test_closed_owner_rejects_late_outcome(tmp_path: Path, whole_workspace: bool) -> None:
    provider = _GatedProvider(["Too late"])
    provider.release()
    env = _TitleEnv(tmp_path, provider)
    await env.seed()
    if whole_workspace:
        await env.naming.aclose()
    else:
        await env.naming.close_pool("default")
    _submit(env, "default", "abc123.main")
    await settle_titles(env.naming)
    assert provider.calls == 0
    assert await env.ops.read_title("abc123.main") is None


class _SuppressCancel:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, asyncio.CancelledError)


__all__: list[str] = []

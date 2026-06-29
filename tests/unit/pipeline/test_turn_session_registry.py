"""TurnSessionRegistry — shared per-session turn state (was AgentPipeline's 4 dicts)."""
import asyncio
import pytest
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry


@pytest.fixture
def reg():
    return TurnSessionRegistry()


async def test_register_and_unregister_turn(reg):
    task = asyncio.get_running_loop().create_task(asyncio.sleep(10))
    reg.register_task("s1", task)
    reg.set_turn_uuid("s1", "uuid-1")
    assert reg.is_active("s1") is True
    assert reg.get_turn_uuid("s1") == "uuid-1"
    reg.unregister_turn("s1")
    assert reg.is_active("s1") is False
    assert reg.get_turn_uuid("s1") is None
    task.cancel()


async def test_has_active_sessions(reg):
    assert reg.has_active() is False
    t = asyncio.get_running_loop().create_task(asyncio.sleep(10))
    reg.register_task("s1", t)
    reg.set_turn_uuid("s1", "u")
    assert reg.has_active() is True
    t.cancel()
    reg.unregister_turn("s1")
    assert reg.has_active() is False


async def test_get_or_create_queue_idempotent(reg):
    q1 = reg.get_or_create_queue("s1")
    q2 = reg.get_or_create_queue("s1")
    assert q1 is q2
    assert q1.maxsize == 50


async def test_session_lock_idempotent(reg):
    lock1 = reg.set_session_lock("s1")
    lock2 = reg.set_session_lock("s1")
    assert lock1 is lock2
    assert isinstance(lock1, asyncio.Lock)


async def test_cleanup_removes_all(reg):
    reg.set_session_lock("s1")
    reg.get_or_create_queue("s1")
    t = asyncio.get_running_loop().create_task(asyncio.sleep(10))
    reg.register_task("s1", t)
    reg.set_turn_uuid("s1", "u")
    reg.cleanup("s1")
    assert reg.get_queue("s1") is None
    assert reg.get_turn_uuid("s1") is None
    assert reg.is_active("s1") is False
    t.cancel()


async def test_get_session_task_for_busy_cancel(reg):
    assert reg.get_session_task("s1") is None
    t = asyncio.get_running_loop().create_task(asyncio.sleep(10))
    reg.register_task("s1", t)
    assert reg.get_session_task("s1") is t
    t.cancel()


async def test_get_turn_uuid_none_when_task_registered_without_uuid(reg):
    """Active turn with no uuid (runtime-None path) must return None, not ''.

    Regression guard: _try_intercept_control treats None as 'no turn to stop';
    an empty string would attach '' to a CANCEL_TURN payload.
    """
    t = asyncio.get_running_loop().create_task(asyncio.sleep(10))
    reg.register_task("s1", t)  # task only, no uuid
    assert reg.is_active("s1") is True
    assert reg.get_turn_uuid("s1") is None  # NOT ""
    t.cancel()

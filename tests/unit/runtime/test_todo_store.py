import pytest

from modex_agent.core.types import TodoStatus
from modex_agent.runtime.store import JsonFileTodoStore, TodoItem, TodoStore


def _item(content: str, status: TodoStatus = TodoStatus.PENDING) -> TodoItem:
    return TodoItem(content=content, status=status)


@pytest.mark.asyncio
async def test_save_then_get(tmp_path) -> None:
    store = JsonFileTodoStore(tmp_path)
    await store.save("s1", [_item("a"), _item("b", TodoStatus.IN_PROGRESS)])
    got = await store.get("s1")
    assert [t.content for t in got] == ["a", "b"]
    assert got[1].status is TodoStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_get_missing_returns_empty(tmp_path) -> None:
    store = JsonFileTodoStore(tmp_path)
    assert await store.get("nope") == []


@pytest.mark.asyncio
async def test_save_replaces_full_list(tmp_path) -> None:
    store = JsonFileTodoStore(tmp_path)
    await store.save("s", [_item("a"), _item("b"), _item("c")])
    await store.save("s", [_item("b")])  # full replace drops a and c
    got = await store.get("s")
    assert [t.content for t in got] == ["b"]


@pytest.mark.asyncio
async def test_delete(tmp_path) -> None:
    store = JsonFileTodoStore(tmp_path)
    await store.save("s", [_item("a")])
    await store.delete("s")
    assert await store.get("s") == []


@pytest.mark.asyncio
async def test_sessions_isolated(tmp_path) -> None:
    store = JsonFileTodoStore(tmp_path)
    await store.save("s1", [_item("a")])
    await store.save("s2", [_item("b")])
    assert [t.content for t in await store.get("s1")] == ["a"]
    assert [t.content for t in await store.get("s2")] == ["b"]


@pytest.mark.asyncio
async def test_real_session_id_preserves_dot_in_filename(tmp_path) -> None:
    """Regression: filename should look like ``<prefix>.<agent>.json``.

    Old implementation appended a hash suffix (``--0cab0a3d``) because it
    treated ``.`` as an unsafe character. Session ids are ``{prefix}.{agent}``,
    so ``.`` should be preserved.
    """
    store = JsonFileTodoStore(tmp_path)
    session_id = "3d1eb6cd187f.main"
    await store.save(session_id, [_item("a")])
    expected_file = tmp_path / f"{session_id}.json"
    assert expected_file.exists(), f"expected {expected_file}, got {list(tmp_path.iterdir())}"
    assert [t.content for t in await store.get(session_id)] == ["a"]


@pytest.mark.asyncio
async def test_save_is_atomic_on_crash(tmp_path, monkeypatch) -> None:
    """A failed write must not corrupt the existing file."""
    import modex_agent.runtime.store as store_mod

    store = JsonFileTodoStore(tmp_path)
    await store.save("s", [_item("orig")])

    def boom(src, dst):  # type: ignore[no-untyped-def]
        raise OSError("disk full")

    monkeypatch.setattr(store_mod.os, "replace", boom)
    with pytest.raises(OSError):
        await store.save("s", [_item("new")])
    got = await store.get("s")
    assert [t.content for t in got] == ["orig"]


def test_todo_item_roundtrip() -> None:
    item = TodoItem(content="x", status=TodoStatus.COMPLETED)
    d = item.to_dict()
    assert d == {"content": "x", "status": "completed"}
    assert TodoItem.from_dict(d) == item


def test_todo_store_is_abstract() -> None:
    with pytest.raises(TypeError):
        TodoStore()  # type: ignore[abstract]

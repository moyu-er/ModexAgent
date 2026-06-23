import pytest

from framework.core.types import TodoStatus
from framework.runtime.store import JsonFileTodoStore, TodoItem, TodoStore


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
async def test_save_is_atomic_on_crash(tmp_path, monkeypatch) -> None:
    """A failed write must not corrupt the existing file."""
    import framework.runtime.store as store_mod

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

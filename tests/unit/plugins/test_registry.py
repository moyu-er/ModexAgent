"""TDD tests for ComponentRegistry + TypedBundle (task 3).

Written FIRST, drives the implementation of
``src/modex_agent/plugins/registry.py``. Covers:

- ``ComponentNotFoundError`` exception contract.
- ``ComponentRegistry.register`` — conflict (ValueError) + overwrite.
- ``ComponentRegistry.resolve`` — found + not-found.
- ``ComponentRegistry.resolve_namespace_model`` — returns the model CLASS
  without calling ``factory.create``.
- ``ComponentRegistry.resolve_bundle`` + ``TypedBundle`` round-trip with a
  real FILE-backend ``DefaultScopedStorage`` (not mock).
- Scope isolation — different ``RecordScope`` values produce isolated
  keyspaces.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from modex_agent.core.scope import RecordScope
from modex_agent.memory.core.split_stores import KVStore
from modex_agent.memory.scope import MemoryLayerName
from modex_agent.memory.stores.scoped_file import DefaultScopedStorage
from modex_agent.plugins.abc import ComponentFactory, ComponentSlot, SimpleFactory
from modex_agent.plugins.registry import (
    ComponentNotFoundError,
    ComponentRegistry,
    TypedBundle,
)

# ---- Test helpers --------------------------------------------------------


class _DummyConfig(BaseModel):
    """Minimal frozen config for SimpleFactory (config is unused for DATA_NAMESPACE)."""

    model_config = {"frozen": True, "extra": "forbid"}


class _PlayerProfile(BaseModel):
    """Test model for TypedBundle round-trip — varied field types."""

    model_config = {"frozen": True, "extra": "forbid"}
    name: str
    level: int
    tags: list[str]


def _make_namespace_factory(model_cls: type[BaseModel]) -> SimpleFactory:
    """Build a SimpleFactory whose wrapped instance IS the model class.

    For DATA_NAMESPACE, the SimpleFactory's ``instance`` is the model CLASS
    itself (``type[BaseModel]``), not a model instance. This is the mechanism
    ``resolve_namespace_model`` reads.
    """
    return SimpleFactory(instance=model_cls, config_model=_DummyConfig)


# ---- ComponentNotFoundError ---------------------------------------------


class TestComponentNotFoundError:
    def test_message_includes_name_and_slot(self) -> None:
        exc = ComponentNotFoundError("my_tool", ComponentSlot.TOOL)
        msg = str(exc)
        assert "my_tool" in msg
        assert "tool" in msg  # ComponentSlot.TOOL.value == "tool"

    def test_stores_name_and_slot_as_attributes(self) -> None:
        exc = ComponentNotFoundError("xyz", ComponentSlot.HOOK)
        assert exc.name == "xyz"
        assert exc.slot == ComponentSlot.HOOK

    def test_is_exception_subclass(self) -> None:
        assert issubclass(ComponentNotFoundError, Exception)


# ---- ComponentRegistry.register ------------------------------------------


class TestRegister:
    def test_register_and_resolve_roundtrip(self) -> None:
        registry = ComponentRegistry()
        factory = SimpleFactory(instance=object(), config_model=_DummyConfig)
        registry.register(ComponentSlot.TOOL, "hammer", factory)
        assert registry.resolve(ComponentSlot.TOOL, "hammer") is factory

    def test_same_slot_name_non_overwrite_raises_value_error(self) -> None:
        registry = ComponentRegistry()
        f1 = SimpleFactory(instance=object(), config_model=_DummyConfig)
        f2 = SimpleFactory(instance=object(), config_model=_DummyConfig)
        registry.register(ComponentSlot.TOOL, "dup", f1)
        with pytest.raises(ValueError, match="dup"):
            registry.register(ComponentSlot.TOOL, "dup", f2)

    def test_overwrite_true_replaces(self) -> None:
        registry = ComponentRegistry()
        f1 = SimpleFactory(instance=object(), config_model=_DummyConfig)
        f2 = SimpleFactory(instance=object(), config_model=_DummyConfig)
        registry.register(ComponentSlot.TOOL, "item", f1)
        registry.register(ComponentSlot.TOOL, "item", f2, overwrite=True)
        assert registry.resolve(ComponentSlot.TOOL, "item") is f2

    def test_same_name_different_slots_no_conflict(self) -> None:
        registry = ComponentRegistry()
        f1 = SimpleFactory(instance=object(), config_model=_DummyConfig)
        f2 = SimpleFactory(instance=object(), config_model=_DummyConfig)
        registry.register(ComponentSlot.TOOL, "shared", f1)
        registry.register(ComponentSlot.HOOK, "shared", f2)
        assert registry.resolve(ComponentSlot.TOOL, "shared") is f1
        assert registry.resolve(ComponentSlot.HOOK, "shared") is f2


# ---- ComponentRegistry.resolve -------------------------------------------


class TestResolve:
    def test_resolve_unknown_name_raises_not_found(self) -> None:
        registry = ComponentRegistry()
        with pytest.raises(ComponentNotFoundError) as exc_info:
            registry.resolve(ComponentSlot.TOOL, "ghost")
        assert exc_info.value.name == "ghost"
        assert exc_info.value.slot == ComponentSlot.TOOL

    def test_resolve_unknown_slot_raises_not_found(self) -> None:
        registry = ComponentRegistry()
        # Slot was never populated — should also raise not-found, not KeyError.
        with pytest.raises(ComponentNotFoundError):
            registry.resolve(ComponentSlot.INTERCEPTOR, "anything")

    def test_resolve_returns_registered_factory(self) -> None:
        registry = ComponentRegistry()
        factory = SimpleFactory(instance=42, config_model=_DummyConfig)
        registry.register(ComponentSlot.LLM_PROVIDER, "openai", factory)
        resolved = registry.resolve(ComponentSlot.LLM_PROVIDER, "openai")
        assert resolved is factory

    def test_names_returns_sorted_names_for_slot(self) -> None:
        registry = ComponentRegistry()
        factory = SimpleFactory(instance=object(), config_model=_DummyConfig)
        registry.register(ComponentSlot.INPUT_STAGE, "zeta", factory)
        registry.register(ComponentSlot.INPUT_STAGE, "alpha", factory)
        registry.register(ComponentSlot.TOOL, "other_slot", factory)

        assert registry.names(ComponentSlot.INPUT_STAGE) == ("alpha", "zeta")

    def test_names_returns_empty_tuple_for_unregistered_slot(self) -> None:
        registry = ComponentRegistry()

        assert registry.names(ComponentSlot.INPUT_STAGE) == ()


# ---- resolve_namespace_model ---------------------------------------------


class TestResolveNamespaceModel:
    def test_returns_model_class_not_instance(self) -> None:
        registry = ComponentRegistry()
        registry.register(
            ComponentSlot.DATA_NAMESPACE,
            "player",
            _make_namespace_factory(_PlayerProfile),
        )
        model_cls = registry.resolve_namespace_model("player")
        assert model_cls is _PlayerProfile
        # Must be a class, not an instance.
        assert isinstance(model_cls, type)
        assert issubclass(model_cls, BaseModel)

    def test_unknown_namespace_raises_not_found(self) -> None:
        registry = ComponentRegistry()
        with pytest.raises(ComponentNotFoundError):
            registry.resolve_namespace_model("nope")

    def test_non_simple_factory_raises_type_error(self) -> None:
        registry = ComponentRegistry()

        class _BadFactory(ComponentFactory):
            config_model = _DummyConfig

            async def create(self, config: BaseModel, ctx: object) -> object:  # type: ignore[override]
                return None

        registry.register(
            ComponentSlot.DATA_NAMESPACE,
            "bad",
            _BadFactory(),  # type: ignore[arg-type]
        )
        with pytest.raises(TypeError, match="SimpleFactory"):
            registry.resolve_namespace_model("bad")


# ---- TypedBundle FILE-backend round-trip ---------------------------------


class TestTypedBundleFileRoundTrip:
    """Round-trip with a real FILE-backend DefaultScopedStorage (not mock)."""

    async def test_set_get_roundtrip(self, tmp_path: Path) -> None:
        registry = ComponentRegistry()
        registry.register(
            ComponentSlot.DATA_NAMESPACE,
            "player",
            _make_namespace_factory(_PlayerProfile),
        )
        kv_store = DefaultScopedStorage(
            tmp_path / "kv",
            layer=MemoryLayerName.SESSION,
        )
        bundle = registry.resolve_bundle("player", kv_store)
        scope = RecordScope(session_id="s1", agent_id="main")

        original = _PlayerProfile(name="Alice", level=30, tags=["warrior", "fire"])
        await bundle.set("alice", original, scope)

        restored = await bundle.get("alice", scope)
        assert restored is not None
        assert restored == original
        assert restored.name == "Alice"
        assert restored.level == 30
        assert restored.tags == ["warrior", "fire"]

    async def test_get_missing_returns_none(self, tmp_path: Path) -> None:
        registry = ComponentRegistry()
        registry.register(
            ComponentSlot.DATA_NAMESPACE,
            "player",
            _make_namespace_factory(_PlayerProfile),
        )
        kv_store = DefaultScopedStorage(
            tmp_path / "kv",
            layer=MemoryLayerName.SESSION,
        )
        bundle = registry.resolve_bundle("player", kv_store)
        scope = RecordScope(session_id="s1")
        assert await bundle.get("nope", scope) is None

    async def test_delete_then_get_returns_none(self, tmp_path: Path) -> None:
        registry = ComponentRegistry()
        registry.register(
            ComponentSlot.DATA_NAMESPACE,
            "player",
            _make_namespace_factory(_PlayerProfile),
        )
        kv_store = DefaultScopedStorage(
            tmp_path / "kv",
            layer=MemoryLayerName.SESSION,
        )
        bundle = registry.resolve_bundle("player", kv_store)
        scope = RecordScope(session_id="s1")

        model = _PlayerProfile(name="Bob", level=5, tags=[])
        await bundle.set("bob", model, scope)
        assert await bundle.get("bob", scope) is not None

        await bundle.delete("bob", scope)
        assert await bundle.get("bob", scope) is None

    async def test_list_keys_returns_user_keys_without_prefix(
        self, tmp_path: Path
    ) -> None:
        registry = ComponentRegistry()
        registry.register(
            ComponentSlot.DATA_NAMESPACE,
            "player",
            _make_namespace_factory(_PlayerProfile),
        )
        kv_store = DefaultScopedStorage(
            tmp_path / "kv",
            layer=MemoryLayerName.SESSION,
        )
        bundle = registry.resolve_bundle("player", kv_store)
        scope = RecordScope(session_id="s1")

        await bundle.set("k1", _PlayerProfile(name="A", level=1, tags=[]), scope)
        await bundle.set("k2", _PlayerProfile(name="B", level=2, tags=[]), scope)

        keys = await bundle.list_keys(scope)
        assert sorted(keys) == ["k1", "k2"]

    async def test_overwrite_via_set(self, tmp_path: Path) -> None:
        registry = ComponentRegistry()
        registry.register(
            ComponentSlot.DATA_NAMESPACE,
            "player",
            _make_namespace_factory(_PlayerProfile),
        )
        kv_store = DefaultScopedStorage(
            tmp_path / "kv",
            layer=MemoryLayerName.SESSION,
        )
        bundle = registry.resolve_bundle("player", kv_store)
        scope = RecordScope(session_id="s1")

        v1 = _PlayerProfile(name="Old", level=1, tags=[])
        v2 = _PlayerProfile(name="New", level=99, tags=["boss"])
        await bundle.set("slot", v1, scope)
        await bundle.set("slot", v2, scope)

        restored = await bundle.get("slot", scope)
        assert restored == v2


# ---- TypedBundle scope isolation -----------------------------------------


class TestTypedBundleScopeIsolation:
    """Different RecordScope values produce isolated keyspaces."""

    async def test_different_scopes_are_isolated(self, tmp_path: Path) -> None:
        registry = ComponentRegistry()
        registry.register(
            ComponentSlot.DATA_NAMESPACE,
            "player",
            _make_namespace_factory(_PlayerProfile),
        )
        kv_store = DefaultScopedStorage(
            tmp_path / "kv",
            layer=MemoryLayerName.SESSION,
        )
        bundle = registry.resolve_bundle("player", kv_store)

        scope_a = RecordScope(session_id="session_a")
        scope_b = RecordScope(session_id="session_b")

        await bundle.set(
            "key", _PlayerProfile(name="A", level=1, tags=[]), scope_a
        )
        # scope_b must not see scope_a's data.
        assert await bundle.get("key", scope_b) is None

        await bundle.set(
            "key", _PlayerProfile(name="B", level=2, tags=[]), scope_b
        )
        restored_a = await bundle.get("key", scope_a)
        restored_b = await bundle.get("key", scope_b)
        assert restored_a is not None and restored_b is not None
        assert restored_a.name == "A"
        assert restored_b.name == "B"

    async def test_list_keys_scoped(self, tmp_path: Path) -> None:
        registry = ComponentRegistry()
        registry.register(
            ComponentSlot.DATA_NAMESPACE,
            "player",
            _make_namespace_factory(_PlayerProfile),
        )
        kv_store = DefaultScopedStorage(
            tmp_path / "kv",
            layer=MemoryLayerName.SESSION,
        )
        bundle = registry.resolve_bundle("player", kv_store)

        scope_a = RecordScope(session_id="a")
        scope_b = RecordScope(session_id="b")

        await bundle.set("a1", _PlayerProfile(name="A1", level=1, tags=[]), scope_a)
        await bundle.set("a2", _PlayerProfile(name="A2", level=2, tags=[]), scope_a)
        await bundle.set("b1", _PlayerProfile(name="B1", level=3, tags=[]), scope_b)

        keys_a = await bundle.list_keys(scope_a)
        keys_b = await bundle.list_keys(scope_b)
        assert sorted(keys_a) == ["a1", "a2"]
        assert keys_b == ["b1"]


# ---- TypedBundle type contract -------------------------------------------


class TestTypedBundleTypeContract:
    def test_is_generic(self) -> None:
        # TypedBundle must accept a type parameter.
        bundle = TypedBundle(
            "ns", _PlayerProfile, _NullKVStore()  # type: ignore[arg-type]
        )
        assert bundle is not None

    async def test_set_serializes_via_model_dump_json(self) -> None:
        """Verify set() uses model_dump_json (Pydantic serialization)."""
        kv = _RecordingKVStore()
        bundle = TypedBundle("ns", _PlayerProfile, kv)  # type: ignore[arg-type]
        scope = RecordScope(session_id="s1")
        model = _PlayerProfile(name="Z", level=7, tags=["x"])
        await bundle.set("k", model, scope)
        # The stored value must be a JSON string produced by model_dump_json.
        assert kv.last_set_value is not None
        assert isinstance(kv.last_set_value, str)
        assert '"name":"Z"' in kv.last_set_value
        assert '"level":7' in kv.last_set_value

    async def test_get_deserializes_via_model_validate_json(self) -> None:
        """Verify get() uses model_validate_json (Pydantic deserialization)."""
        kv = _RecordingKVStore()
        scope = RecordScope(session_id="s1")
        # Pre-populate with a JSON string that model_validate_json can parse.
        canonical_prefix = scope.canonical() + "\x00"
        kv.data[canonical_prefix + "k"] = _PlayerProfile(
            name="Pre", level=3, tags=["m"]
        ).model_dump_json()

        bundle = TypedBundle("ns", _PlayerProfile, kv)  # type: ignore[arg-type]
        restored = await bundle.get("k", scope)
        assert restored is not None
        assert restored.name == "Pre"
        assert restored.level == 3


# ---- Minimal KVStore stubs for type-contract tests -----------------------


class _NullKVStore(KVStore):
    """No-op KVStore for construction tests."""

    async def get(self, key: str) -> object | None:
        return None

    async def set(self, key: str, value: object) -> None:
        pass

    async def delete(self, key: str) -> bool:
        return False

    async def list_keys(self, prefix: str = "") -> list[str]:
        return []


class _RecordingKVStore(KVStore):
    """KVStore that records the last set value for assertion."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.last_set_value: str | None = None

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value
        self.last_set_value = value

    async def delete(self, key: str) -> bool:
        return self.data.pop(key, None) is not None

    async def list_keys(self, prefix: str = "") -> list[str]:
        return [k for k in self.data if k.startswith(prefix)]

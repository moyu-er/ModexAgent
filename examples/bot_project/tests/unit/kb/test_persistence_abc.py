"""Tests for bot.kb.persistence — KbPersistence(ABC) contract.

Locks the core decoupling decision: persistence handles CRUD only, NO search.
Search belongs to the retriever (bot/kb/retriever.py). Hermes' MemoryStore
leaked search_facts into the persistence layer and caused a circular import;
these tests prevent that regression at the ABC level.
"""

from __future__ import annotations

import pytest

from bot.kb.models import KbEntry, KbFilter, KbUpsertRequest
from bot.kb.persistence import KbPersistence

_EXPECTED_ABSTRACT_METHODS = frozenset({"upsert", "get", "delete", "list_keys"})


class _ConcretePersistence(KbPersistence):
    """Minimal concrete subclass implementing all four abstract methods.

    Used only to prove the ABC is instantiable once every abstract method
    is overridden. Method bodies are stubs — behavior is not under test here.
    """

    async def upsert(self, request: KbUpsertRequest) -> KbEntry:  # pragma: no cover
        ...

    async def get(self, key: str, filter: KbFilter) -> KbEntry | None:  # pragma: no cover
        ...

    async def delete(self, key: str, filter: KbFilter) -> bool:  # pragma: no cover
        ...

    async def list_keys(
        self,
        filter: KbFilter,
        prefix: str | None = None,
    ) -> list[str]:  # pragma: no cover
        ...


def test_cannot_instantiate_when_abstract_methods_unimplemented() -> None:
    """KbPersistence is abstract — direct instantiation must fail.

    Given: the KbPersistence ABC with unimplemented abstract methods.
    When: an attempt is made to instantiate it directly.
    Then: TypeError is raised (ABC instantiation guard).
    """
    with pytest.raises(TypeError):
        KbPersistence()  # type: ignore[abstract]


def test_all_four_methods_are_abstract_when_inspected() -> None:
    """Exactly upsert/get/delete/list_keys are abstract, no more, no less.

    Given: the KbPersistence class.
    When: its __abstractmethods__ set is inspected.
    Then: it equals {upsert, get, delete, list_keys} and each method carries
        __isabstractmethod__ = True.
    """
    assert KbPersistence.__abstractmethods__ == _EXPECTED_ABSTRACT_METHODS

    for name in _EXPECTED_ABSTRACT_METHODS:
        method = getattr(KbPersistence, name)
        assert getattr(method, "__isabstractmethod__", False) is True, (
            f"{name} must be abstract"
        )


def test_no_search_method_when_inspecting_class() -> None:
    """KbPersistence must NOT expose a search method.

    Given: the KbPersistence class.
    When: checked for a 'search' attribute.
    Then: no such attribute exists — search is the retriever's responsibility,
        not persistence's. This is the decoupling decision that prevents the
        hermes circular-import bug.
    """
    assert not hasattr(KbPersistence, "search")


def test_concrete_subclass_instantiable_when_all_methods_implemented() -> None:
    """A subclass overriding all four abstract methods instantiates.

    Given: _ConcretePersistence implementing upsert/get/delete/list_keys.
    When: instantiated.
    Then: no TypeError is raised and the instance is a KbPersistence.
    """
    instance = _ConcretePersistence()
    assert isinstance(instance, KbPersistence)

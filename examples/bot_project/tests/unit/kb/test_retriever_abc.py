"""Tests for bot.kb.retriever — KbRetriever ABC contract.

Verifies the retrieval abstraction: exactly one abstract method (search),
no CRUD methods leaked from persistence, the ABC cannot be instantiated
directly, and a concrete subclass that implements search() is instantiable.

This is the other half of the persistence/retriever decoupling
(DESIGN.md §0, §5). KbPersistence owns CRUD; KbRetriever owns search.
"""

from __future__ import annotations

import pytest
from bot.kb.models import KbFilter, KbSearchResult
from bot.kb.retriever import KbRetriever

# -- ABC instantiation -------------------------------------------------


def test_cannot_instantiate_when_abstract() -> None:
    """KbRetriever is abstract; direct instantiation raises TypeError.

    Given: KbRetriever is an ABC with an unimplemented abstract method.
    When: attempting to instantiate KbRetriever directly.
    Then: TypeError is raised.
    """
    with pytest.raises(TypeError):
        KbRetriever()  # type: ignore[abstract]


# -- search is the sole abstract method --------------------------------


def test_search_is_abstract_when_inspected() -> None:
    """search is decorated with @abstractmethod.

    Given: KbRetriever.search is declared with @abstractmethod.
    When: inspecting the __isabstractmethod__ attribute.
    Then: it is True.
    """
    assert getattr(KbRetriever.search, "__isabstractmethod__", False) is True


def test_search_is_the_only_abstract_method_when_collected() -> None:
    """No method other than search is abstract.

    Given: KbRetriever is declared per DESIGN.md §5 with one abstract method.
    When: collecting __abstractmethods__.
    Then: the set is exactly {'search'}.
    """
    assert KbRetriever.__abstractmethods__ == frozenset({"search"})


# -- No CRUD methods (persistence/retriever decoupling) -----------------


def test_no_crud_methods_when_inspected() -> None:
    """Retriever has no upsert/get/delete/list_keys — those belong to persistence.

    Given: KbRetriever is the retrieval ABC (DESIGN.md §5).
    When: checking for CRUD method attributes.
    Then: none of upsert/get/delete/list_keys exist on the class.
    """
    for method in ("upsert", "get", "delete", "list_keys"):
        assert not hasattr(KbRetriever, method), (
            f"unexpected CRUD method on KbRetriever: {method}"
        )


# -- Concrete subclass contract -----------------------------------------


class _StubRetriever(KbRetriever):
    """Minimal concrete subclass for contract testing."""

    async def search(
        self,
        query: str,
        filter: KbFilter,
        limit: int = 20,
    ) -> list[KbSearchResult]:
        return []


def test_concrete_subclass_instantiates_when_search_implemented() -> None:
    """A subclass that implements search() can be instantiated.

    Given: a concrete subclass _StubRetriever implementing search().
    When: instantiating it.
    Then: it is an instance of KbRetriever with no TypeError.
    """
    retriever = _StubRetriever()
    assert isinstance(retriever, KbRetriever)


def test_incomplete_subclass_cannot_instantiate_when_search_missing() -> None:
    """A subclass that does not implement search() remains abstract.

    Given: a subclass of KbRetriever that does not override search.
    When: attempting to instantiate it.
    Then: TypeError is raised.
    """

    class _Incomplete(KbRetriever):
        pass

    with pytest.raises(TypeError):
        _Incomplete()  # type: ignore[abstract]

"""MemoryInjectionPolicy exposes explicit capability queries, not private attrs."""

from framework.memory.injection.full_injection import FullInjectionPolicy
from framework.memory.injection.restricted_injection import RestrictedInjectionPolicy


class _StubPruned:
    def get_injection_xml(self, *, session_id: str) -> str:
        return "<pruned/>"


def test_full_policy_capability_queries() -> None:
    assert FullInjectionPolicy(archive_inject_count=3).injects_archive() is True
    assert FullInjectionPolicy(archive_inject_count=0).injects_archive() is False
    assert FullInjectionPolicy().injects_pruned() is False
    assert FullInjectionPolicy(pruned_manager=_StubPruned()).injects_pruned() is True  # type: ignore[arg-type]


def test_restricted_policy_capability_queries() -> None:
    assert RestrictedInjectionPolicy().injects_pruned() is False
    assert RestrictedInjectionPolicy(pruned_manager=_StubPruned()).injects_pruned() is True  # type: ignore[arg-type]
    assert RestrictedInjectionPolicy().injects_archive() is False

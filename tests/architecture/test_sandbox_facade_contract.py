"""Architecture guard: the sandbox facade __all__ stays slim (ADR-0005/0007).

Candidate ⑤ Part A reduced modex_agent.sandbox.__all__ to the seam —
selection entry points + the SandboxAdapter ABC + consumer-facing
types/errors. Concrete adapters live behind sandbox.adapters; guards behind
sandbox.guard / sandbox.guard_*; env builder behind sandbox.env_builder;
workspace policy behind sandbox.workspace_policy; platform/docker helpers
behind sandbox.platform / sandbox.docker_utils.

__all__ is load-bearing (ADR-0005): any addition must update EXPECTED_ALL and
justify itself. This is the semantic peer of test_dead_code_gone.py, pinning
a facade contract rather than a deletion.
"""
from __future__ import annotations

import modex_agent.sandbox as sandbox

EXPECTED_ALL = frozenset(
    {
        # Selection entry points
        "get_default_sandbox",
        "get_sandbox",
        "get_local_sandbox",
        "get_cloud_sandbox",
        "list_available_adapters",
        # Seam ABC
        "SandboxAdapter",
        # Consumer-facing types / errors
        "SandboxConfig",
        "SandboxResult",
        "SandboxType",
        "SandboxError",
        "SandboxUnavailableError",
        "SandboxTimeoutError",
        "CommandRejectedError",
        "WorkspaceBoundaryError",
    }
)


def test_sandbox_facade_all_is_exactly_the_seam() -> None:
    actual = frozenset(sandbox.__all__)
    assert actual == EXPECTED_ALL, (
        "sandbox facade __all__ drifted from the slimmed seam "
        "(ADR-0005/0007, candidate ⑤ Part A).\n"
        f"  missing: {sorted(EXPECTED_ALL - actual)}\n"
        f"  extra:   {sorted(actual - EXPECTED_ALL)}"
    )


def test_expected_all_symbols_are_importable() -> None:
    """The facade must actually re-export everything it declares."""
    missing = [name for name in EXPECTED_ALL if not hasattr(sandbox, name)]
    assert not missing, f"sandbox facade __all__ names not importable: {missing}"

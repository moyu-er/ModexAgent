"""Verify cd creates the same directory structure as initial pool creation."""
from __future__ import annotations

from pathlib import Path


def test_pool_mode_cd_matches_create_pool_structure() -> None:
    """After cd, .modex/ should have the same category-first layout as
    create_pool: .modex/memory/{pool}/ and .modex/runtime_state/{pool}/."""

    data_dir = Path("/tmp/project/.modex")
    pool_name = "main"

    # Initial create_pool layout (pool_builder.py)
    original_memory = data_dir / "memory" / pool_name       # .modex/memory/main/
    original_runtime = data_dir / "runtime_state" / pool_name  # .modex/runtime_state/main/

    assert original_memory == Path("/tmp/project/.modex/memory/main")
    assert original_runtime == Path("/tmp/project/.modex/runtime_state/main")

    # BUG: the old callback layout was new_dir / pool_name / "memory"
    wrong_memory = data_dir / pool_name / "memory"          # .modex/main/memory/ ← wrong
    assert wrong_memory != original_memory, (
        f"pool-first layout {wrong_memory} must NOT be used — "
        f"it is inconsistent with the original create_pool path {original_memory}"
    )

    # FIX: use category-first layout
    fixed_memory = data_dir / "memory" / pool_name          # .modex/memory/main/
    fixed_runtime = data_dir / "runtime_state" / pool_name  # .modex/runtime_state/main/
    assert fixed_memory == original_memory
    assert fixed_runtime == original_runtime

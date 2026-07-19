"""Architecture guard: persistence schema invariants from ADR-0028/0029/0031.

Prevents regressions that would re-introduce the dropped ``workspace_meta`` /
``inbox_dead_letter`` tables, the removed ``RecordScope.pool`` field, or
relocate ``now_ms`` away from ``modex_agent.utils.time``.
"""

from __future__ import annotations

import ast
import pathlib
import re

from modex_agent.core.scope import RecordScope
from modex_agent.utils import time as time_utils

SRC_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "modex_agent"

_DROPPED_TABLE_NAMES: tuple[str, ...] = (
    "workspace_meta",
    "inbox_dead_letter",
)

_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(s) for s in _DROPPED_TABLE_NAMES) + r")\b"
)


def test_no_dropped_table_references_in_python_src() -> None:
    offenders: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        hits = set(_PATTERN.findall(text))
        if hits:
            offenders.append(f"{path.relative_to(SRC_ROOT.parents[1])}: {sorted(hits)}")
    assert not offenders, (
        "Dropped-table names from ADR-0031 re-introduced in src/:\n  "
        + "\n  ".join(offenders)
    )


def test_record_scope_has_no_pool_field() -> None:
    assert "pool" not in RecordScope.model_fields, (
        "RecordScope must not carry a 'pool' field (ADR-0028 removed it; "
        "pool-scoped scope_keys use a RecordScope subclass like "
        "BotRecordScope in the examples layer)."
    )


def test_record_scope_forbids_extra_fields() -> None:
    assert RecordScope.model_config.get("extra") == "forbid", (
        "RecordScope must keep extra='forbid' so accidental pool=... "
        "constructions fail loudly instead of silently dropping the value."
    )


def test_utils_time_exports_now_ms() -> None:
    assert hasattr(time_utils, "now_ms"), (
        "modex_agent.utils.time must export now_ms (ADR-0029 §2 single "
        "source of truth for epoch-ms timestamps)."
    )
    assert callable(time_utils.now_ms)
    assert "now_ms" in time_utils.__all__


def test_no_time_time_calls_in_persistence() -> None:
    """ADR-0029 §2: persistence modules must use ``now_ms()`` from
    ``modex_agent.utils.time`` (or the schema DEFAULT expression), not
    ``time.time()`` directly."""
    persistence_dir = SRC_ROOT / "persistence"
    if not persistence_dir.is_dir():
        return
    offenders: list[str] = []
    for path in persistence_dir.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "time"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "time"
            ):
                offenders.append(
                    f"{path.relative_to(SRC_ROOT.parents[1])}:{node.lineno}: time.time()"
                )
    assert not offenders, (
        "Direct time.time() calls in persistence/ (use now_ms() "
        "from modex_agent.utils.time instead):\n  " + "\n  ".join(offenders)
    )

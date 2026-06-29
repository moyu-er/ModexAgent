"""Architecture guard: the genuinely-dead control-plane pieces removed in
candidate ④ (ADR-0007) must not be reintroduced.

If a future change re-adds any of these symbols, this test fails loudly so the
PR is forced to justify why — see ADR-0007 "genuinely dead" list. This is a
governance decision, not a missed import.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "modex_agent"

# Symbols removed as genuinely dead in candidate ④ and ④b (ADR-0007). Live
# neighbors kept on purpose: AgentControlError exceptions; ControlCommand /
# ControlCommandType / ControlScope (live channel payload); InMemoryControlChannel
# + drain_control_channel + the ControlDrainInterceptor / LlmCancelInterceptor
# (the LIVE /stop + WebUI-pause mechanism — confirmed load-bearing in ④b, not dead).
DEAD_SYMBOLS = (
    # candidate ④ — dead event bus + dead durable command store
    "ControlEventBus",
    "CallbackControlEventBus",
    "ProgressReportHook",
    "RuntimeCommandStore",
    "InMemoryRuntimeCommandStore",
    "JsonFileRuntimeCommandStore",
    "NoOpRuntimeCommandStore",
    "ControlCommandState",
    "ControlCommandKind",
    # candidate ④b — dead control event types + dead config aggregate + dead hook point
    "ControlEvent",
    "ControlEventType",
    "RuntimeControl",
    "AgentRuntimeConfig",
    "OnControlCommandHook",
)

# Match whole identifiers, not substrings.
_PATTERN = re.compile(r"\b(" + "|".join(re.escape(s) for s in DEAD_SYMBOLS) + r")\b")


def test_dead_symbols_absent_from_src() -> None:
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        hits = set(_PATTERN.findall(text))
        if hits:
            offenders.append(f"{path.relative_to(ROOT.parents[1])}: {sorted(hits)}")
    assert not offenders, (
        "Dead symbols from ADR-0007 re-introduced in src/:\n  " + "\n  ".join(offenders)
    )


def test_guard_symbol_list_is_nonempty() -> None:
    """Sanity: the guard must actually watch something."""
    assert DEAD_SYMBOLS

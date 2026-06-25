"""C1 regression guard: the memory.core.{message,scope} shims are gone;
consumers import shared value types directly from modex_agent.core."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src"
FORBIDDEN = ("modex_agent.memory.core.message", "modex_agent.memory.core.scope")

def test_no_memory_core_shim_references_in_src() -> None:
    offenders: list[str] = []
    for p in ROOT.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        for needle in FORBIDDEN:
            if needle in text:
                offenders.append(f"{p.relative_to(ROOT)}: {needle}")
    assert not offenders, f"stale memory.core shim references remain:\n{offenders}"

# 06 — `SummarizerAgent` removal + dead code cleanup

**What to build:** Remove the deprecated, unused `SummarizerAgent` and its supporting classes from the codebase. These classes have zero callers inside `src/` (only `tests/unit/agents/test_summarizer_memory_prompt.py` reads a constant). Additionally, clean up any dead code uncovered by the graph engine migration (tickets 01-05). This ticket is independent of the graph engine work and can be done in parallel at any time.

**Blocked by:** None — can start immediately. Fully independent of tickets 01-05.

**Status:** completed (commit 603c2953)

## Acceptance criteria

- [ ] `SummarizerAgent` class deleted from `modex_agent/agents/summarizer/agent.py`
- [ ] `SummarizerEvent` enum deleted
- [ ] `SummarizerStrategy` ABC deleted from `modex_agent/agents/summarizer/strategy.py`
- [ ] `DefaultSummarizerStrategy` class deleted
- [ ] `SummarizerAgent` / `SummarizerStrategy` / `DefaultSummarizerStrategy` / `SummarizerEvent` removed from `modex_agent/agents/summarizer/__init__.py` `__all__`
- [ ] `PROMPT_MEMORY_COMPRESSION` constant (and any other `PROMPT_*` constants only used by `SummarizerAgent`) either relocated to a still-existing module if externally referenced, or deleted if only referenced by the removed code
- [ ] `tests/unit/agents/test_summarizer_memory_prompt.py` either deleted (if it only tested the removed `SummarizerAgent`) or updated to read the relocated constant
- [ ] `modex_agent/agents/summarizer/` directory retains `ArchiveSummarizer` / `CoreMemoryConsolidator` (renamed from `KnowledgeConsolidator` per ADR-0035) / `ScopedFileAgent` / `abc.py` / `archive_agent.py` / `consolidator.py` / `emitter.py` — these are still in use by `DreamEngine` (see `modex_agent/memory/consolidation/dream_engine.py`) and must NOT be removed
- [ ] Any imports of `SummarizerAgent` / `SummarizerStrategy` / `DefaultSummarizerStrategy` / `SummarizerEvent` elsewhere in `src/` are removed (verify with grep — expected to find none, since they have zero callers per pre-ticket analysis)
- [ ] `modex_agent` package's `__init__.py` updated if it re-exported any of the removed symbols
- [ ] `examples/bot_project/` scanned for any reference to the removed symbols — expected none, but verify
- [ ] Dead code uncovered by tickets 01-05 migration (e.g. unused helper functions, obsolete type aliases, stale comments referencing old graph engine) cleaned up
- [ ] Full test suite passes (`pytest tests/unit/ -v` + `pytest tests/integration/ -v -m integration`)
- [ ] `ruff check src/modex_agent tests/` passes
- [ ] `mypy src/modex_agent` passes
- [ ] Architecture guard tests pass (no `modex_agent.core.graph` imports — already enforced by ticket 05, but re-verified here)

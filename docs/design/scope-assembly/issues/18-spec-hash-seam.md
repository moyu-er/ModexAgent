# 18 — Seam: spec-hash + generation counter

**What to build:** The reserved hot-reload seam, ~50 lines, no swap mechanism: a pure-function hash over each agent's compiled `AssemblySpec` plus a per-pool generation counter. Nothing consumes them yet except a hash-stability test (same input tree → same hash; any spec change → different hash). This is the N2 seam: future hot reload builds on it; until then it costs nothing.

**Blocked by:** 06 (hashes are over compiler output).

**Status:** closed (resolved 2026-08-21)

- [x] `AssemblySpec` hash is stable across processes (no dict-ordering/env noise) and distinguishes every spec-affecting change (sha256 over the lane-07 pinned `model_dump_json` face minus `spec.workspace_ctx`; two subprocesses under different `PYTHONHASHSEED` produce equal digests matching the in-process compile; 17-field mutation matrix all distinct and pairwise-disjoint — `scope/seam.py:spec_hash`)
- [x] Per-pool generation counter increments on each compile; queryable for tests/logging only (`ScopeGenerationTracker.compile` bumps once per hosted pool — the compiler stays pure/stateless, Oracle R2#7; `generation(pool)` returns 0 for never-compiled pools)
- [x] Hash-stability unit tests (byte-identical compile → identical hash; mutation matrix → different hashes) (`tests/unit/scope/test_seam.py`, 27 tests incl. the subprocess-isolation and workspace_ctx-exclusion cases)
- [x] Zero runtime consumers wired (no swap mechanism, per N2 — restart-effective stands) (grep clean: no reference to `spec_hash`/`ScopeGenerationTracker` outside `src/modex_agent/scope/` + tests; boot wiring is ticket 07's call)
- [x] Documentation note in SPEC §10 cross-referenced from the seam's code comment (`scope/seam.py` module docstring cites the §10 hot-reload row + N2 flip condition)

# 03 — ScopeTreeValidator: two-phase validation (V1-V11)

**What to build:** A pure-function validator rejects bad scope declarations at startup with precise errors. Phase 1 (declaration shape, pre-derivation): acyclic (V1), connected (V2), exactly one root per pool tree (V3), kind hierarchy legal (V4), peer endpoints exist + same-workspace + bidirectional + root-to-root + pool-rooted declarations cannot declare peers in v1 (V5), profile references single-level (V7), pool-internal agent names unique and workspace-internal pool names unique (V11). Phase 2 (effective values, post-derivation): agents with declared children must have `task` in their compiler-derived effective toolset (V6); non-root nodes declaring approval fail startup (V9); graph-spec node references (pool, agent) must exist in the declaration tree (V10). Validation runs at startup after spec loading — a bad declaration aborts boot instead of producing orphan messages or silently orphaned subtrees at runtime.

**Blocked by:** 02 (needs the ScopeSpec tree types to validate).

**Status:** closed (resolved 2026-08-21)

- [x] Phase-1 rules V1-V5, V7, V11 each fail startup with precise, actionable error messages
- [x] Phase-2 rules V6, V9, V10 consume compiler-derived effective toolsets/graph specs (input contract: tree + profile store + derived configs; V10 implemented in phase 1 per SPEC §7 phase-1 table — this ticket's prose placing it in phase 2 is a typo, SPEC errata tracked in todo 20)
- [x] V6 catches the profile-wholesale-list case: a `tools:` replacement dropping `task` from an agent with declared children fails startup
- [x] V10 cross-checks loaded graph specs' BotAgentNode references against the declaration tree (typo'd agent names fail at boot)
- [x] Positive path: all shipped configs validate clean
- [x] Unit-test matrix covers every rule's positive and negative cases

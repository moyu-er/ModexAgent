# 07 — First pool switched to scope declaration (the pivot)

**What to build:** One shipped pool (e.g. review) boots entirely from its scope YAML declaration: load → validate → compile → existing assembly pipeline. The pool's tools, memory, communication registration, and subagent materialization all flow from the compiled per-agent AssemblySpecs. Communication tools register by tree derivation: the root gets `task` (its target store built at assembly), each non-root node gets `send_to_agent` with the parent target — product-equivalent to today's registration, but derived, not main-hardcoded. This is the pivot ticket: the declaration path becomes the real path for real traffic on one pool while the rest still boot the old way (dual-boot tolerated only until ticket 11 contracts).

**Blocked by:** 05 (roster+factory path proven for tools), 06 (compiler produces the specs).

**Pivot pool = default** (plan todo 8, Oracle R2#1/Momus R2#1): the only shipped pool exercising the full derivation table — main + office-expert subagent + peers [opencode, review] + approval + aci supplement + mcp [playwright]. The SPEC §3.2 authority applies: any agent with declared children gets `task` (root or not); leaves get none.

**Status:** closed (resolved 2026-08-21)

- [x] The chosen pool boots from scope YAML with zero business-glue tool/resource construction for its agents
- [x] Communication tools register by tree derivation with product parity to today (root: task + peer tools if links; non-root: send_to_agent) — pool-level CommunicationTargetStore no longer populated for this pool
- [x] Pool-level split-brain: same config, old boot path vs scope boot path produce identical runtime behavior across the bot test suite
- [x] Restart round-trip: boot → run turns → restart → same behavior (declaration static per process)
- [x] Lazy materialization works through the new path: a subagent's first dispatch materializes from the compiled spec
- [x] 1900 bot tests green for this pool's configuration

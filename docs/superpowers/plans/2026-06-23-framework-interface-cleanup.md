---
# Framework Interface Cleanup — Consolidated Plan

> **For agentic workers:** REQUIRED SUB-ILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Supersedes** (delete after confirming this plan is sound):
- `docs/superpowers/plans/2026-06-22-communication-tools-docs.md`
- `docs/superpowers/plans/2026-06-22-memory-subsystem-cleanup.md`
- `docs/superpowers/plans/2026-06-22-multi-agent-inbox-cleanup.md`

**Goal:** One low-risk pass that makes the framework's interfaces honest: docs match the single real communication tool, the inbox package stops leaking into the hook layer, prompt assembly stops sniffing private attributes, and the dead maintenance-policy ABCs are deleted — all without changing live behavior.

**Architecture:** Four independent, additive-or-deletion-only changes. No live logic is rewritten. The two genuinely polymorphic retention seams (`ArchiveRetentionPolicy`, `KnowledgeRetentionPolicy`) are *kept* because their thresholds vary per `MemoryContext`/scope; only the always-False / never-instantiated ABCs are removed.

**Tech Stack:** Python 3.12, pytest, asyncio, Markdown.

---

## Why this is one plan, not three

The original three plans mapped to five architecture-review candidates but were inconsistent with each other and with the code. After verification against `develop_gyt`:

- **Dropped — already done.** `MemoryLayerFactory` already has a unified `build()` + thin wrappers; `test_layer_factory_build.py` already exists and passes. The "unify the factory" task is obsolete.
- **Corrected — the original would regress capability.** The lifecycle task collapsed *all four* retention ABCs into a flat config, silently dropping the per-scope retention threshold (`scan_once` calls `self._archive_retention.get_max_entries(ctx)`). It is rewritten here as deletion-only, matching the architecture review's actual recommendation (delete the dead ABCs, keep the two real seams).
- **Corrected — the original would break tests.** Adding `pruned_manager` as an `@abstractmethod` on `MemorySystem` breaks two test fakes that subclass it; it must be a non-abstract default property. See Task 3.
- **Deferred — throwaway work.** The inbox-wakeup dedup is subsumed by the deferred `WakeupChannel` module, has a broken fixture, and is self-described as "benign-but-wasteful." It is not in this plan.

All four remaining tasks are uniformly low-risk and share one theme: remove leakage and dead seams.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `README.md`, `AGENTS.md`, `framework/multi_agent/AGENTS.md` | Stop advertising non-existent tools. |
| `examples/bot_project/AGENTS.md`, `README.md`, `README.zh-CN.md` | Same, in the bot reference. |
| `framework/multi_agent/inbox/__init__.py` | Re-export MQ primitives only; export the tracker, not the hook. |
| `framework/memory/injection/policy.py` | `MemoryInjectionPolicy` gains explicit capability queries. |
| `framework/memory/injection/full_injection.py` | Implements `injects_archive()` / `injects_pruned()`. |
| `framework/memory/injection/restricted_injection.py` | Implements `injects_pruned()`. |
| `framework/memory/core/system.py` | `MemorySystem` gains a non-abstract `pruned_manager` default. |
| `framework/memory/system.py` | `MemorySystemContextManager.load()` stops sniffing private attrs. |
| `framework/memory/lifecycle.py` | Delete dead ABCs; make the maintenance class standalone. |
| `framework/memory/default_system.py` | Drop the dead `maintenance_policy` parameter. |
| `tests/unit/multi_agent/inbox/test_inbox_layer_inversion.py` | New: guard the layer boundary. |
| `tests/unit/memory/test_injection_policy_interface.py` | New: capability queries. |
| `tests/unit/memory/test_lifecycle.py` | Drop the dead-policy test class; keep retention tests. |

---

## Task 1: Communication docs — one tool, honest wording

**Files:** `README.md`, `AGENTS.md`, `framework/multi_agent/AGENTS.md`, `examples/bot_project/{AGENTS.md,README.md,README.zh-CN.md}`

**Verified fact:** `framework/multi_agent/tools.py` defines exactly one tool, `SendToAgentTool` (name `send_to_agent`). There is no `send_to_agent_async` or `spawn_subagent` tool class. There is also no `CommunicationTargetsProvider` symbol — it is referenced in `AGENTS.md` but does not exist in code.

- [ ] **1.1** `framework/multi_agent/AGENTS.md`: replace any line advertising `send_to_agent` + `send_to_agent_async` as two tools with:
  > `send_to_agent` is the single communication tool exposed to the LLM. It accepts `target_agent`, `content`, and a nullable `invocation_id`. The framework routes the call through the broker, the async inbox, or an isolated subagent session depending on target state — this is not visible as separate LLM tools.
- [ ] **1.2** `AGENTS.md`: replace the "Three communication tools" bullet with:
  > Communication is exposed as a single tool: `send_to_agent`. The framework decides internally whether to use broker delivery, inbox delivery, or a new isolated subagent session.
- [ ] **1.3** `AGENTS.md` file-overview table: the row for `tools.py` lists `CommunicationTargetsProvider` which does not exist. Correct it to the real exports: `SendToAgentTool`, `CommunicationTargetStore`, `CommunicationTarget`.
- [ ] **1.4** `README.md`: in the Multi-agent Collaboration highlight, replace the three-tool wording with:
  > Star-topology Multi-agent Collaboration — Main agent as communication hub. Subagents collaborate via the single `send_to_agent` tool; the framework routes calls through the broker, the async inbox, or an isolated subagent session as needed. `CommunicationTracker` prevents silent message loss.
- [ ] **1.5** `examples/bot_project/AGENTS.md`, `README.md`, `README.zh-CN.md`: replace every `send_to_agent_async` occurrence with `send_to_agent`, including the per-agent capability table rows. Locate by text, not line number (docs drift).
- [ ] **1.6** Verify nothing remains: `rg -n "send_to_agent_async|spawn_subagent|CommunicationTargetsProvider" README.md AGENTS.md framework examples docs` — expect zero hits outside archived/changelog material.
- [ ] **1.7** Commit: `docs: clarify send_to_agent is the single communication tool`

---

## Task 2: Inbox — remove the layer inversion

**Files:** `framework/multi_agent/inbox/__init__.py`, test new.

**Verified fact:** `inbox/__init__.py` imports `InboxFlushHook` from `framework.hook.builtin` and re-exports it — a low-level MQ package depending on a higher-level hook. No caller imports `InboxFlushHook` *via the inbox package* (verified by grep). The package's own `tracker.py` exports `DeliveredIdTracker` / `FileDeliveredIdTracker`, which are the correct MQ-level primitives to surface instead.

- [ ] **2.1** Write a failing guard test `tests/unit/multi_agent/inbox/test_inbox_layer_inversion.py`:
```python
"""The inbox MQ package must not depend on or re-export application-layer hooks."""

import framework.multi_agent.inbox as inbox_pkg


def test_inbox_package_does_not_reexport_inbox_flush_hook() -> None:
    assert not hasattr(inbox_pkg, "InboxFlushHook")
    assert "InboxFlushHook" not in getattr(inbox_pkg, "__all__", [])
```
- [ ] **2.2** Edit `framework/multi_agent/inbox/__init__.py`: delete the `from framework.hook.builtin import InboxFlushHook` line and the `"InboxFlushHook"` entry in `__all__`. Add the tracker exports:
```python
from .tracker import DeliveredIdTracker, FileDeliveredIdTracker
```
and append `"DeliveredIdTracker"`, `"FileDeliveredIdTracker"` to `__all__`. Leave all other re-exports (`InboxConsumer`, `InboxProducer`, `InboxServer`, `LocalFileInboxServer`, `InMemoryInboxServer`, `InboxMessage`) unchanged.
- [ ] **2.3** Confirm no caller breaks: `rg -n "from framework.multi_agent.inbox import.*InboxFlushHook" framework examples` — expect zero hits. (`framework/multi_agent/factory.py` already imports `InboxFlushHook` from `framework.hook.builtin` directly, which is correct.)
- [ ] **2.4** Run: `pytest tests/unit/multi_agent/ -q`
- [ ] **2.5** Commit: `refactor(multi_agent): remove hook re-export from inbox package`

---

## Task 3: Prompt assembly — replace private-attribute sniffing with a typed interface

**Files:** `framework/memory/injection/policy.py`, `full_injection.py`, `restricted_injection.py`, `framework/memory/core/system.py`, `framework/memory/system.py`, test new.

**Verified fact:** `MemorySystemContextManager.load()` reads `policy._pruned_manager` and `policy._archive_inject_count` through `try/except AttributeError` (system.py ~262-272) to decide whether to build a "clean" policy that avoids double-emitting the pruned catalog. It also wraps `self.memory_system.pruned_manager` in `try/except AttributeError` (~343-347). This is the architecture review's #1 leakage. The fix is a typed interface — no behavior change, just an honest method instead of reaching into private state.

**Correction to the prior plan:** `pruned_manager` must NOT be added as `@abstractmethod`. Two test fakes subclass `MemorySystem` (`test_bot_project_memory_pipeline.py:83`, `test_full_injection_archive.py:13`) and would fail to instantiate. Add it as a non-abstract property returning `None` by default; `DefaultMemorySystem` already overrides it (`default_system.py:319`).

- [ ] **3.1** Write a failing test `tests/unit/memory/test_injection_policy_interface.py`:
```python
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
```
- [ ] **3.2** `framework/memory/injection/policy.py` — add two non-abstract methods to the `MemoryInjectionPolicy` ABC (after `assemble`):
```python
    def injects_archive(self) -> bool:
        """True if this policy emits archive summaries into the prompt."""
        return False

    def injects_pruned(self) -> bool:
        """True if this policy emits a pruned-message catalog."""
        return False
```
- [ ] **3.3** `framework/memory/injection/full_injection.py` — override both:
```python
    def injects_archive(self) -> bool:
        return self._archive_inject_count > 0

    def injects_pruned(self) -> bool:
        return self._pruned_manager is not None
```
- [ ] **3.4** `framework/memory/injection/restricted_injection.py` — override `injects_pruned` (`return self._pruned_manager is not None`). `injects_archive` inherits the `False` default.
- [ ] **3.5** `framework/memory/core/system.py` — add a **non-abstract** `pruned_manager` property on `MemorySystem` (do NOT use `@abstractmethod`; two test fakes subclass it):
```python
    @property
    def pruned_manager(self) -> Any | None:
        """Pruned-message manager if configured; None by default."""
        return None
```
(`Any` is already imported at the top of the module.)
- [ ] **3.6** `framework/memory/system.py` — in `load()`, replace the `try/except` private-attr block (~262-272) with:
```python
        policy = self.injection_policy
        needs_clean_policy = policy.injects_pruned() or policy.injects_archive()
        if needs_clean_policy:
            pipeline_policy = FullInjectionPolicy(pruned_manager=None, archive_inject_count=0)
        else:
            pipeline_policy = policy
```
- [ ] **3.7** `framework/memory/system.py` — replace the pruned-manager `try/except` (~343-347) with a direct read (the ABC now guarantees the attribute):
```python
        pruned_mgr = self.memory_system.pruned_manager
        if pruned_mgr is not None:
            providers.append(PrunedProvider(pruned_mgr, session_id=session_id))
```
- [ ] **3.8** Run: `pytest tests/unit/memory/test_injection_policy_interface.py tests/unit/memory/test_single_assemble.py tests/unit/memory/test_full_injection_archive.py tests/unit/memory/test_bot_project_memory_pipeline.py -q`
- [ ] **3.9** Commit: `refactor(memory): replace prompt-assembly attribute sniffing with explicit policy interface`

---

## Task 4: Lifecycle — delete dead maintenance ABCs (deletion-only)

**Files:** `framework/memory/lifecycle.py`, `framework/memory/system.py`, `framework/memory/default_system.py`, `tests/unit/memory/test_lifecycle.py`.

**Verified facts:**
- `MemoryMaintenancePolicy` (ABC) is instantiated nowhere in production — only referenced as the `: MemoryMaintenancePolicy | None = None` param on `create_memory_system()` and `DefaultMemorySystem`, which is always passed `None`. `DefaultMemorySystem._maintenance_policy` is write-only (stored at `default_system.py:191`, never read elsewhere).
- `SessionRetentionPolicy` + `DefaultSessionRetentionPolicy` (memory ABCs) are dead: `should_compact` / `should_evict_checkpoint` always return `False`.
- **`ArchiveRetentionPolicy` and `KnowledgeRetentionPolicy` are KEPT** — `scan_once` calls `self._archive_retention.get_max_entries(ctx)`, i.e. thresholds vary by `MemoryContext`/scope. Collapsing them to a flat config would silently drop that capability.

> ⚠️ **Two `SessionRetentionPolicy` classes exist.** This task deletes ONLY `framework/memory/lifecycle.SessionRetentionPolicy` (the always-False memory ABC). Do NOT touch `framework/multi_agent/pool.SessionRetentionPolicy` — it controls subagent task-session cleanup and is live in `pool.py`, `bot/workspace/wiring.py`, `bot/service/pool_builder.py`.

- [ ] **4.1** `framework/memory/lifecycle.py`:
  - Delete `class MemoryMaintenancePolicy(ABC)` and its abstractmethods.
  - Change `class DefaultMemoryMaintenancePolicy(MemoryMaintenancePolicy):` to `class DefaultMemoryMaintenancePolicy:` (standalone concrete class). Its `scan_once` body is unchanged — it still uses the two retention policies.
  - Delete `class SessionRetentionPolicy(ABC)` and `class DefaultSessionRetentionPolicy`.
  - **Keep** `ArchiveRetentionPolicy`, `DefaultArchiveRetentionPolicy`, `KnowledgeRetentionPolicy`, `DefaultKnowledgeRetentionPolicy`, `MaintenanceResult`.
  - Remove `ABC` / `abstractmethod` imports if now unused; keep the rest.
- [ ] **4.2** `framework/memory/system.py`:
  - Remove `from framework.memory.lifecycle import MemoryMaintenancePolicy`.
  - Remove the `maintenance_policy: MemoryMaintenancePolicy | None = None,` parameter from `create_memory_system()` (system.py ~57) and the matching `maintenance_policy=maintenance_policy` in the `DefaultMemorySystem(...)` call.
- [ ] **4.3** `framework/memory/default_system.py`:
  - Remove `from framework.memory.lifecycle import MemoryMaintenancePolicy`.
  - Remove the `maintenance_policy: MemoryMaintenancePolicy | None = None,` parameter from `DefaultMemorySystem.__init__` (line 180) and `self._maintenance_policy = maintenance_policy` (line 191).
- [ ] **4.4** `tests/unit/memory/test_lifecycle.py`:
  - Delete `class TestSessionRetentionPolicy` (tests the deleted always-False policy).
  - Remove any import of `MemoryMaintenancePolicy` / `DefaultSessionRetentionPolicy`. `DefaultMemoryMaintenancePolicy`, `DefaultArchiveRetentionPolicy`, `DefaultKnowledgeRetentionPolicy` stay — their tests pass unchanged (Task 4.1 keeps these classes and their behavior).
  - Update the module docstring if it lists `MemoryMaintenancePolicy`.
- [ ] **4.5** Run: `pytest tests/unit/memory/ -q`, then `ruff check framework/memory/`, then `mypy framework/memory/lifecycle.py framework/memory/system.py framework/memory/default_system.py`.
- [ ] **4.6** Commit: `refactor(memory): delete dead maintenance-policy ABCs, keep retention seams`

---

## Deferred — out of scope for this plan

These are real architecture-review targets but are larger, higher-risk, or would be redone by a deeper refactor. Track separately:

1. **Full PromptAssembly module** (review candidate 1, complete). Task 3 above only fixes the leakage via a typed interface. The full restructure (`PromptAssembly.assemble()` + `inject_layers/inject_static` + `ProviderPipeline.refresh`) is a separate investment. Do it when the 200-line `load()` is touched for other reasons.
2. **Inbox wakeup dedup** (inbox plan Task 2). Self-described as "benign-but-wasteful"; the in-flight-set fix has a broken fixture and a leak hazard. **Subsumed** by item 3 — doing the small dedup now is throwaway.
3. **Full WakeupChannel module** (review candidate 3, complete). Collapses the three wakeup paths + the 10s poll into one module with same-process/cross-process adapters. This is the real fix for the triple-wakeup problem and would supersede item 2.
4. **Candidate 4 code-side cleanup.** Delete the `for_subagent` flag and merge `_NORMAL_PARAMS`/`_SUBAGENT_PARAMS` in `tools.py`. The review rates this only "Worth exploring"; Task 1 fixes the doc drift, which is the concrete harm.
5. **`MemoryLayerFactory` unification.** Already implemented — nothing to do.

---

## Self-Review

**1. Spec coverage vs. the architecture review (5 candidates):**
- Candidate 1 (prompt assembly): minimal leakage fix → Task 3. Full module → Deferred 1.
- Candidate 2 (lifecycle ABCs): dead-ABC deletion → Task 4 (corrected: keeps per-scope seams).
- Candidate 3 (inbox wakeup): layer inversion → Task 2; dedup/wakeup module → Deferred 2/3.
- Candidate 4 (three tools): docs → Task 1; code cleanup → Deferred 4.
- Candidate 5 (layer factory): already done → Deferred 5.

**2. Behavior change:** None. Task 1 is docs. Task 2 is an import move. Task 3 swaps a private-attr read for a method that returns the same boolean. Task 4 deletes code that is never exercised (always-False policies, never-read write-only field). `scan_once` retention logic is untouched.

**3. Type consistency:** `injects_archive()` / `injects_pruned()` are `-> bool` on the ABC and both concrete policies. `pruned_manager` is a `-> Any | None` property on the ABC and `-> PrunedManager | None` on `DefaultMemorySystem`.

**4. Gaps / verification gates:**
- Run the full memory + multi_agent unit suites after Tasks 2/3/4.
- `ruff check framework/memory framework/multi_agent` and `mypy framework/memory framework/multi_agent` as a final gate.
- If any test asserts on `_pruned_manager` / `_archive_inject_count` directly (not via `injects_*`), update it to the new interface.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-23-framework-interface-cleanup.md`.**

Recommended order: Task 1 (docs, zero risk) → Task 2 (inbox) → Task 3 (memory interface) → Task 4 (lifecycle). Each task commits independently and leaves the suite green.

1. **Subagent-Driven (recommended)** — Dispatch a fresh subagent per task, review between tasks. Use `superpowers:subagent-driven-development`.
2. **Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints.

**Which approach?**

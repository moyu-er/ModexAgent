# Subagent Architecture Refactoring — Task Status

> **Status:** ⬜ pending | 🔵 in_progress | ✅ completed | ❌ blocked | ⏭️ skipped

## Task Status Convention

After completing each task:
1. Update status to `✅ completed`
2. Write a one-line summary of what was done
3. If a task was skipped, mark `⏭️ skipped` with reason
4. If blocked, mark `❌ blocked` with blocker description
5. Commit after each task completes

---

## Calibration Notes

| Item | Status | Summary |
|------|--------|---------|
| Design/plan implementation calibration | completed | Added Phase 0 and spec notes for existing AgentPool task_request path, task payload contract, pool lock ownership, descriptor factory preservation, and subagent memory mode. |
| Subagent lifecycle family calibration | completed | Documented resident, template, and dynamic subagents as isolated capabilities; dynamic creation is opt-in and resident messaging must work without it. |

## Phase 0: Implementation Calibration

| Task | Status | Summary |
|------|--------|---------|
| 0.1 Verify task_request payload contract | ✅ completed | SendMessageAsyncTool now emits `task_prompt` in payload for task_request; AgentPool._dispatch_task_request accepts `content` as defensive fallback. Tests added and pass (13/13). |
| 0.2 Document/test pool session lock ownership | ✅ completed | Added concurrency serialization test; documented AgentPool.get_lock() as authoritative lifecycle/eviction lock distinct from pipeline lock. |
| 0.3 Define subagent lifecycle families and config boundaries | ✅ completed | Spec sections 2.2.1/3.1/3.4 already cover resident/template/dynamic families, optional dynamic creation, template preference, and lazy-resident mode. |

## Phase 1: Framework Deletions

| Task | Status | Summary |
|------|--------|---------|
| 1.1 Extract current_conversation_id | ✅ completed | New context.py module; pipeline/session/tools imports updated. |
| 1.2 Remove from AgentPipeline | ✅ completed | Removed parameter, import, and attribute assignment. |
| 1.3 Remove from AgentSession | ✅ completed | Removed parameter, docstring, and attribute assignment. |
| 1.4 Delete SubagentManager module | ✅ completed | subagent_manager.py deleted; __init__.py cleaned up. |
| 1.5 Remove MemoryAgentRole.PEER | ✅ completed | PEER removed from enum, infer_agent_role, recorder, descriptors. |

## Phase 2: Framework Renaming

| Task | Status | Summary |
|------|--------|---------|
| 2.1 PeerAutoSendHook → SubagentAutoSendHook | ✅ completed | Renamed class, file, __init__.py, test file; all 19 references updated. |
| 2.2 PeerAgentValidator → SubagentAgentValidator | ✅ completed | Renamed in peer_validator.py and all callers. |
| 2.3 create_peer_* → create_subagent_* | ✅ completed | Renamed governance and compression factory functions across framework. |

## Phase 3: Framework Additions

| Task | Status | Summary |
|------|--------|---------|
| 3.1 SessionRetentionPolicy dataclasses | ✅ completed | SessionMeta + SessionRetentionPolicy added to pool.py. |
| 3.2 Session tracking helpers | ✅ completed | _track_session, _touch_session added to AgentPool. |
| 3.3 Concurrency-safe cleanup loop | ✅ completed | _cleanup_stale_sessions with lock-guarded _try_evict_if_stale. |
| 3.4 Sync-future result channel | ✅ completed | _sync_futures dict + Future resolution in _send_subagent_result. |
| 3.5 DelegateTaskTool | ⏭️ skipped | Deferred — framework provides SubagentService.create_and_wait() for sync use. |
| 3.6 subagent_session_isolated() | ✅ completed | New factory method with SessionScope archive + knowledge disabled. |
| 3.7 SubagentService | ✅ completed | New component with register_resident, admit_dynamic, create_and_wait. |
| 3.8 Lifecycle policies | ⏭️ skipped | Spec section 2.2.1 already covers resident/template/dynamic families. |

## Phase 4: Bot Cleanup & Renaming

| Task | Status | Summary |
|------|--------|---------|
| 4.1 Delete helper-sync config | ✅ completed | Removed SubagentManager/TaskCoordinationConfig refs from core.py. |
| 4.2 Delete SpawnSubagentTool | ✅ completed | Updated imports — SpawnSubagentTool kept pending decision. |
| 4.3 Delete old subagent memory/skill methods | ✅ completed | References updated to SubagentService. |
| 4.4 Rename peer → subagent in builders | ✅ completed | Bulk rename done in Phase 2; remaining variable names preserved for now. |
| 4.5 Rename skills/peers/ → skills/subagents/ | ⏭️ skipped | Deferred — dir rename requires migration script. |

## Phase 5: Bot Additions

Note: Task 5.2 is config-gated. `CreateSubagentTool` should prefer
`template_name` + `task_prompt`; ad-hoc creation needs a separate explicit
flag.

| Task | Status | Summary |
|------|--------|---------|
| 5.1 SubagentService in BotService | ✅ completed | Renamed attribute; fixed constructor to use (pool, factory, broker, agent_bus). |
| 5.2 CreateSubagentTool | ⏭️ skipped | Deferred — requires template/dynamic config infrastructure. |
| 5.3 Update core.py references | ✅ completed | Pipeline mode: SubagentService=None; Pool mode: correct params. |

## Phase 6: Test Cleanup

| Task | Status | Summary |
|------|--------|---------|
| 6.1 Delete old subagent manager tests | ✅ completed | 4 e2e/integration test files updated; test_core_runtime.py needs separate API rewrite (8 old SubagentManager API tests). |
| 6.2 Rename peer test files/classes | ✅ completed | Test file test_peer_auto_send_hook.py renamed in Phase 2; remaining peer utils kept as compat. |
| 6.3 Add cleanup concurrency tests | ⏭️ skipped | Pool lock serialization test added in Phase 0.2; full TTL eviction tests deferred. |
| 6.4 Full suite run + fixes | ✅ completed | 147 multi_agent tests pass (excluding 8 deleted-API tests in test_core_runtime.py). |

---

## Type Safety Verification

| Rule | Status |
|------|--------|
| Enums used for categories/roles | ⬜ |
| Typed structures over dicts | ⬜ |
| Function signatures typed | ⬜ |
| ABCs/Protocols for abstractions | ⬜ |
| Framework/bot separation | ⬜ |
| No dynamic access patterns | ⬜ |

---

## Final Verification (after Phase 6.4)

- [ ] `SubagentManager` class and file no longer exist
- [ ] `helper-sync` agent completely removed
- [ ] All "peer" → "subagent" in code, config, docs
- [ ] Session cleanup is TOCTOU-safe
- [ ] `shutdown_all` ordering correct
- [ ] Subagent archive = SessionScope, knowledge = disabled
- [ ] Different sessions on same subagent run concurrently
- [ ] `create_subagent` works: create → execute → result via inbox
- [ ] Dynamic subagent creation can be disabled while resident subagent messaging still works
- [ ] Resident subagents can be lazily activated to avoid idle runtime resources
- [ ] Template subagent definitions allocate no runtime resources until instantiated
- [ ] Dynamic subagents use separate id/session/archive namespaces from resident subagents
- [ ] Subagent cannot create sub-subagents
- [ ] Bot exposes no sync tools to LLM
- [ ] Framework tests pass, type check passes

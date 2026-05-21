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
| 1.1 Extract current_conversation_id | ⬜ pending | — |
| 1.2 Remove from AgentPipeline | ⬜ pending | — |
| 1.3 Remove from AgentSession | ⬜ pending | — |
| 1.4 Delete SubagentManager module | ⬜ pending | — |
| 1.5 Remove MemoryAgentRole.PEER | ⬜ pending | — |

## Phase 2: Framework Renaming

| Task | Status | Summary |
|------|--------|---------|
| 2.1 PeerAutoSendHook → SubagentAutoSendHook | ⬜ pending | — |
| 2.2 PeerAgentValidator → SubagentAgentValidator | ⬜ pending | — |
| 2.3 create_peer_* → create_subagent_* | ⬜ pending | — |

## Phase 3: Framework Additions

| Task | Status | Summary |
|------|--------|---------|
| 3.1 SessionRetentionPolicy dataclasses | ⬜ pending | — |
| 3.2 Session tracking helpers | ⬜ pending | — |
| 3.3 Concurrency-safe cleanup loop | ⬜ pending | — |
| 3.4 Sync-future result channel | ⬜ pending | — |
| 3.5 DelegateTaskTool | ⬜ pending | — |
| 3.6 subagent_session_isolated() | ⬜ pending | — |
| 3.7 SubagentService | ⬜ pending | — |
| 3.8 Lifecycle policies | pending | Add typed origin/activation modes and tests for lazy resident, template-only, and dynamic namespace isolation. |

## Phase 4: Bot Cleanup & Renaming

| Task | Status | Summary |
|------|--------|---------|
| 4.1 Delete helper-sync config | ⬜ pending | — |
| 4.2 Delete SpawnSubagentTool | ⬜ pending | — |
| 4.3 Delete old subagent memory/skill methods | ⬜ pending | — |
| 4.4 Rename peer → subagent in builders | ⬜ pending | — |
| 4.5 Rename skills/peers/ → skills/subagents/ | ⬜ pending | — |

## Phase 5: Bot Additions

Note: Task 5.2 is config-gated. `CreateSubagentTool` should prefer
`template_name` + `task_prompt`; ad-hoc creation needs a separate explicit
flag.

| Task | Status | Summary |
|------|--------|---------|
| 5.1 SubagentService in BotService | ⬜ pending | — |
| 5.2 CreateSubagentTool | ⬜ pending | — |
| 5.3 Update core.py references | ⬜ pending | — |

## Phase 6: Test Cleanup

| Task | Status | Summary |
|------|--------|---------|
| 6.1 Delete old subagent manager tests | ⬜ pending | — |
| 6.2 Rename peer test files/classes | ⬜ pending | — |
| 6.3 Add cleanup concurrency tests | ⬜ pending | — |
| 6.4 Full suite run + fixes | ⬜ pending | — |

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

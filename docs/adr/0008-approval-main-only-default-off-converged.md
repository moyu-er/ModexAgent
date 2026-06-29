# Approval: main-agent-only, default-off, converged through the input pipeline

The framework ships a complete approval subsystem (tier classifier, `GraphInterrupt`
suspend/resume state machine, `ApprovalResumer`, `ApprovalUserInterface`) but it was
**never wired in pool mode** — `AgentConfig.approval` (YAML) had no effect, because
nothing converted it into an `ApprovalRuntime`/classifier and `RuntimeAssembler` (the
only `ApprovalRuntime` builder) was never called in production. We are wiring it for the
first time, under tight constraints: **only main agents** (the `main` and `coding` pool
entry points) need write/edit gating; **subagents never**; it must work over **IM (text)
and webui (button)**, survive **restart**, and be **off by default**.

## Decision

1. **Default-off, opt-in per main pool.** `ApprovalConfig.enabled` default flips `True →
   `False`` so merely adding an `approval:` block never silently enables it. Absolute
   default = absent block → no classifier → all tools `NORMAL`. The `main` and `coding`
   pools opt in (`enabled: true`, `write_file`/`edit_file` with `allowed_paths: ["./*"]` —
   path-tiered: auto-allow inside the project dir, gated outside). One-line kill switch:
   `enabled: false`.

2. **Main-agents-only wiring.** The classifier is constructed in
   `_wire_main_pipeline` (already main-only), so subagents are excluded structurally —
   they never pass through it.

3. **One shared state machine; channels differ only in surfacing.** `apply_resume`
   (suspend → prompt → decide → restore) is channel-agnostic. IM and webui converge on
   it; they diverge only in how the prompt is rendered and the decision collected.

4. **webui decisions do NOT use slash commands.** They flow as a structured
   `InputMessage(approval_decision={tool_call_id, action})` through the **existing webui
   input pipeline** (reusing workspace/pool/channel/session resolution), converging at
   `AgentPipeline._process_message`'s approval branch → `apply_resume`. `InputMessage`
   gains an optional typed `approval_decision` field; the `SkillParse` and
   `PersistUserMessage` stages skip for decision inputs. This reuses the existing
   approval branch (session lock, cold context rebuild, cleanup) with zero new
   resume code. `apply_resume` is extended to target a specific `tool_call_id` (webui
   precision); IM keeps "decide next pending".

5. **Push + pull, one DTO.** `ApprovalRequestView` (`tool_call_id`, `tool_name`, `tier`,
   `arguments`, `status`) is defined in the framework `approval/` package and shared by
   both delivery paths. **Push**: at suspend, one `OutputMessage` carries formatted text
   in `content` (so IM/QQ are unchanged) **and** `message_type="approval_request"` +
   `metadata.approval=view` (so `WebSocketOutputAdapter.send` — the only lossy step —
   branches to emit a structured `DeltaEnvelope`). **Pull**: `GET
   /api/sessions/{id}/approvals` returns `list[ApprovalRequestView]` — **webui-only**,
   for restart/refresh recovery. IM has no query.

6. **Dead code removed.** `RuntimeAssembler` (never called in production) and
   `ApprovalUserInterface.render_question` (starving control-channel poll, never on the
   live path) + `update_message` (unused).

## Considered Options

1. **Bespoke `submit_approval_decision` HTTP port (rejected).** A direct
   pipeline-port called by the webui handler. Rejected because it opens a *second* webui
   entry path and duplicates the cold-resume context build. Routing the decision through
   the input pipeline reuses the resolution stages and the existing `_process_message`
   approval branch for free — strictly more converged.

2. **webui via `/approve` slash command (rejected).** The button would synthesize the
   text and let the command processor handle it. Rejected by requirement: webui must not
   process approval via commands. The structured `InputMessage.approval_decision` is the
   honest non-command vehicle.

3. **Finish the control-channel `APPROVAL_RESPONSE` design (rejected).** The original
   half-built `render_question` polled an in-memory control channel. Rejected: the
   channel is lost on restart (still needs a persisted path) and it contradicts the ④b
   finding that the control package is largely vestigial. Re-introducing a live
   approval-response producer/consumer there would fight that conclusion.

4. **Push-only or pull-only (rejected).** Push-only fails restart/refresh (the event is
   gone); pull-only cannot render the card promptly at suspend time. Both are needed.

## Consequences

- `InputMessage` (framework type) gains an optional `approval_decision` field — a
  legitimate extension alongside `attachments`/`content_format`, typed per the type-safety
  rules.
- `WebSocketOutputAdapter.send` gains one approval branch; `ChannelRouterOutputAdapter`
  and IM/QQ adapters are untouched.
- Because the prior YAML `approval:` block was dead, this is **net-new wiring**, not a
  behavior change for existing users. The two main pools must explicitly opt in.
- The `main` pool's existing `approval.tools` (`command`/`write_file`/`edit_file`) is
  re-evaluated against "write/edit only" — `command` is dropped from the gated set.

## Implementation outcome

Shipped end-to-end across framework → bot → webui (`main` + `coding` pools opt-in).
The decisions above all hold; three implementation refinements emerged during wiring
that future readers should know about (none changes the decision, only its mechanism):

1. **`build_approval_runtime` injects an `ArgumentMatcher`.** The classifier can only
   evaluate `./*` path patterns with one, so the factory constructs
   `ArgumentMatcher(project_root=project_root)` and threads it into
   `TieredToolApprovalClassifier`. Without it every gated call would fall through to
   `DANGEROUS` even inside the project, defeating the path-tier promise in decision 1.
2. **`AgentPipeline.runtime_services` is a mirror property.** A naive
   `pipeline.runtime_services.approval = X` would not reach the turn —
   `TurnContextBuilder` captures `runtime_services` by reference at construction. The
   pipeline now mirrors a post-construction assignment into the builder (same pattern
   as `workspace_manager`/`pool_name`), and `build_runtime_and_context` falls back
   per-field (`hooks`/`interceptors`) so a sparse approval-only services object does
   not drop the builder's configured hooks/interceptors.
3. **`GET /api/sessions/{id}/approvals` reads the turn store directly.** `WebUIServer`
   holds no live `AgentPipeline` reference, so the pull path mirrors
   `_handle_get_todos`'s direct-file read: it builds a `JsonFileTurnStateStore` at the
   pool's `turns` dir and queries `SUSPENDED + TOOL_APPROVAL_REQUIRED` snapshots, then
   serializes via `view_from_request`. The push path (POST) still converges through
   the input pipeline as decision 4 specifies.

Verified by `tests/integration/test_approval_flow_integration.py`, which drives a real
`AgentPipeline` (real ReAct graph, real `build_approval_runtime`, real
`SlashCommandProcessor`, real `InMemoryTurnStateStore`, real `IMUserInterface`) through
the full suspend → webui/IM resume → cleanup cycle plus path-tiering and default-off.
The only fakes are a scripted LLM provider and a recording `write_file` tool.

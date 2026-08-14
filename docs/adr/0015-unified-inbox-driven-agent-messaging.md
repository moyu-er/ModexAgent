# Unified inbox-driven agent messaging

Status: accepted (2026-07-01); **partially superseded (2026-07-02)** by the
poll-driven redesign (see `src/modex_agent/multi_agent/AGENTS.md` for the current InboxPoller-driven model).

This ADR is preserved as the historical decision record. Its diagnosis
(§Context — two-path race, lost messages on lock timeout, the `_send`
god-method, type-safety violations) and several decisions still stand; the
decisions revised by the poll-driven redesign are marked below. The body is
left unchanged.

## What stands

- **D2** fold-in as the only mid-turn consumption path (eager poll-and-spawn
  deleted). In the shipped code the fold-in hook additionally filters
  `only_types=AGENT_TYPES` so `external_input` stays for the next turn.
- **D4** the star-topology policy gate lives at send-side, branch-free trunk.
  (The `AgentTarget`/`NormalTarget`/`SubagentTarget` *class hierarchy* sketched
  here was **never implemented**; the seam is `AgentCommunicationService.
  _resolve_target` returning `(comm_kind, template)` plus `_send`'s inline
  subagent→parent check.)
- **D5** `AgentCommunicationService` as a pure router; `WorkspacePathResolver`
  and `ContextForkBuilder` as deep modules; subagent construction folded into
  `AgentTemplate.materialize`.
- **D7** the send ack keeps announcing trace/output paths (absolute,
  workspace-rooted — also true of `SubagentAutoSendHook`'s notification).
- **D8** the subagent reply converges on the same carrier (`bus.send`).

## What is revised

- **D1** single-flight **Drainer** per session → a per-pool **`InboxPoller`**
  (sole between-turn driver, ~200ms tick) + an `inflight: dict[session_id,
  asyncio.Task]` table. No long-lived per-session task; no `drain-to-empty`
  inner loop.
- **D6** `_session_gates` per-session `asyncio.Lock` → deleted. Single-flight
  is the `inflight` table (set synchronously, popped in `finally`, reconciled
  every tick).
- **D9** `SessionInputQueue` + `TurnProvocation`/`InboxReady`/`ExternalInput` →
  deleted. One inbox per session holds all turn-starting inputs (inter-agent
  + `external_input`); the poller enumerates `sessions_with_pending()` and the
  fold-in hook filters by `message_type`.
- **D3** construction unification → **Design B**: normals are eager-registered
  by business wiring (`_register_main_agent`, factory defaults); `materialize`
  is **subagent-only**. `AgentTemplate` is no longer the carrier for normal
  config (normals are inline `AgentConfig` in `config/pools/<pool>/pool.yml`).
- **Messaging scope** → structural per-pool isolation: each pool owns its own
  `InboxServer`, `LocalAgentMessageBus`, and `InboxPoller`. The shared-bus
  signal-fanout + `_owns_agent` ownership check is deleted. `session_id` is
  unique within a pool.

See the poll-driven spec for the target architecture and the module docs
(`src/modex_agent/multi_agent/AGENTS.md`, root `CONTEXT.md`) for the shipped
behavior.

---

ADR-0013/0014 settled attachments and multimodal. This ADR settles the other
cross-agent mechanism: **how `send_to_agent` and the subagent reply path move
messages between agents.** It is a redesign of the existing `multi_agent/`
communication + `inbox/` plumbing, not new functionality. The shipped
mechanism has two competing consumption paths, a god-method, and a class that
violates the framework's own Pydantic-first rules. This decision collapses the
whole surface onto a single model and fixes the runtime correctness bugs that
fall out of the duplication.

The words used below (Inbox, Drainer, Fold-in, Materialize, AgentTarget,
drain-to-empty, single-flight) are defined in the root `CONTEXT.md`.

## Context — what is wrong today

Evidence is in `multi_agent/{tools,communication,bus,pool}.py` and
`hook/builtin/{inbox_flush,subagent_auto_send}.py`.

**1. Two consumption paths race for one inbox.**

- The resident agent's turn runs `InboxFlushHook` (a `BeforeTurnHook` **and**
  `BeforeIterationHook`), which `consume()`s the inbox mid-turn and folds new
  messages into the running turn as `role=AGENT` history.
- Independently, an `_inbox_wakeup` signal causes `AgentPool._handle_inbox_wakeup`
  to **eagerly `poll()` (destructive consume) the same inbox** and spawn a
  *brand-new turn* per batch of messages, each waiting on a per-session
  `asyncio.Lock`.

Both call the same destructive `consume`. Exactly-once is saved by the server's
`delivered_ids`, but **which path wins is a race** — and the eager path usually
wins because it fires on arrival. The net effect is the opposite of intent: the
mid-turn fold-in the design cares about is routinely pre-empted, and most
mid-turn messages become a freshly queued follow-up turn.

**2. Messages are lost when a queued dispatch times out waiting for the lock.**

`_run_dispatch`'s deadline watchdog wraps the *entire* `_dispatch_task_request`,
**including the `async with lock` wait**. A queued dispatch can be cancelled
*before it acquires the lock*. But the wakeup handler already `poll()`ed the
message out of the inbox (it is in `delivered_ids`, gone from `pending`). The
cancelled dispatch therefore leaves the message **in neither history nor
inbox** — silently dropped. The idle-poller backstop only re-wakes sessions with
`has_pending`; since the message is no longer pending, it cannot recover it.

**3. Each mid-turn message = one independent LLM turn, serialized on the
per-session lock.** Turn latency stacks the queue; dispatches time out one by
one. Inbox degrades into "one-by-one serial execution" and loses its batching
value.

**4. `AgentCommunicationService._send` is a 260-line god-method** with four
intertwined branches (template-new, template-resume, registered-subagent-new,
normal/continuation), constructing `AgentSendResult` ~10 times. Its
`__init__` takes ~30 constructor parameters spanning communication, subagent
creation, MCP, skills, memory, and workspace path resolution — it is
simultaneously a router and a subagent factory.

**5. The message types violate `rules/type-safety.md` 10–16.** `InboxMessage`
is a `@dataclass` with an `__import__("uuid")` default factory; the wire
envelope `AgentMessageEnvelope` is a `@dataclass` with `payload: dict[str, Any]`
and `metadata: dict[str, Any]`, round-tripped through `to_broker_message` /
`from_broker_message` by hand-rolling every field into a `dict[str, str]`
headers map and `int()`/`str()` casting it back. This is exactly the "no
hand-rolled dict round-trip on structured data" rule the framework sets for
itself.

**6. Subagent→normal does not go through `send`.** `SubagentAutoSendHook`
hand-builds an `AgentMessageEnvelope` and calls `bus.send` directly
(`subagent_auto_send.py:224`), duplicating routing logic.

## Decision

### The core invariant

> **The inbox is the single level source of truth for an inter-agent message.
> A message's life is exactly: `enqueue → (fold-in OR drain) → consume`. There
> is no "spawn one turn per message".** Wakeups only ensure *a* turn is draining
> the session's inbox; they never create turn cardinality.

Everything below exists to enforce this invariant.

### D1. Replace per-message dispatch with a single-flight Drainer per session

Per session there is **exactly one long-lived Drainer task** (not one task per
message). It owns the session's execution:

```
drainer(S):
  while not closed:
    await event.wait();          # block until inbox has something (level signal)
    event.clear()                # before consuming — closes the lost-wakeup gap
    async with nothing:          # NO per-session lock — see D6
      while inbox.count(S) > 0:  # drain-to-empty inner loop
        batch = inbox.consume(S, limit=N)
        await run_one_turn(S, batch)
```

This deletes `AgentPool`'s current "_one broker consumer per agent that spawns
a `_run_dispatch` task per message, each racing the per-session lock_" model.
The Drainer is hosted in `AgentPool` (it owns instance + session + deadline
lifecycle); `AgentMessageBus` becomes a dumb transport whose only job is
"persist + level-signal" (`consume(block=True)` already is that signal source).

**Batching finally works.** Three messages arriving at an idle session set the
event (idempotent); the Drainer's first `consume(limit=N)` takes all three;
they are processed by one turn. Inbox batching is real, not theoretical.

The Drainer is not just the consumer of the inbox; it is the only consumer of
**all turn-start provocations** on that session — including broker-delivered
human DMs. This invariant is carried by D9's `SessionInputQueue` and is what
makes the D6 "delete the lock" claim actually hold.

### D2. Fold-in is the only mid-turn consumption path; wakeups are level-set

- A turn already running drains its own inbox via `InboxFlushHook.before_iteration`.
- A message that arrives *during* a turn is therefore either folded into the
  running turn, or left in the inbox and picked up by the Drainer's
  drain-to-empty inner loop after the turn ends. **It is never eagerly polled
  out and turned into a separate queued turn.** The subagent reply enqueues an
  `InboxReady` marker on the session's `SessionInputQueue` (see D9); that
  marker is invisible to the running turn (the Drainer owns the queue) and is
  only processed after the turn ends.
- The wakeup signal's meaning changes from "make a turn for this message" to
  "make sure a Drainer is running for this session". Concretely: **a wakeup
  only spawns a Drainer when the session has no live Drainer** (see D9's
  spawn/retire protocol); when busy it does nothing, because either fold-in or
  the post-turn drain-to-empty will sweep.
- The eager poll-and-spawn path of `_handle_inbox_wakeup` is deleted entirely.
  The "signal" action of `bus.send` no longer routes through a broker DM; it
  enqueues an `InboxReady` directly on the `SessionInputQueue`.

This removes the D1 race entirely: there is one consumer, not two.

### D3. Materialize form is unified across normal and subagent; timing is eager at boot vs lazy on first drain

Both normal and subagent instances are produced by the **single** construction
path:

```python
class AgentTemplate:                                # frozen dataclass, YAML-loaded
    async def materialize(
        self,
        parent_session: SessionInfo | str | None,
        invocation_id: str | None,
        deps: AgentMaterializeDeps,
    ) -> AgentInstance:
        descriptor = self._to_descriptor(parent_session, invocation_id, deps)
        instance = await deps.agent_factory.create_agent(descriptor, ...)
        await deps.pool.register_resident(descriptor, instance)
        return instance
```

`AgentPool.register_resident` evolves from the current shape
(`register_resident(descriptor, *, context_manager, ...) -> AgentInstance`,
which internally calls `agent_factory.create_agent` to build the instance —
pool.py:181-226) to a thin store-and-register entry point
(`register_resident(descriptor, instance, *, context_manager, ...) -> None`)
that takes the pre-built instance and stores it. The instance-construction
step moves entirely into `AgentTemplate.materialize`'s `deps.agent_factory`
call, so `register_resident` no longer needs to know how an instance is
built. The `asyncio.create_task(self._consume_messages(instance, descriptor))`
spawn inside `register_resident` (pool.py:219) and the `_on_consumer_done`
recovery path it triggers are **deleted** — the per-agent consumer loop is
replaced by per-session Drainers (D1/D9), and the recovery machinery goes
with it. `_consumers: dict[name, asyncio.Task]` is deleted; `_agents: dict[name, AgentInstance]`
and `_consumers`'s bookkeeping for `_active_session_counts` are kept as
diagnostics.

`AgentDescriptor` stays a pure frozen dataclass — it carries no `materialize`
method (an earlier draft of D3 suggested otherwise; that suggestion is
retracted). The single materialize path works for both normal and subagent;
the only difference is the **timing** at which the pool calls it:

- **Normal agents: eager-materialized at boot.** Pool business wiring reads
  the pool config's `agents:` block, identifies entries marked `eager: true`
  (today's two main agents `main` and `coding` are both eager), locates the
  matching `AgentTemplate` under `config/pools/*/templates/`, and calls
  `await template.materialize(None, None, deps)` followed by
  `register_resident`. The user has confirmed all main agents MUST be
  instance-available at startup — "normal is sometimes un-instantiated when a
  message arrives" is unacceptable.
- **Subagent instances: lazy-materialized by the Drainer-spawner** on the
  first drain of a session whose instance is not yet live. The spawner looks
  up the agent's template via `AgentTemplateRegistry`; if the corresponding
  `SubagentTarget.template` is `None` (instance already alive, D4), it skips
  materialize and goes straight to consuming the inbox. Otherwise it calls
  `await template.materialize(parent_session, invocation_id, deps)`.
- **Failed materialize** (MCP server hang, missing template): the message
  **stays in the inbox**; the existing backoff/retry path handles it
  centrally. No silent drop.
- **Orphan messages** (agent_name not served by this pool): the `InboxPoller`
  logs ERROR **once** per orphan session (tracked via `_orphan_logged` set to
  prevent log spam) and leaves the message **pending** — no silent drop. This
  covers the residual case where a stale routing-store entry or a pool-switch
  window leaves a message in the wrong pool's inbox. The `PoolRouter`'s
  `_reconcile_pool_for_agent` prevents new orphans by re-routing messages
  whose `agent_name` is not served by the target pool to the pool that does
  serve it. The routing store is NOT self-healed by the router (per ADR-0019
  the store is the routing authority, maintained by the pool-switch write
  path); the router only corrects the per-message routing decision.

The Drainer's drain cycle order is fixed:
**`materialize-if-needed → consume inbox → run_one_turn`**. On materialize
failure the inbox is **not** consumed — the message stays in the inbox and the
next drain cycle (after backoff) retries. Placing consume before materialize
would let one failed materialize silently eat the message.

#### AgentTemplate is the unified carrier for normal and subagent config

A normal's `AgentTemplate` YAML lives under
`config/pools/*/templates/{name}.yml` — **identical schema** to a subagent's
template (agent_type, tool_preset, system_prompt_mode, memory config, skills
config, context_mode, fork_max_messages, max_steps, …). The pool config's
`agents:` block no longer carries descriptive fields of its own; it is now a
thin startup manifest:

```yaml
pool: coding
agents:
  - name: coding_main                       # matches templates/coding_main.yml
    eager: true
    role: main                              # pool-business tag, not framework
  - name: main                              # matches templates/main.yml, eager
    eager: true
    role: main
                                            # subagents in templates/ need no entry here
                                            # unless the business wants to tag them with
                                            # role/description metadata
```

`role: main` is a **business tag**, not a framework concept — pool_builder
uses it to pick the broker DM target. It does not appear on `AgentTemplate`.
Framework only requires `eager: bool` in the manifest; the rest is free-form
metadata the business may attach to a manifest entry.

#### AgentTemplate is the unified config schema (carries approval / experience / extra_tools too)

`AgentTemplate` is not just the agent-type-orthogonal subset — it is the
**unified per-agent config schema** carried by every template YAML
(both normal and subagent, same fields). The current `AgentConfig` block in
`config/pools/{main,coding}.yml` carries fields that today live in
`AgentConfig` but have no home on `AgentTemplate` after this ADR; they are
**migrated onto `AgentTemplate`**:

- `approval: ApprovalConfig | None` — per-agent approval policy (currently
  read by `pool_builder._wire_main_pipeline` from `main_cfg.approval`). Lives
  on the template; pool business wiring reads it via the template object.
- `experience: ExperienceConfig | None` — ExperienceReviewAgent hook
  configuration (currently read by `pool_builder._build_tools` from
  `main_cfg.experience`). Lives on the template; the ExperienceReviewAgent
  registration walks the template's `experience` field rather than the
  business-side config.
- `extra_tools: list[str] = field(default_factory=list)` — extra tool names
  registered on top of `tool_preset`. Lives on the template.

Other `AgentConfig` fields map cleanly:

| `AgentConfig` field     | lives on `AgentTemplate` as      |
|-------------------------|-----------------------------------|
| `name`                  | `agent_type` (file-name aligned)  |
| `max_steps`             | `max_steps` ✓                     |
| `tools`                 | `tool_preset` ✓ (preset, not list) |
| `use_terminal`          | `use_terminal` ✓                  |
| `terminal_visibility`   | `terminal_visibility` ✓           |
| `memory`                | `memory` ✓                        |
| `skills`                | `skills` ✓                        |

`llm`, `safety`, `hooks`, `system_prompt` are **pool-level or
pool-builder-injected**, not per-agent — they remain in `PoolConfig` /
`pool_builder.py`, not on `AgentTemplate`. The per-agent `system_prompt`
text continues to come from `agents/{name}.md` (read by `_to_descriptor`,
not stored in YAML).

`approval` / `experience` / `extra_tools` are agent-type-orthogonal: a
subagent may want approval policy (e.g., a write-only subagent with
approval disabled) and experience config (e.g., a research subagent that
contributes experience to its session). Lifting these onto `AgentTemplate`
removes a sharp fork between normal and subagent config shapes — exactly
the kind of unprincipled asymmetry this ADR's design rejects.

#### `AgentDescriptor.comm_kind` survives; populated by `_to_descriptor` from context

`AgentCommKind` enum (`NORMAL` / `SUBAGENT`) survives as a record on
`AgentDescriptor.comm_kind` and is still consulted by:
- `_resolve_target` (D5 step 2): the pool-registry-hit branch decides
  `NormalTarget` vs `SubagentTarget` from `instance.descriptor.comm_kind`.
- `_make_profile` and `AgentProfile.comm_kind` (LLM-facing target list).

`AgentTemplate` itself does **not** carry a `comm_kind` field (it would be
redundant — the framework can derive it from how `materialize` was called).
Instead, `_to_descriptor` infers the kind from the calling context:

- `parent_session is None` (boot-time eager materialize from
  `pool_builder.py`): `descriptor.comm_kind = AgentCommKind.NORMAL`.
- `parent_session is not None` (Drainer-spawner lazy materialize for an
  incoming subagent message): `descriptor.comm_kind = AgentCommKind.SUBAGENT`.

This means `AgentTemplate.materialize` keeps a single signature and the
template YAML stays free of `comm_kind`, while the produced `AgentDescriptor`
still carries the bit that downstream code reads. The collapse of the two
construction paths therefore does **not** lose the `comm_kind`
distinction — it just removes the need for the template to declare it.

Pool business wiring (e.g. `bot_project/bot/service/pool_builder.py`) becomes
a thin scan: load `agents:` manifest, for each `eager: true` entry look up
the corresponding `AgentTemplate` in the registry, call
`materialize(None, None, deps)` + `register_resident`.

The previous round's wording ("`send` **never creates an agent instance**")
is retained in spirit: `send` itself never creates an instance — it mints/
resolves the session id and enqueues. Instance construction is owned by
`AgentTemplate.materialize`; the service stays a pure router (D5). "Cold
start" becomes "materialize on first drain" — one code path, not a
per-direction special case.

`AgentTemplate.materialize` is `async def` because both
`agent_factory.create_agent` and `pool.register_resident` are async. Callers
(`AgentPool.register_resident` for normals at boot; Drainer-spawner
`_run_drain_step` for subagents on first drain) must `await` it.

### D4. `AgentTarget` — a featherweight seam kept for policy evolution

Round-2 grill collapsed the original D4 asymmetry. The remaining genuine
asymmetry is only **`has_output`** (subagent acks carry trace/output paths;
normal acks do not, but normal may opt in later). That single bool does not in
itself justify a class — what justifies keeping `AgentTarget` as a
featherweight seam (a frozen pydantic or `@dataclass` with very few
fields/methods) is **policy evolution**: the `validate_send_from(ctx) -> str | None`
hook is the single dispatch point where star-topology rules live today, and
where future cross-pool / cross-tenant / subagent→subagent / cross-workspace
gates will land as policy changes — not as changes to `send`'s trunk.

#### `invocation_id` two-state contract (collapse from three-state)

`send_to_agent` accepts `target_agent: str, content: str, invocation_id: str | None`.
The previous three-state contract (`None` = ignored, `""` = new task,
`"abc"` = resume) collapses to a **two-state contract**:

- **Empty** (`None` or `""`) → **mint a new session prefix**. The service
  mints a fresh prefix (subagent: uuid4 hex; normal: encode_snowflake of a
  fresh conversation id) and echoes it in the ack.
- **Concrete** (`"abc123"`) → **continue** that session verbatim. The service
  uses the string as the session prefix directly; for subagents this resumes
  the existing subagent session, for normals it identifies the existing
  conversation.

`normal → subagent` is the primary direction this contract serves. The
LLM-facing tool description simplifies to "leave `invocation_id` blank to
start a new subagent task; pass the previously returned `invocation_id` (from
the ack) to continue that task."

`normal → normal` is not a live path in the star-topology model (only a future
D4 policy evolution).

```
class AgentTarget(ABC):
    has_output: bool
    def resolve_session(parent: SessionInfo, invocation_id_in: str | None)
        -> tuple[SessionInfo, str | None]      # (receiver session, invocation_id to echo)
    def validate_send_from(ctx) -> str | None   # star-topology policy gate; None=ok

class NormalTarget(AgentTarget):
    has_output = False
    # resolve_session:
    #   inv_id_in empty → factory.create(external_id=None) → encode_snowflake(mint fresh)
    #   inv_id_in concrete → factory.create(external_id=inv_id_in) → encode_snowflake(value)
    #   echoes the resulting session_id_prefix
    # validate_send_from: always None — anyone may send to a normal target

class SubagentTarget(AgentTarget):
    has_output = True
    template: AgentTemplate | None        # None = instance already live, skip materialize (D3)
    # resolve_session:
    #   inv_id_in empty → mint uuid4 hex; factory.create_with_prefix(prefix=inv_id, parent=...)
    #   inv_id_in concrete → reuse verbatim; factory.create_with_prefix(prefix=inv_id, parent=...)
    #   echoes that invocation_id in the ack
    # validate_send_from: if source_ctx.comm_kind == SUBAGENT:
    #     parent_name = SessionInfo.from_str(source_ctx.session.parent_session_id).agent_name
    #     if target_name != parent_name: return error  # star topology (subagent→parent only)
    # else None
```

The `SubagentTarget.template` field is populated by `_resolve_target` (D5 step
2): `None` when the subagent instance is already live (continuing a session —
no materialize needed); set to a concrete `AgentTemplate` when the subagent is
not yet registered (cold-start, the Drainer-spawner will call
`template.materialize(...)` on first drain). This `None`-vs-`template`
distinction is what lets the **service trunk be branch-free** on live/cold
state — the service never inspects instance liveness; the Drainer-spawner
does, using the same `SubagentTarget.template` field. **`service.send` does
not call `materialize` under any circumstance** — that is the Drainer-spawner's
role (D3/D5).

Compared to the original D4 wording (which captured "invocation id + has_output
asymmetry"), `comm_kind` and `materializable_template` are no longer fields on
the target — `comm_kind` is implicit in the target type;
`materializable_template` is renamed to `template: AgentTemplate | None`
(per the previous paragraph the caller — `service.send` — sees exactly one
entry point `target.resolve_session(...)` and never observes
`factory.create` vs `factory.create_with_prefix` directly; that internal split
lives inside `AgentTarget` and `SessionIdFactory` keeps both methods as its
documented API (per architecture rule 6 — two named methods carry more meaning
than a dispatched enum). `AgentTarget` owns only `has_output` + addressing
policy + send-side policy — no fork-context, no side effects.

`send` becomes branch-free:

```
target = resolve_target(name)                       # NormalTarget | SubagentTarget(template=...)
if err := target.validate_send_from(ctx): return err
session, inv_id = target.resolve_session(ctx.session, invocation_id_in)
await bus.enqueue_and_signal(str(session), build_message(...))
paths = path_resolver.for_session(str(session)) if target.has_output else None
comm_tracker.record_send(...)
return SendResult(session_id=str(session), invocation_id=inv_id, **paths)
```

(The previous `if envelope.hop_count >= MAX_ENVELOPE_HOPS: return drop_with_log`
line has been removed — see §Deferred: hop-count field is deleted from the
envelope and the gate is removed, since the collapsed design has no forward
middleware path that would ever increment it.)

Future normal→normal / subagent→subagent / cross-pool is a change to
`validate_send_from` policy, not a change to `send`'s trunk — which is the
whole reason the seam survives even after its original asymmetry collapsed.

### D5. Two deep modules + fold the factory into the template: `AgentCommunicationService` (pure router), `WorkspacePathResolver`, and `ContextForkBuilder` — with `AgentTemplate.materialize` replacing `SubagentFactory`

`AgentCommunicationService` contracts to a deep module whose entire body is
**in-memory and side-effect-free except for one enqueue+signal**:

1. `_resolve_source` (sender identity from context)
2. `_resolve_target` → returns an `AgentTarget` (existence check + target
   construction; query order is fixed below)
3. `_validate_invocation_id` (contract check; two-state per D4
   `invocation_id`-two-state-contract)
4. mint/resolve `session_id` via `AgentTarget.resolve_session` (string
   assembly, no disk)
5. build the typed message (Pydantic — Deferred)
6. `bus.enqueue_and_signal` (the single delivery path)
7. `comm_tracker.record_send` (kept in the service — same transaction point)

`_resolve_target` query order is **pool registry first, template registry
fallback**:

```
def _resolve_target(self, name: str) -> AgentTarget | Error:
    instance = self._pool.get(name)
    if instance is not None:
        if instance.descriptor.comm_kind == NORMAL:
            return NormalTarget()
        # subagent already live — continuation, NOT cold-start
        return SubagentTarget(template=None)   # D3: template=None → Drainer-spawner skips materialize
    template = self._templates.get_template(self._pool_name, name)
    if template is not None:
        return SubagentTarget(template=template)   # cold-start path
    return Error(f"target {name!r} not found")
```

This order is what eliminates the old `_send` 260-line god-method's
"template-first-then-fall-through-to-pool-if-already-alive" dirty branch
(`communication.py:967-977`). Service.send needs only `target.template` is
None-or-not — the Drainer-spawner (D3) uses the same field later to decide
materialize-on-drain.

Interface: `send(target, content, invocation_id, context) -> SendResult`.

`SubagentFactory` as an **independent class is removed**. Its only public
surface was `materialize(template, parent_session)`; folding that body into
`AgentTemplate.materialize(parent_session, invocation_id, deps)` removes a
bookmarking class without losing any seam — `AgentTemplate` was already the
right home (templates are the place that knows agent_type + memory config +
tool preset + skill roots). Everything that builds a subagent today —
`_create_dynamic_subagent`, `_build_subagent_tool_manager`, `_wire_subagent_hooks`,
the directory-creation half of `_ensure_invocation`, all workspace-path ctor
arguments — moves into `AgentTemplate.materialize`. `service.send` never calls
`materialize`; only the Drainer-spawner does (D3).

`AgentMaterializeDeps` (a value object of ~12 fields: `agent_factory`,
`broker`, `workspace_manager`, `session_registry`, `output_adapter_factory`,
`comm_tracker`, `safety`, `pool` ref for `register_resident`, the
`on_subagent_created` callback, etc.) replaces the ~30-scattered constructor
parameters that `AgentCommunicationService.__init__` currently takes to
"maybe one day build a subagent". `AgentCommunicationService.__init__` drops
all subagent-construction args; the deps bundle is constructed once by the
pool at wiring time and passed into `AgentTemplate.materialize` per call.

`WorkspacePathResolver` stays (D7): `output_path(session_id)`,
`trace_path(session_id)`, `runtime_dir()`, `memory_dir()`, `pruned_manager()` —
injected into **both** the service (to compute path strings for the ack, when
`target.has_output=True`) and `AgentTemplate.materialize` (to mkdir the
output/trace dirs). Two real consumers still justify the seam (architecture
rule 6). The service stays **zero-FS-write**: it reads `runtime_dir()` and
assembles a path string; it does not `mkdir`.

`ContextForkBuilder` is a **new third deep module** introduced by Round-2
grill. The fork-context block currently living inside
`_create_dynamic_subagent` (two-stage truncate of parent history → optional
lossy governance compaction → XML persistence under
`fork_contexts/{name}_{inv_id}.xml` → cold-injected into the system prompt)
is extracted to `ContextForkBuilder.build(parent_session, agent_type,
invocation_id, fork_max_messages, fork_workspace) -> str | None`. The method
returns XML content; `AgentTemplate.materialize` calls it once when
`context_mode == FORK` and prepends the result to the system prompt. `materialize`
stays single-purpose — it composes an AgentInstance; fork XML becomes one of its
inputs. `ContextForkBuilder` is independently testable and independently
swappable (future governance lossy-vs-lossless fork strategy switches happen
there, not inside materialize).

`ContextForkBuilder` owns the **fork-file registry** (`dict[session_id, Path]`,
today's `_FORK_FILE_REGISTRY` in `communication.py:47`). The registry moves
with the construction: `build(...)` registers the file when it persists
the XML; `cleanup(session_id)` removes the entry and unlinks the file when
the pool evicts the session. `AgentPool._evict_dynamic_session` calls
`ContextForkBuilder.cleanup(session_id)` instead of the current
`cleanup_fork_context(session_id)` (which lives in `communication.py` today
and is deleted when `_create_dynamic_subagent` is folded into `materialize`).
The registry is a private module-level dict inside `ContextForkBuilder`
(same pattern as today, just relocated) — it is not on the deep module's
interface, but the **two public methods** (`build`, `cleanup`) cover every
caller.

**Considered alternatives (round 2 grill)**: the original draft of ADR-0015
proposed three independent classes — `SubagentFactory`,
`WorkspacePathResolver`, and a deeply decoupled `AgentCommunicationService`.
Round-2 grill found that `SubagentFactory`'s only public method
(`materialize(template, parent_session)`) was operationally identical to what
`AgentTemplate.materialize` would naturally be — the template carries every
fact the factory needed (agent_type, memory config, tool preset, skill roots,
context mode, fork_max_messages), and the deps bundle is constructed at pool
wiring time regardless. Preserving a separate class added wiring cost without
adding a real seam. Folding the factory's body into `AgentTemplate.materialize`
collapses "three independent deep modules" into "two deep modules
(`WorkspacePathResolver`, `ContextForkBuilder`) + `AgentTemplate.materialize`
holding the construction body", and `AgentCommunicationService` becomes the
pure router with no hop_count-gate step.

#### hop-count field is deleted (Round-3 grill)

`MAX_ENVELOPE_HOPS`, `AgentMessageEnvelope.hop_count`, and the associated
send-side gate / consumer-side drop blocks are **removed**. The collapsed
design has no envelope forward middleware: envelopes are minted fresh by
`service.send` (no hop_count incrementing on the way) and consumed locally
by the Drainer-spawner. A future forwarding path that does need hop-count
tracking can re-add the field; until then the gate is dead code that
misleads readers. (§Context item 5 still indicts the broader metadata
dict round-trip; Pydantic-ization of the envelope remains in §Deferred.)

### D6. Delete the dispatch path per-session execution lock; spawn/retire use a tiny atomic gate

The claim "delete the per-session `asyncio.Lock`" only stands up once D9's
`SessionInputQueue` is in place: **every turn-start provocation goes through
the Drainer**, so intra-session concurrency is excluded structurally by the
"single consumer" shape of the Drainer — a dispatch-path execution lock that
queues turns is no longer needed.

What replaces it is `_session_gates: dict[session_id, asyncio.Lock]` — a
**tiny, await-free** pool-level atomic gate held only at the two boundaries of
the Drainer's lifecycle (spawn and retire):

```
# enqueue (external)
async with self._session_gates[sid]:
    drainer = self._drainers.get(sid)
    if drainer is None or drainer.done():
        self._drainers[sid] = asyncio.create_task(self._drainer_loop(sid))
    self._queues[sid].enqueue(provocation)   # sync, O(1)

# Drainer retire
async with self._session_gates[sid]:
    if self._queues[sid].is_empty() and inbox_count(sid) == 0:
        self._drainers.pop(sid, None)
        return                                  # self-cancel from inside
    # otherwise continue draining
```

While the gate is held there is no `await` on a turn, no `await` on IO — it
only touches dict/queue/counts. This is the exact inverse of the
§"Context what is wrong" item 2 bug: the old lock waited across an entire
dispatch + watchdog; the gate only protects dict consistency between
spawn/retire and enqueue. The old D2 failure mode — "a queued dispatch is
cancelled by the watchdog before it acquires the lock, but the message was
already polled out of the inbox" — cannot occur: enqueue is synchronous, with
no window to be cancelled into.

The Drainer retires **immediately** when both the queue and the inbox are empty
(no idle hold time), because any later provocation will simply re-spawn it via
the gate. There is no value in keeping an idle Drainer alive for "spawn-cost
savings"; spawn is cheap.

The output-existence collision-avoidance in `_ensure_invocation`
(`communication.py:379-390`) is deleted. New subagent tasks always mint a fresh
invocation id; a concrete passed id is the explicit "resume this session"
intent and must not be diverted.

### D7. The send ack keeps announcing trace/output paths

Unlike the path-announce-and-drop variant considered, the ack **keeps**
returning `trace_dir` / `output_path` (per user). This is exactly why
`WorkspacePathResolver` is shared between service and `AgentTemplate.materialize`
rather than private to one of them: the service needs the resolved path strings
for the ack (when `AgentTarget.has_output=True`), and `materialize` needs them
to create the output/trace directories. The service computes strings only; it
does not create files.

Note that for normals today `has_output=False`, so the service side does not
read the resolver — but `materialize` still creates the trace directory for the
agent's own TraceCollectorHook, and the round-2 eager-materialize-at-boot rule
(D3) means normals always have their dirs ready. The two-consumer justification
is therefore carried by the subagent direction (service ack + materialize
mkdir) and by the normal direction's `materialize` forming the second consumer
on its own. Preserving the seam also leaves the ack opt-in future (normals may
gain `has_output=True` later) wire-ready.

### D8. Subagent→normal goes through the same `send`

`SubagentAutoSendHook` no longer hand-builds an envelope and calls `bus.send`.
It is injected with the `AgentCommunicationService` and calls
`await service.send(target=parent, content=<result>, invocation_id=...,
context=ctx)`. All directions now use one call site (the original "four
directions" wording is dropped — normal→normal is not a live path in the
star-topology model, only a hypothetical D4 policy evolution).

The subagent's reply-to-parent asymmetric restriction is **not** enforced by
the service trunk; it is enforced by `SubagentTarget.validate_send_from(ctx)`
(D4): a subagent sender may only target the agent named by
`SessionInfo.from_str(source_ctx.session.parent_session_id).agent_name`.
Normal senders face no restriction from this gate. The service trunk stays
branch-free on `comm_kind`; only the policy hook branches.

### D9. `SessionInputQueue` is the single turn-start dispatcher on a session; broker DMs are enqueued too

The per-session execution lock that D6 wants to delete hides a seam that
ADR-0015 did not make explicit: today `_dispatch_raw_broker_message` lets a
human/external broker DM **bypass the inbox** and go straight to
`pipeline.process_message`, sharing the session with inbox-driven turns
without an invariant mediating them. Once the lock is gone those two routes
would race on the same session's history/turn-state/memory.

The "single-flight Drainer" of D1 implicitly assumes there is one turn-start
source per session. A new abstraction makes that assumption explicit:
**`SessionInputQueue[S]`** is a per-session FIFO whose entry type is
`TurnProvocation`:

```python
class TurnProvocation(ABC): ...
class InboxReady(TurnProvocation): ...      # level marker, no payload
class ExternalInput(TurnProvocation):
    message: InputMessage                   # human / WebUI / approval_decision etc.
```

- `SessionInputQueue` is the Drainer's only data source; the Drainer is its
  only consumer.
- **`InboxReady` is a level marker, not a snapshot.** When the Drainer
  dequeues one it calls `inbox.consume(S, limit=N)` to take a batch right
  then: non-empty batch → run one turn; empty batch (already folded into the
  running turn by `InboxFlushHook`) → no-op, do not run an empty turn.
  Multiple messages landing on an idle session each enqueue a single
  `InboxReady`; one `consume(limit=N)` takes them all; one turn. ADR D1's
  batching semantics are preserved.
- `ExternalInput` carries an `InputMessage`-like object (reusing the existing
  metadata fields of ADR-0012 `approval_decision`, ADR-0013
  `attachments_resolved`, etc. — no new variant is introduced).
- `message_type` values (`task_request` / `subagent_result` / `agent_message`)
  are **not** variants of `TurnProvocation`; they are fields of the inbox
  envelope. After `inbox.consume` the Drainer constructs an `InputMessage`
  from the envelope and hands it to `pipeline.process_message`.

**The framework exposes a proper entry point** to business:
`AgentPool.submit_external_input(session_id, InputMessage)` synchronously
enqueues an `ExternalInput` on the session's `SessionInputQueue` and follows
the spawn protocol to ensure a Drainer is running. `PoolRouter` calls it
directly, **no longer going through `broker.send_to` to deliver a DM**.
`AgentPool._dispatch_raw_broker_message`'s fallback (a broker message that
fails to parse as an envelope) becomes a synonymous enqueue of
`ExternalInput`, kept as a safety net for framework tests / direct-connect
scenarios that bypass `PoolRouter`. As a result, the broker **degrades to a
pure inter-agent wakeup channel** and no longer mixes in human DM payloads.

`SessionInputQueue` is **unbounded** — the inbox server is already
on-disk-backed so message count has a ceiling, and `ExternalInput` arrives at
most one per user action, so there is no backpressure need; omitting a cap
avoids an extra error path.

Cross-process human DM delivery is out of ADR-0015 scope (today's deployment
has no cross-process); a follow-up placeholder is recorded in §Deferred.

## Lost-wakeup and lost-wakeup recovery

Three defenses, in order:

1. **`event.clear()` before `consume`**, and the **drain-to-empty inner `while
   inbox.count(S) > 0` loop** close the classic "message arrives between
   clear and consume" gap — anything that lands after the clear but before the
   inner loop's next check is still drained this turn.
2. **Single-flight** guarantees at most one Drainer per session, so there is no
   "two drainers both cleared and both missed".
3. The existing 10s `_poll_inbox_for_idle_agents` remains as a **final
   backstop** — but it now actually works, because messages are never eagerly
   polled out of the inbox. If the event signal is ever lost, the poller sees
   real `has_pending` and re-arms the Drainer. Its role is downgraded from
   "rescue dropped messages" to "rescue a lost wakeup signal".
4. **The `SessionInputQueue` enqueue is synchronous**, so there is no "event
   was cleared but the message is not yet persisted" race — the small window
   that pure-`asyncio.Event` paths can leave for fold-in and drain to mutually
   miss. The `_session_gates` tiny lock eliminates double-spawn / miss-spawn
   between enqueue and spawn/retire.
5. **`InboxFlushHook.before_iteration` and the Drainer's drain share the same
   `inbox.consume(S, limit=N)` primitive**, and are naturally serialized (the
   Drainer does not dequeue the next provocation while a turn is running).
   Anything fold-in did not finish is picked up by the Drainer's next
   `InboxReady` dequeue via the leftover `consume`.

## What does NOT change

- Star topology and the "subagents cannot spawn subagents" enforcement stay
  (now expressed as `AgentTarget.validate_send_from` policy).
- `SessionIdFactory` and the `{prefix}.{agent_name}` format are unchanged. For
  normal, `prefix = encode_snowflake(conversation id)` and no invocation id is
  echoed; for subagent, `prefix = invocation_id` verbatim and that id is echoed.
  (`tools.py` already documents this correctly; the stale `AGENTS.md`
  colon-and-three-segment format is corrected.)
- `CommunicationTracker` (`record_send`/`acknowledge`) stays in the service at
  the enqueue transaction point. Sideband bracket-matching is unchanged.
- `InboxServer`/`InboxProducer`/`InboxConsumer` storage and dedup semantics are
  unchanged.
- `MessageBroker` retains its role as the **pure inter-agent wakeup channel**.
  Before ADR-0015, `_dispatch_raw_broker_message` also ferried human DMs; per
  D9, human DMs now go through `AgentPool.submit_external_input` straight into
  the `SessionInputQueue`, and the broker no longer mixes DM payloads. The
  broker's former `_inbox_wakeup` signal (emitted from
  `LocalAgentMessageBus.send`) was **removed** in the event-driven poller
  refactor: between-turn wakeup is now an in-process `asyncio.Event` on
  `InboxPoller`, signalled directly from `LocalAgentMessageBus.send` (the single
  convergence point of all inbox writers). The broker retains only its
  cross-pool peer-routing role (ADR-0019 `bus_ref`); within one process the
  `SessionInputQueue` is taken directly.

#### Drainer-spawner materialize-on-drain protocol (D3 details)

The Drainer-spawner is the **single** routine that decides whether to call
`AgentTemplate.materialize` for a given session. Its contract:

```
async def _run_drain_step(session_id, agent_name, session, provocation):
    instance = self._pool.get(agent_name)
    if instance is None or instance.pipeline is None:
        # The provocation arrived but no live instance. Need materialize.
        template = self._templates.get_template(self._pool_name, agent_name)
        if template is None:
            logger.error("Drainer for %s: no template, dropping provocation", session_id)
            return     # rare: target_name was valid at send time but template registry empty
        try:
            instance = await template.materialize(parent_session=session.parent_session_id,
                                                    invocation_id=provocation_invocation_id(session_id),
                                                    deps=self._materialize_deps)
        except Exception:
            logger.exception("Materialize failed for %s; message stays in inbox", session_id)
            return     # drain cycle aborts; message stays in inbox; backoff retries
    # instance is now live; run the turn
    ...
```

The `SubagentTarget.template` field populated by `_resolve_target` (D5 step 2)
is **informational for the service trunk**, not authoritative for the spawner:
the service may have minted a `SubagentTarget(template=...)` cold-start, but by
the time the Drainer runs the pool may have registered the subagent via some
other path (rare but possible — e.g. boot-time eager register of a subagent
template). The spawner's `pool.get(name)` lookup is the authoritative
"need materialize?" check.

**`SubagentTarget.template = None`** (D4 / D5 step 2 pool-registry-hit case)
**does not** instruct the spawner to skip materialize; it is the service's
hint that "at send time the instance was already alive". The spawner still
re-checks `pool.get(name)`. The same applies symmetrically: a target minted
with `template=concrete_Template` may turn out to have a live instance by
drain time — the spawner's `pool.get(name)` then returns the live instance
and skips materialize. In short, **`SubagentTarget.template` is a hint that
helps the service avoid deciding "is this a new session prefix or continue?"
in the trunk**; the spawner's `pool.get(name)` is the operational truth.

#### `invocation_id` is recoverable from `session_id`

The Drainer-spawner's `template.materialize(parent_session, invocation_id, deps)`
call must pass the same `invocation_id` the service minted at send time —
otherwise the subagent instance would be built under one prefix while the
inbox entry sitting under `session_id` was built from a different one, and
the freshly materialized subagent would have no session to consume from.

The invariant: `AgentCommunicationService` builds the receiver's session via
`SessionIdFactory.create_with_prefix(agent_name=target_agent,
prefix=invocation_id, parent_session_id=parent_sid)`, producing a session_id
of the form `{invocation_id}.{agent_name}`. Therefore the `invocation_id` the
spawner needs is recoverable from the session_id by `session_id_prefix_of`
— the prefix segment IS the `invocation_id` verbatim for subagents
(and the encoded conversation id for normals, where `invocation_id` is the
argument used at send time). The spawner reads
`SessionInfo.from_str(session_id).session_id_prefix` (or, for subagents,
simply the segment before the first `.`) and passes that back as
`materialize`'s `invocation_id`. No separate registry or hint is required.

This is why `service.send`'s `SubagentTarget.resolve_session` is allowed to
be the sole point where `create_with_prefix` runs: the resulting session_id
string is self-describing, and any downstream consumer (Drainer-spawner,
follower turns) recovers the prefix without needing a side-channel store.

## Consequences

**Positive.**

- The two-path race is gone — one consumer per session. Mid-turn messages are
  folded into the running turn as intended.
- The "queued dispatch cancelled while waiting for the lock → message dropped"
  bug is gone: there is no lock to wait for, and `consume` is adjacent to
  `run_turn`.
- Inbox batching works: N messages to an idle session = one turn.
- `_send`'s 260 lines collapse into a branch-free `send`; the remaining
  asymmetry is only `AgentTarget.has_output` plus `validate_send_from` as a
  policy dispatch point. Round-2 grill removed `comm_kind` and
  `materializable_template` from the target (D4) — they survive only as
  internal `NormalTarget` vs `SubagentTarget` selection and the agent's
  materialize-timing (D3).
- `AgentCommunicationService` becomes a deep module (router only); subagent
  construction moves to **`AgentTemplate.materialize`** (the original
  `SubagentFactory` is folded away); path logic moves to
  `WorkspacePathResolver`; fork-context construction moves to
  `ContextForkBuilder` (D5).
- Normal and subagent instances share one construction path —
  `AgentTemplate.materialize` (D3 round-2: the `AgentDescriptor.materialize`
  name from the previous draft is retracted; `AgentDescriptor` stays a
  frozen data record, `AgentTemplate.materialize` is the sole body that
  produces instances). Only the **timing** differs — eager at boot for
  normals, lazy on first drain for subagents (D3). Round-1 draft's
  "SubagentFactory" class is removed.
- The messaging directions share one call site (`SubagentAutoSendHook`
  included); the subagent→parent restriction lives in
  `SubagentTarget.validate_send_from` (D4) rather than in the `send` trunk.

**Negative / accepted.**

- The pool now owns Drainer lifecycle per session (start/stop/restart-on-crash);
  a crashed Drainer must be restarted by the pool (the existing consumer-restart
  machinery is the template for this).
- Materialize-on-first-drain adds a small cold-start latency before a
  subagent's first turn — acceptable, and it centralizes creation-failure retry.
- Normals eager-materialize **at boot** (D3 round-2): the user has confirmed all
  main agents must be live at startup, so the lazy path's cold-start win does not
  apply to them. This means start-up cost is paid up-front for normals, and
  business wiring must construct the `AgentDescriptor` + call
  `descriptor.materialize(...)` early in pool bring-up. A faithful YAML template
  for each normal lives under `config/pools/*/templates/` only as a fallback.
- `WorkspacePathResolver` is a deep module with two real consumers (service ack
  when `has_output=True` + `AgentTemplate.materialize` mkdir); it is one more
  thing to wire. `ContextForkBuilder` is a second new module (D5 round-2),
  replacing the inlined fork block; the construction logic dimension grows by
  one explicit unit but the inline tangle shrinks correspondingly.
- `AgentMaterializeDeps` is a new value object (~12 fields) — it replaces the
  ~30 scattered constructor parameters that `AgentCommunicationService.__init__`
  currently takes to tentatively support subagent construction; net code-line
  reduction, but it is a new type to learn.
- There is one more object `SessionInputQueue[S]` plus the `TurnProvocation`
  abstraction (D9); it is what makes D6's "delete the lock" actually stand up,
  so it is the cost of D6 rather than a pure subtraction.
- `_session_gates` is still a per-session `asyncio.Lock`, but it **only guards
  the atomicity of dict operations**, with hold time in the microsecond range
  and no `await` inside. ADR-0015 §"Context what is wrong" indicts "the lock
  waits across an entire dispatch + is cancelled by the watchdog", not "any
  lock for any concurrency is wrong".
- `AgentTarget` survives as a featherweight seam (D4 round-2) even though its
  original justification collapsed; this is a deliberate extension point for
  future policy changes — a reader who expects a target object with rich
  state will be surprised. The surprise is documented in D4 and is the cost
  of keeping `send`'s trunk immune to policy evolution.
- **`AgentCommunicationService.__init__` migration breaks 7+ test call sites.**
  D5's "`__init__` drops all subagent-construction args" means every
  instantiation site that passes `safety` / `pool_llm_model` /
  `inbox_consumer` / `notification_service` / `runtime_dir` /
  `workspace_manager` / `root_provider` / `target_store` / `on_subagent_created`
  etc. must be updated. The migration is mechanical (search-and-replace
  across `tests/unit/multi_agent/test_communication_service.py`,
  `tests/unit/multi_agent/test_dynamic_subagent_integration.py`,
  `tests/unit/multi_agent/test_subagent_v2_integration.py`,
  `tests/unit/multi_agent/test_subagent_auto_send_hook.py`,
  `tests/unit/multi_agent/test_send_to_agent_tools.py`,
  `tests/integration/multi_agent/test_send_to_agent_subagent_e2e.py`, and the
  production wiring in `examples/bot_project/bot/service/pool_builder.py` /
  `examples/bot_project/bot/workspace/wiring.py`). The implementation plan
  bundles the search-and-replace and the test rewrites into the same PR —
  one atomic landing, no shim period, no `**kwargs` deprecation. The
  framework's git history shows this pattern has worked for prior
  constructor-shape evolution (e.g. `AgentFactory.create_agent`); the same
  pattern applies here.

## Deferred (tracked, not decided here)

- **Pydantic message types.** `InboxMessage`, `AgentMessageEnvelope`, the
  payload, and `message_type` (to an enum) move to frozen `BaseModel`; broker
  serialization goes through `model_dump_json` / `model_validate_json`. This is
  a self-contained follow-up that does not change the model above.
- **XML render relocation.** `build_agent_comm_message` wrapping content into XML
  moves from send-side to receive-side rendering in `InboxFlushHook`: the
  payload carries structured fields, and the model-facing text is rendered at
  injection time. Wire format and presentation decouple; pairs with the
  Pydantic move.
- **Prompt changes.** The `send_to_agent` tool/prompt text is out of scope;
  this ADR is about the implementation, not the LLM-facing description.
- **Cross-process human DM delivery.** D9 reroutes the broker DM path into
  `SessionInputQueue`; today's deployment has no cross-process human DMs. Once
  a cross-process runtime exists, an IPC is needed to deliver `ExternalInput`
  to the owning `SessionInputQueue`; left as a follow-up, not in this ADR's
  scope.

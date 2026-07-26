# modexctl Control Plane Glossary

This glossary records design language for the modexctl control plane.
Product-domain terms also live in `examples/bot_project/CONTEXT.md`.
Terms from the prior direct-CLI approach (archived) are retained where
they remain relevant to the current implementation.

## Control Client

The bot-owned `modexctl` command-line program under
`examples/bot_project`. It validates its invocation context, selects
local or online behavior, calls the bot's supported control interface
when required, and renders the response.

The Control Client does not own routing, session lifecycle, message
projection, persistence schema, or live runtime-control semantics.

## Bot Control Interface

The single application interface that owns externally supported target
discovery, messaging, and history behavior. WebUI and Control Client
transports invoke the same interface rather than maintaining parallel
interpretations.

## Control Transport

An adapter around the Bot Control Interface. It parses and validates a
wire request, invokes the shared interface, and serializes the result.
HTTP is one Control Transport; a future local IPC transport would be
another adapter rather than another implementation of bot behavior.

## Bootstrap Context

The `MODEX_*` environment values injected into an agent process. They
let the Control Client locate the bot and form a request for the
current agent, session, workspace, and topology. The client validates
their structure and places operation inputs in JSON request bodies
rather than large URL query strings.

Authentication of this context is deliberately outside the first
core-capability phase.

## ModexCtlContext

A Pydantic `BaseModel` that serves as the single env-var interpretation
point in the CLI. It resolves the caller's session id, agent name, comm
kind, parent session id, workspace root, control origin, pool map, and
targets snapshot. Commands consume the context rather than reading env
vars directly. Provides smart defaults per normal/subagent mode (D25).

## Control Origin

The scheme, loopback host, and port of the bot's shared HTTP listener.
It is injected without an API path. WebUI and Control Transports use
the same origin. Injected as `MODEX_CONTROL_ORIGIN` through
`ExternalEnvBuilder.build_modex_vars()` for external agents and through
`AgentMaterializeDeps` for native subagents (D6).

## Control Workspace

The explicit `workspace_root` supplied to each online control
operation. It selects one multi-live workspace's
`PoolWorkspaceResources`; session, pool, and agent identifiers are
interpreted only inside that workspace.

## Legacy Reference Implementation

The existing framework-package `src/modexctl` source retained during
development as behavioral and test evidence. It is not the production
fallback for the new Control Client.

## Runtime Fallback

Automatic use of an alternate implementation after the primary path
fails. The Control Client does not fall back to the Legacy Reference
Implementation or direct SQLite access when the bot control interface
cannot be reached.

## Invocation Continuation

The `modexctl send --invocation-id <id>` behavior for continuing a
same-pool subagent task session. It is unrelated to runtime
pause/resume.

## invocation_id (subagent)

The 8-byte hex prefix portion of a subagent's `session_id`. The full
subagent session_id is `{invocation_id}.{agent_name}` (e.g.
`f3a9c1d2.researcher`). The invocation_id is minted by the framework
when the caller omits it, or reused when the caller passes a non-empty
value.

`modexctl send --invocation-id` mirrors this. `modexctl history
--invocation-id` identifies the exact subagent session (optional for
self-history in the current implementation).

## History Session Address

The complete `session_id` accepted by the bot history interface. The
CLI converts `--invocation-id <prefix> --agent <name>` to the canonical
`<prefix>.<name>` address before making the request. For self-history,
`--agent` and `--invocation-id` are optional and the caller's own
session id is used (D19).

## History Target Authorization

The bot-side check that a caller may only read its own session history
or the history of a subagent registered under the caller's session.
Unauthorized reads return `403 forbidden_target` (D26).

## Dispatch Outcome

An enum describing how an optional Invocation Continuation was
resolved: `new_task`, `resumed`, `requested_invocation_not_found`, or
`not_applicable`. In the not-found case, the structured result also
carries the newly minted invocation id and the originally requested id.

## CLI Compatibility Surface

The full observable behavior of the existing client: dynamic command
availability, arguments and options, exit codes, stdout/stderr text,
JSONL fields and ordering, routing outcomes, and Invocation
Continuation semantics. The Phase 4 redesign (D5) intentionally changed
parts of this surface (output vocabulary, history gate) for agent
ergonomics.

## Domain Route Package

A bot package containing one exposed domain's typed request/response
models, application interface, and thin HTTP route adapter. The root
server composes these packages. The first package covers the Control
Client slice; later WebUI domains migrate through the same seam rather
than forming another architecture.

## Server Projection

The typed, filtered representation exposed by the bot control endpoint.
It protects internal storage/runtime models and defines the HTTP
contract. Eight fields: `role`, `content`, `tool_calls`,
`tool_call_id`, `tool_name`, `name`, `created_at`, `message_id`.

## Observable History

The provider-neutral history that the bot can expose for inspection.
Native agents use MessageStore records. External coding agents use
materialized canonical transcript events. External Observable History
is not the provider-owned session memory used to resume Pi or
OpenCode.

## Source Fidelity

The rule that Observable History represents only data present in the
selected MessageStore or materialized transcript source. Projection
does not backfill missing facts from inboxes, current requests,
provider-private storage, or synthetic defaults.

## Client Output Projection

The Control Client's separate allowlist and serialization rules for
agent-facing output. It is independently versioned and tested even
when its current fields happen to match the Server Projection.

## VO (View Object) whitelist

The fixed set of eight fields the history output exposes to the agent,
filtering out internal markers (`_deleted`, `_pinned`, `token_count`,
`is_content_json`, `content_format`, `reasoning_content`). Both the
Server Projection and the Client Output Projection use this whitelist.

## true history

A history query that includes soft-deleted messages
(`state='soft_deleted'` in the SQLite `memory_session_messages` table).
This contrasts with `MessageStore.load_messages` (the LLM-context path)
which filters to `state IN ('normal', 'pinned')`. The control facade's
`load_all_messages()` includes soft-deleted records.

## Public Command Directory

The resolved directory containing product-owned `modexbot` and
`modexctl` launchers. Its physical location depends on install mode:
packaged Windows uses `<install>/commands` (deferred); editable/wheel
installs use the active Python environment's normal `Scripts` or `bin`
directory. First implementation continues using
`<install>/python/Scripts/`.

## Private Tool Directory

The platform-specific bundled-helper directory, such as
`<install>/bin/windows` for `rg.exe`. It is available to the bot and
child processes but is not registered as a public user PATH entry.

## Public Command Ownership

The installed `modexctl` command name is owned by
`examples/bot_project`. During migration, the legacy source may remain
in the repository, but it does not share or compete for the public
command name.

## Import Isolation

The constraint that `bot/control/__init__.py` must not re-export
server-side components, so the CLI's import graph does not pull in the
full `modex_agent` framework (D28). Verified by a regression test.

## Cross-references

- **ADR-0022** — modexctl origin, `InboxMQ.deliver()` cross-process
  pattern.
- **ADR-0019** — peer prefix-reuse rule (determines the peer-normal
  target session_id that `modexctl send` does not expose).
- **ADR-0023** — hybrid persistence (SQLite is the bot's default).
- **ADR-0028** — RecordScope base/subclass split.
- **ADR-0029** — epoch-millisecond timestamps.
- **ADR-0030** — ColumnProjection.

<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-09-09 -->

# acp

## Purpose
ACP (Agent Client Protocol) agent-server surface — exposes a ModexAgent agent pool to code
editors (Zed, JetBrains) over JSON-RPC stdio. This package is the **business-agnostic protocol
mapping layer**; the project-bound backend and pool assembly live in the bot
(`examples/bot_project/bot/acp/`, surfaced as `modexbot acp`). See ADR-0049 and
`docs/design/acp-adapter/DESIGN.md` (single-project contract: one process, one IDE project
workspace, one selected pool, many sessions).

ACP's process model is the editor-spawned stdio subprocess, which is non-isomorphic to the
resident bot_service's channel registry — so this surface does NOT enter `@register`. Requires
the optional `acp` extra (`agent-client-protocol`, pinned — see ADR-0049 §6 for the drift policy).

## Key Files
| File | Description |
|------|-------------|
| `backend.py` | The session seam (DESIGN.md §8, SDK-free ABCs): `AcpSessionBackend` (`supports_load` / `open(AcpOpenRequest)` / `close`), `AcpSessionHandle` (`session_id` / `prompt` / `cancel` / `read_history` / `close`), `AcpInteraction` (`emit` raw `TurnEvent`s + `request_decision` — the permission ABC lives here directly, no separate sink). The bot supplies the production backend; `scripted.py` is the framework one. No configure/mode/model stubs — later capabilities extend this same seam. |
| `types.py` | SDK-free frozen Pydantic value objects: `AcpOpenRequest` (`kind`/`cwd`/`session_id`), `AcpPromptInput`, `AcpBackendError(Code)` (typed rejections: invalid_cwd / project_mismatch / boot_failed / not_found / unsupported / busy), `AcpSessionMode` (default only), `AcpPermissionOption` (once-only: allow_once / reject_once), `DEFAULT_PERMISSION_OPTIONS` (the shared once-only pair — prompt default and scripted/driver offer), `PermissionPrompt`, `PermissionChoice`. |
| `events_map.py` | Pure mappers (the test surface): `TurnEvent` → ACP `session/update` models, `infer_tool_kind`, `tool_call_id_for` (`{turn_id}:{call_id}`), total `map_stop_reason` table, `map_history_replay` (typed `ChatMessage` facts → load/replay updates; real source facts only, nothing fabricated). Branches on the `kind` Literal, never isinstance. |
| `server.py` | `ModexAcpAgent` structurally implementing `acp.Agent` (the sanctioned rule-7 Protocol exception, confined here): `initialize` (static, never boots), `new_session`/`load_session` (cwd forwarded to the backend; load reads + replays history before publishing), `prompt` (per-session busy gate, no queueing), `cancel` (prompt → handle; load/replay → the load RPC fails, never a prompt stop reason), `aclose` (cooperative drain). Permission round-trip rejects client answers outside the offered options (`RequestError` invalid_params — never 500, never silent allow); `cancelled` outcome maps to `PermissionChoice(option=None)`. |
| `entry.py` / `__main__.py` | stdio entry `main(backend)`; the async serve's `finally` drains the agent (prompts via handles, load operations) then awaits `backend.close()` exactly once. stdout is protocol-owned, logs go to stderr. Runnable as `python -m modex_agent.acp`. |
| `scripted.py` | `ScriptedAcpBackend` — deterministic demo/smoke backend on the same seam (echo / approve / wait directives; `supports_load=False`) for the bare entry without an LLM. |

## Design Rules
- **`acp.Agent` is the only Protocol in framework code** (rule-7 exception at the SDK boundary);
  implemented by structural subtyping, never inherited. All internal seams are ABCs (rule 4/7).
- **`acp.schema` wire types stay inside `server.py`/`entry.py`/`events_map.py`** (the mapping
  layer); everything else consumes `types.py` value objects. This is the SDK-drift firewall.
- Import-light: importing `modex_agent.acp` must not require the SDK — `__init__.py` loads
  SDK-touching submodules (`server`/`entry`/`events_map`) lazily; `types`/`backend`/
  `scripted` are SDK-free.
- The framework never imports the bot; the bot never imports `acp.schema`. SDK imports live only
  in the protocol/mapping modules (T08 wiring rule).
- Busy is an error, never a queue: one in-flight prompt or load per session (DESIGN.md §8).
  Cancel answers the pending prompt with `stop_reason="cancelled"` via the handle's cooperative
  contract — turn tasks are awaited, never hard-killed (convergence rule 3). This includes a
  prompt suspended on a pending permission request: the handle's cancel/close must end it —
  cancellation is never reported as a denial (no fabricated execution facts). The client
  *dismissing* the permission card is different: it arrives as `PermissionChoice(option=None)`
  and is a real denial outcome (DESIGN.md §6.3), completing the turn normally.
- Approvals reuse the existing suspend/resume loop; ACP is a parallel third front-end, not a
  branch. The ACP layer models one permission round-trip (`PermissionPrompt` →
  `PermissionChoice`), not approval batches — batch atomicity (ADR-0011) belongs to the
  framework's `ApprovalTransaction` inside the pool's resume loop; the option surface is
  once-only (`DEFAULT_PERMISSION_OPTIONS` in `types.py`). Load replay streams real stored facts
  only (`map_history_replay`) — no LLM, no store access from the framework, no fabricated
  content (DESIGN.md §5.3).

## Testing
`tests/acp/` — pure-function coverage of `events_map` + seam contracts (`test_backend_seam`),
permission value-object contracts (`test_permission_types`), server behavior against fake
backend/connection (`test_server`), entry lifecycle with a stubbed `run_agent` (`test_entry`),
scripted backend (`test_scripted`), and an e2e turn test that spawns a real stdio subprocess of
`python -m modex_agent.acp` and drives it with the SDK client (`test_e2e_turn`). The e2e is the
regression guard for SDK version bumps.

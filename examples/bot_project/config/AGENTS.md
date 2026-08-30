<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-28 | capability-bundles doc sync (ADR-0047) -->

# config

Runtime configuration files for the bot: main config, IM adapter credentials, model definitions, MCP server registry, and the scope declaration (the single source of pool/agent assembly). YAML/JSON with `${ENV_VAR}` interpolation. The **typed config code** (Pydantic domain schemas, stores, controllers) lives in `bot/config/` (a separate Python package), not here — this directory holds only data files.

## Key Files

| File | Description |
|------|-------------|
| `bot_config.yml` | Main config — runtime safety, observability, persistence defaults. (Pool/agent composition no longer lives here; see `scopes/`) |
| `im.yml` | IM adapter credentials — one top-level section per platform (`qq`, `telegram`). Gitignored (contains secrets). Each adapter reads only its own section. Configured via the WebUI Settings → IM tab (no template shipped) |
| `model.yml` | Model definitions — the single source of truth: `default_provider` / `default_model` + a per-provider model list. Shared across all pools. Bootstrapped by the `modexbot config` wizard (no template shipped) |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `scopes/` | Scope declarations (see below) |
| `graphs/` | Declarative graph specifications (DAG workflows) — loaded by `GraphSpecLoader` at startup; agent nodes reference (pool, agent) pairs cross-checked against the declaration at boot (V10) |
| `mcp/` | MCP server registry (see below) |

## scopes/ Structure

The **scope declaration** (ADR-0042) is the single source of truth for pool/agent assembly — the legacy `config/pools/<name>/pool.yml` + `templates/*.yml` roster is deleted (ticket 11). One primary file, plus runtime-created workspace declarations:

```
scopes/
├── bot.yml                    # the workspace declaration — resource selection + all pools
└── workspaces/                # WebUI runtime-created workspaces (ticket 17), one file each:
    └── <name>.yml             #   file STEM = workspace identity; restart-persistent authority
```

`bot.yml` shape — a `workspace:` root carrying resource selection (`persistence:` memory backend, `paths:` data-dir layout, `mcp:` shared server-name set; every field `None` = inherit the service-level domain config) and the pool trees. Each pool is a name key with optional `peers:` (cross-pool links, bidirectional, same-workspace) and an `agents:` mapping; agent names are mapping keys, nesting `agents:` under an agent is sugar for the flat `parent` model. A pool-as-root declaration (root key `pool:` instead of `workspace:`) boots the single-home stack.

Position-derived defaults (SPEC §3.2) are NOT transcribed — only deviations declare: `toolset` (root → `full`, non-root → `read_write`), `eager`, `memory` (`archive_enabled`/`core_enabled`/`session.max_context_tokens`), `approval` (root-only), `execution_strategy` + `provider_kind` (external pools), `hooks` (with `+`/`-` merge prefixes), `capabilities` (override map, ADR-0047 — `false` forces a capability off, a config mapping forces it on: the shipped declaration enables `todo`/`experience`/`aci`/`ast_grep` per agent this way), `tools`, plus the roster face (`llm_provider`, `system_prompt`/`system_prompt_provider`, `memory_system`, `interceptors`, `commands`) resolved through the 11-slot `ComponentRegistry`. The full field face is `AgentSpec`/`WorkspaceSpec` in `modex_agent/scope/spec.py` (see `src/modex_agent/scope/AGENTS.md`).

Editing: by hand, or via the WebUI Settings → Scope tab (tree canvas + provenance bill; writes back through `PUT /api/scope/declaration`, restart-effective).

## mcp/ Structure

A single MCP server registry (not per-agent files):

```
mcp/
├── registry.json            # global MCP server registry (stdio/SSE/streamable_http)
└── registry.example.json    # template
```

Agents select which registered servers they see via their `mcp:` list (agent level) narrowed by the workspace's `mcp:` set (workspace level); the WebUI **MCP** tab edits `registry.json`. MCP JSON uses `command`/`args` (stdio) or `url` + `headers` (SSE/streamable_http).

## For AI Agents

### Working In This Directory
- These are **data files**; the code that loads/validates them lives in `bot/config/` (`domain.py`, `domains/im.py`, `domains/model.py`, `mcp_registry.py`, `skills_store.py`, …). The scope declaration is loaded/validated/compiled by `modex_agent/scope/` (boot wiring: `bot/service/pool/declaration.py`).
- `im.yml` and `model.yml` are REGISTRY-flavored config domains — each platform/provider registers a Pydantic schema via `register_kind` (`bot/config/domains/im.py` registers `qq` + `telegram`; `bot/config/domains/model.py` registers providers). Secrets use `Annotated[str, Secret()]`.
- `${ENV_VAR}` in yml values are interpolated at load time.
- Adding a new pool: add a name key under `workspace.pools` in `bot.yml` with its `agents:` tree (root agent = the one with no parent).
- Adding a new subagent: nest an `agents:` mapping under the parent agent (or add a flat entry with `parent:`). Position defaults handle the rest — declare only deviations.

### Common Patterns
- Secrets (IM tokens, API keys) live in `im.yml` / `model.yml` / `.env`, never committed.
- Editing pools via the WebUI Settings → Scope tab writes back to `bot.yml` (same file you could edit by hand); restart applies.

## Dependencies

### Internal
- `bot/config/` — typed config code (domains, stores, controllers) that loads and validates these files
- `modex_agent/scope/` — declaration loading, validation, compilation for `scopes/`

<!-- MANUAL -->

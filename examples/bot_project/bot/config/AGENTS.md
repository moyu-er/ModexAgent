<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-19 -->

# config

The **typed configuration code** for the bot — config-domain schemas, CRUD stores, and wire payloads that back the WebUI Settings API. This is the Python counterpart to the data files in `config/` (the YAML/JSON directory): the stores here read/write those files with validation, and the domains declare their typed shapes.

Two layers:

- **Config domains** (`domain.py` + `domains/`) — a generic typed-config registry. A `ConfigDomain` has a `name`, `yaml_path`, and a `flavor` (`SINGLETON` for one root schema, `REGISTRY` for many named kinds). Each domain registers Pydantic schemas; secrets use `Annotated[str, Secret()]` so reads mask them.
- **Persistence stores** (`*_store.py`) — single-source-of-truth read/write for one concern each (MCP registry, skills, prompts). All writes are atomic. Pools are declared in the scope declaration (`config/scopes/bot.yml`); the legacy pool tree store was deleted with the legacy road (ticket 11).

## Key Files

| File | Description |
|------|-------------|
| `domain.py` | `ConfigDomain` / `DomainFlavor` / `Secret` / `register_domain` + shared `mask`/`merge`/`describe` helpers. The foundation every domain and store builds on |
| `mcp_registry.py` | MCP server registry — single source of truth (`config/mcp/registry.json`); agents reference servers by name. CRUD: `write_registry` / `upsert_server` / `delete_server` / `server_used_by`. Also `read_shared_registry_flag` — the ADR-0017 `sharedRegistry` gate (default on, fail-open) read by `BotService` to opt into the shared MCP connection registry |
| `skills_store.py` | `SkillsStore` — global skill library (`local_skills/`) + per-agent assignment (`skills/<pool>/<agent>/`, real copy or link into a global source) |
| `prompt_store.py` | `PromptStore` — read/write agent prompt markdown (`agents/<name>.md`); pool-independent by agent name; atomic UTF-8 writes |
| `webui_config.py` | WebUI-specific config helpers (settings panel wiring) |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `domains/` | Per-domain schema registration: `im.py` (REGISTRY — `qq` + `telegram` kinds), `model.py` (SINGLETON — reuses `BotModelConfig`) |

## For AI Agents

### Working In This Directory
- This package holds **code**, not data. The data files live in `config/` (YAML/JSON) — these stores read/write them.
- Every structured object that crosses the HTTP API ↔ store boundary is a frozen Pydantic model (`frozen=True, extra="forbid"`) — see the scope `PoolSpec`/`AgentSpec` in `modex_agent/scope/spec.py`. Do not pass loose dicts.
- Secrets are declared `Annotated[str, Secret()]` on the domain schema; they are masked on read and honored on write. Never log or echo raw secret values.
- Stores operate on a configurable base dir (default `examples/bot_project`-relative; overridable per-instance for `tmp_path` tests).
- `${ENV}` interpolation stays **out** of the stores — they persist raw values with `${ENV}` placeholders; interpolation happens at load in the loader. Preserve this separation.
- Adding a new IM platform: declare a Pydantic section schema + `register_kind` in `domains/im.py`, add a `register_*.py` adapter (see `bot/adapters/`), and a matching section in `config/im.yml`.

### Common Patterns
- **One store per concern** — `PoolStore`, `SkillsStore`, `PromptStore`, MCP registry. Each is the single source of truth for its files.
- **Atomic writes** — `.tmp` + `os.replace`; UTF-8; preserve trailing newline (see `prompt_store.py`).
- **Editable vs baked** — payloads carry only user-editable fields; memory/experience presets are baked in the FW memory presets module (`modex_agent/memory/presets.py`) and never appear in payloads. Stores preserve non-editable fields when writing back.

## Dependencies

### Internal
- `modex_agent/ioc/configs/` — framework config models (memory, mcp, …) reused here instead of redefined
- `bot/service/model_config.py` — `BotModelConfig`, the schema the model domain reuses

<!-- MANUAL -->

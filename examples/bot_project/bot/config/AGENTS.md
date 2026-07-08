<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-08 -->

# config

The **typed configuration code** for the bot — config-domain schemas, CRUD stores, and wire payloads that back the WebUI Settings API. This is the Python counterpart to the data files in `config/` (the YAML/JSON directory): the stores here read/write those files with validation, and the domains declare their typed shapes.

Two layers:

- **Config domains** (`domain.py` + `domains/`) — a generic typed-config registry. A `ConfigDomain` has a `name`, `yaml_path`, and a `flavor` (`SINGLETON` for one root schema, `REGISTRY` for many named kinds). Each domain registers Pydantic schemas; secrets use `Annotated[str, Secret()]` so reads mask them.
- **Persistence stores** (`*_store.py`) — single-source-of-truth read/write for one concern each (pool tree, MCP registry, skills, prompts). All writes are atomic; payloads are frozen Pydantic models (`pool_payloads.py`).

## Key Files

| File | Description |
|------|-------------|
| `domain.py` | `ConfigDomain` / `DomainFlavor` / `Secret` / `register_domain` + shared `mask`/`merge`/`describe` helpers. The foundation every domain and store builds on |
| `pool_store.py` | `PoolStore` — read/write one pool's tree across `pool.yml` + `templates/*.yml`; preserves non-editable fields on write |
| `pool_payloads.py` | Frozen Pydantic wire/value objects (`PoolTree`, `MainAgentNode`, `SubagentNode`, …) crossing the HTTP API ↔ store boundary |
| `mcp_registry.py` | MCP server registry — single source of truth (`config/mcp/registry.json`); agents reference servers by name. CRUD: `write_registry` / `upsert_server` / `delete_server` / `server_used_by` |
| `skills_store.py` | `SkillsStore` — global skill library (`local_skills/`) + per-agent assignment (`skills/<pool>/<agent>/`, real copy or link into a global source) |
| `prompt_store.py` | `PromptStore` — read/write agent prompt markdown (`agents/<name>.md`); pool-independent by agent name; atomic UTF-8 writes |
| `memory_defaults.py` | Baked (non-user-editable) memory presets — `main-rich` (long-term layers) and `sub-minimal` (session-only + pruned + tool_chain_repair) |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `domains/` | Per-domain schema registration: `im.py` (REGISTRY — `qq` + `telegram` kinds), `model.py` (SINGLETON — reuses `BotModelConfig`) |

## For AI Agents

### Working In This Directory
- This package holds **code**, not data. The data files live in `config/` (YAML/JSON) — these stores read/write them.
- Every structured object that crosses the HTTP API ↔ store boundary is a frozen Pydantic model (`frozen=True, extra="forbid"`) — see `pool_payloads.py`. Do not pass loose dicts.
- Secrets are declared `Annotated[str, Secret()]` on the domain schema; they are masked on read and honored on write. Never log or echo raw secret values.
- Stores operate on a configurable base dir (default `examples/bot_project`-relative; overridable per-instance for `tmp_path` tests).
- `${ENV}` interpolation stays **out** of the stores — they persist raw values with `${ENV}` placeholders; interpolation happens at load in the loader. Preserve this separation.
- Adding a new IM platform: declare a Pydantic section schema + `register_kind` in `domains/im.py`, add a `register_*.py` adapter (see `bot/adapters/`), and a matching section in `config/im.yml`.

### Common Patterns
- **One store per concern** — `PoolStore`, `SkillsStore`, `PromptStore`, MCP registry. Each is the single source of truth for its files.
- **Atomic writes** — `.tmp` + `os.replace`; UTF-8; preserve trailing newline (see `prompt_store.py`).
- **Editable vs baked** — payloads carry only user-editable fields; memory/experience presets are baked (`memory_defaults.py`) and never appear in payloads. Stores preserve non-editable fields when writing back.

## Dependencies

### Internal
- `modex_agent/ioc/configs/` — framework config models (memory, mcp, …) reused here instead of redefined
- `bot/service/model_config.py` — `BotModelConfig`, the schema the model domain reuses

<!-- MANUAL -->

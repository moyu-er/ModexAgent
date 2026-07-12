<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-07-08 -->

# config

Runtime configuration files for the bot: main config, IM adapter credentials, model definitions, MCP server registry, and pool definitions. YAML/JSON with `${ENV_VAR}` interpolation. The **typed config code** (Pydantic domain schemas, stores, controllers) lives in `bot/config/` (a separate Python package), not here — this directory holds only data files.

## Key Files

| File | Description |
|------|-------------|
| `bot_config.yml` | Main config — runtime safety, observability, workspace toggle. (Pool/agent config no longer lives here; see `pools/`) |
| `im.yml` | IM adapter credentials — one top-level section per platform (`qq`, `telegram`). Gitignored (contains secrets). Each adapter reads only its own section |
| `im.example.yml` | Template for `im.yml` — copy and fill in real values |
| `model.yml` | Model definitions — the single source of truth: `default_provider` / `default_model` + a per-provider model list. Shared across all pools |
| `model.example.yml` | Template for `model.yml` |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `mcp/` | MCP server registry (see below) |
| `pools/` | Pool definitions (see below) |

## pools/ Structure

Each pool is a **directory** — the directory name is the pool identity. Inside sits `pool.yml` (main agent config) and a `templates/` dir of subagent templates:

```
pools/
├── default/                 # pool name = directory name
│   ├── pool.yml             # main agent config (max_steps, tools, approval, memory, …)
│   └── templates/           # subagent templates — one .yml each, auto-registered
└── coder/
    ├── pool.yml
    └── templates/
```

- The **main agent name** defaults to the directory name (override via `main_agent_name` in `pool.yml`).
- Subagents are `templates/*.yml` — the main agent delegates to them via `send_to_agent`.
- The bundled `default` and `coder` pools are examples — use, inspect, or replace them.

## mcp/ Structure

A single MCP server registry (not per-agent files):

```
mcp/
├── registry.json            # global MCP server registry (stdio/SSE/streamable_http)
└── registry.example.json    # template
```

Each agent selects which registered servers it sees via its pool/subagent config (the WebUI **MCP** tab edits `registry.json`; the per-agent selection lives in pool config). MCP JSON uses `command`/`args` (stdio) or `url` + `headers` (SSE/streamable_http).

## For AI Agents

### Working In This Directory
- These are **data files**; the code that loads/validates them lives in `bot/config/` (`domain.py`, `domains/im.py`, `domains/model.py`, `pool_store.py`, `mcp_registry.py`, `skills_store.py`, …).
- `im.yml` and `model.yml` are REGISTRY-flavored config domains — each platform/provider registers a Pydantic schema via `register_kind` (`bot/config/domains/im.py` registers `qq` + `telegram`; `bot/config/domains/model.py` registers providers). Secrets use `Annotated[str, Secret()]`.
- `${ENV_VAR}` in yml values are interpolated at load time.
- Adding a new pool: create `pools/<name>/pool.yml` + `pools/<name>/templates/` dir.
- Adding a new subagent: create `pools/<pool>/templates/<agent>.yml`.

### Common Patterns
- Secrets (IM tokens, API keys) live in `im.yml` / `model.yml` / `.env`, never committed.
- Editing these via the WebUI Settings tabs writes back to the same files you could edit by hand.

## Dependencies

### Internal
- `bot/config/` — typed config code (domains, stores, controllers) that loads and validates these files

<!-- MANUAL -->

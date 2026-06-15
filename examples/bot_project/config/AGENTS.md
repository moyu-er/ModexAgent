<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-13 -->

# config

Configuration files for bot agents, MCP servers, and pool definitions. Uses YAML/JSON with `${ENV_VAR}` interpolation support.

## Key Files

| File | Description |
|------|-------------|
| `bot_config.yml` | Main config — agents, memory layers, tool settings, runtime safety, observability |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `mcp/` | MCP server configs per agent (JSON files) |
| `pools/` | Pool definitions (see below) |

## pools/ Structure

Each pool has a `{pool_name}.yml` defining the agents and a `{pool_name}/templates/` directory with subagent template configs:

```
pools/
├── main.yml              # Main pool: main agent + office-expert, query-12306 subagents
├── coding.yml             # Coding pool: main agent + planner, worker, reviewer, etc.
├── main/templates/        # Subagent configs for main pool
│   ├── office-expert.yml
│   └── query-12306.yml
└── coding/templates/      # Subagent configs for coding pool
    ├── planner.yml
    ├── worker.yml
    └── ...
```

## mcp/ Structure

One JSON file per agent name, defining MCP server connections:

```
mcp/
├── main.json           # Main agent MCP servers (filesystem, fetch, etc.)
├── coding.json         # Coding pool MCP servers
├── office-expert.json  # Office expert MCP servers
└── query-12306.json    # 12306 query MCP servers
```

## For AI Agents

### Working In This Directory
- `bot_config.yml` is the IOC single source of truth — loaded by `utils/config_loader.py`.
- Pool yml files define agent roles (`main` vs `subagent`), tool access, memory config, and system prompt paths.
- Subagent templates in `pools/{name}/templates/` are loaded by `pool_builder.py` during pool creation.
- MCP JSON files use the standard MCP config format with `command`/`args` (stdio) or `url` (SSE/streamable_http).

### Common Patterns
- `${ENV_VAR}` in yml values are interpolated at load time.
- Adding a new pool: create `{name}.yml` + `{name}/templates/` dir, then add pool name to app config.
- Adding a new subagent: create template yml in the pool's `templates/` dir.

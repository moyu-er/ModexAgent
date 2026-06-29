# Context Map

This repo is multi-context. Each context owns its own domain language
(`CONTEXT.md`) relevant to the topic you're working on.

| Context | Path | Scope |
| --- | --- | --- |
| Core | `CONTEXT.md` (repo root) | The reusable multi-agent framework code under `src/modex_agent/`: Pool, Workspace, Assembly, runtime, multi-agent, approval, terminal |
| Bot project | `examples/bot_project/CONTEXT.md` | The example business wiring: a QQ bot (botpy) with webui, plugins, skills, templates |

System-wide architectural decisions live in `docs/adr/` at the repo root.

# Context Map

This repo is multi-context. Each context owns its own domain language
(`CONTEXT.md`) relevant to the topic you're working on.

| Context | Path | Scope |
| --- | --- | --- |
| Framework | `CONTEXT.md` (repo root) | The reusable framework under `src/modex_agent/`: foundational `core` contracts plus memory, messaging, persistence, runtime, multi-agent, approval, and terminal domains |
| Bot project | `examples/bot_project/CONTEXT.md` | The example business wiring: a QQ bot (botpy) with webui, plugins, skills, templates |

System-wide architectural decisions live in `docs/adr/` at the repo root.

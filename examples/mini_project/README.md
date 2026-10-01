# mini_project — the framework's second consumer

The smallest project that boots on `modex_agent` alone. The whole example
is **one scope declaration + one tiny AppConfig + one plugin file + one
entry point**; every line of assembly between them is framework-owned
(`RunnableAppService`, ADR-0052 §2's runnable defaults). Nothing is copied
from `examples/bot_project`, and no framework code knows this project
exists — that composability claim is mechanically proven by
`tests/test_mini_project_boot.py`, which boots this directory read-only
against a tmp copy, drives one fully offline turn, and fails CI if the
hand-written line count grows past its ceiling.

Fully offline: the LLM is a scripted provider registered by the plugin
(`mini_scripted`) — no network, no API keys, no Langfuse.

## Files

| File | What it is |
|------|------------|
| `config/scopes/mini.yml` | The scope declaration — one pool (`mini`), one react agent (`main`). Declares the custom tool/hook/LLM provider by registered name (`+mini_echo`, `+mini_turn_logger`, `llm_provider: mini_scripted`) plus one framework-bundled tool (`+read`). |
| `config/app.yml` | The AppConfig surface `RunnableAppService` consumes: FILE persistence, tracing off, user-plugin dir off. Everything unlisted keeps its framework default. |
| `agents/main.md` | The main agent's system prompt (resolved via the declaration's `file_prompt` convention `agents/<name>.md`). |
| `mini_plugins/mini.py` | The project plugin — one `Plugin.register` call registering three slots: a TOOL (`mini_echo`, appends to a log), a HOOK (`mini_turn_logger`, after-turn logger), an LLM_PROVIDER (`mini_scripted`, the offline two-step script: call the tool, then answer). |
| `main.py` | The entry point — builds `AppAssemblyRoots` (config/resource/workspace-home all this directory, plugin dir `mini_plugins`, declaration `mini.yml`), constructs `RunnableAppService` with a demo input/output adapter pair, initializes, drives one turn, shuts down. |
| `tests/test_mini_project_boot.py` | The CI anchor — see below. |

## Run

```bash
python examples/mini_project/main.py
```

Output (runtime data lands under `examples/mini_project/.modex/`):

```
turn outcome: finished
agent reply: mini turn complete — echo tool ran.
```

`mini_echo.log` holds the text the custom tool was asked to echo;
`mini_turns.log` holds the line the custom hook appended after the turn.

## The composability story

- **Declaration** (`config/scopes/mini.yml`): the pool, its agent, and the
  component references — the declaration is the single authority.
- **Bootstrap** (`modex_agent.app.runnable.RunnableAppService`): loads the
  AppConfig and the component registry (framework `DefaultPlugin` + this
  project's `mini_plugins/`), opens the shared persistence, compiles the
  declaration, and boots every declared pool through the production
  `create_pool` road. A deployment with richer needs (multi-live
  workspaces, MCP, WebUI) subclasses `AppService` itself — that is what
  `examples/bot_project` is.
- **Plugins** (`mini_plugins/`): the deployment's own components, loaded
  by directory discovery (no `sys.path`, no import-name requirements) and
  referenced from the declaration by registered name.

Adding a second agent, a subagent, or another pool is a declaration edit;
adding another tool/hook/provider is a `ctx.register_*` line in the
plugin. Neither touches assembly code.

## The CI anchor

`tests/test_mini_project_boot.py` (run by the repository's Unit Tests
workflow alongside the framework suite):

1. copies this directory to a tmp path (the in-repo tree stays read-only)
   and boots it exactly the way `main.py` does;
2. drives one turn through the real dispatch road (request scope → inbox →
   poller → react pipeline → scripted provider) and asserts the outcome is
   `finished` with the scripted reply;
3. asserts the custom tool ran (`mini_echo.log`) and the custom hook fired
   (`mini_turns.log`);
4. counts the hand-written non-blank lines of everything except `tests/`
   and this README and asserts the total stays under 400 — if the example
   needs to grow, first check whether the missing piece belongs in the
   framework (`src/modex_agent/app/`), then raise the ceiling consciously.

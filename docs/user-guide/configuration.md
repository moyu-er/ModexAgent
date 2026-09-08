<p align="center">
  <a href="configuration.md">English</a> |
  <a href="configuration.zh-CN.md">简体中文</a> |
  <a href="../../README.md">Root README</a>
</p>

# Configuration

ModexBot can be configured from the WebUI or by editing its files directly. The WebUI validates writes and tells you when a restart is needed, so it is the easiest place to begin.

## Configuration Root

Every relative path in this guide is under the **bot project directory**, normally `examples/bot_project/` in a source checkout. It is not relative to the chat workspace selected with `/cd` or the WebUI workspace picker, and it is not a workspace plugin directory. Bot prompts, MCP registrations, and Skill assignments remain here.

Runtime state defaults to `<workspace-target>/.modex/`. You can rename this directory with `paths.data_dir_name`, including an override in the primary workspace declaration. The home workspace stores it under the bot project; another workspace stores its own state under that target.

The home workspace and ordinarily opened directories reuse `config/scopes/bot.yml`. **Create Workspace** creates an independent declaration at `config/scopes/workspaces/<name>.yml`, still under the bot project. The current **Pools** and **Scope** Settings views edit the primary `bot.yml`; they do not automatically switch to a dynamic workspace declaration.

## Settings And Files

| Settings area | Backing file or directory | Apply behavior |
| --- | --- | --- |
| Runtime infrastructure | `config/bot_config.yml` | Edit by hand; restart |
| IM Adapters | `config/im.yml` | Save, then restart |
| Models | `config/model.yml` | Save, then restart |
| Pools | `config/scopes/bot.yml` | Structured editor; restart |
| Scope | `config/scopes/bot.yml` | Declaration tree, Provenance bill, and Declaration (YAML); restart |
| MCP | `config/mcp/registry.json` | Save, then restart |
| Prompts | `agents/*.md` | Save, then restart |
| Skills | `local_skills/<skill>/` and `skills/<pool>/<agent>/<skill>/` | Upload and assignment are live |

`config/model.yml`, `config/im.yml`, and `config/mcp/registry.json` can contain credentials and are gitignored in the reference bot. Keep these files private and local; never commit API keys, tokens, app secrets, or generated local configuration.

## A Practical UI Flow

1. Open the WebUI and choose **Settings**.
2. In **Models**, add a provider and model, then select a valid default provider/model pair.
3. In **Prompts**, create the prompt files your agents will use.
4. In **Pools**, add or edit pools and agents. Use the preview and effective-tool display before saving.
5. In **Scope**, use **Declaration tree** for structure, **Provenance bill** for effective configuration, and **Declaration (YAML)** for the source text.
6. In **MCP**, register servers; then select those servers on each native agent in **Pools**.
7. In **Skills**, upload a Skill folder and assign it to eligible native agents.
8. Use the restart action shown by Settings after changing models, IM, pools/scope, MCP, or prompts. Skill upload and assignment do not require it.

## Runtime And Models

`config/bot_config.yml` contains shared infrastructure rather than agent rosters: safety deadlines, data paths, WebUI address, persistence defaults, and observability. The primary pool and agent declaration is `config/scopes/bot.yml`; workspaces created with **Create Workspace** have their own files under `config/scopes/workspaces/`.

`config/model.yml` is the shared model registry. Each provider owns its connection settings and model list; `default_provider` and `default_model` must resolve to one declared entry. The WebUI masks secret fields on read. A saved model change becomes active after restart.

## Create A Single Pool

Create the prompt first, either with **Settings > Prompts > Create** or as `agents/assistant.md`. For example:

```markdown
You are a concise project assistant.
Work only from evidence, use tools when useful, and verify results before replying.
```

Use this as a starting point for a complete one-pool declaration with one root agent:

```yaml
workspace:
  name: bot
  pools:
    custom:
      agents:
        assistant:
          description: A concise assistant for project research and support.
          toolset: read_only
          system_prompt: agents/assistant.md
          capabilities:
            todo: {}
```

For an existing `config/scopes/bot.yml`, **do not replace the whole file blindly**. Add `custom:` under the existing `workspace.pools` mapping, or use **Settings > Pools > Add pool**. The shipped graph specifications refer to existing agents such as `coder/orchestrator` and `review/reviewer`; removing those declarations without also updating the workflows makes startup validation fail.

The `system_prompt` path is resolved from the bot project directory. It is explicit above so a missing file is easy to spot. If omitted, the default convention is `agents/<agent-name>.md`; `prompt_name` can select another prompt stem. There is no supported user `config/profiles/` directory: `toolset` selects the built-in profile, and omitting it uses the position default (`full` for a root, `read_write` for a child).

## Tools And Capabilities

Available `toolset` values are `full`, `read_write`, `read_only`, `none`, and `web`. A toolset is a roster preset, not a security boundary. In particular, `read_only` includes `bash`; remove `bash` or configure the sandbox if shell writes must be constrained.

Use exactly one `tools` list mode:

```yaml
tools:
  - +web_search
  - -bash
```

The prefixed form incrementally adds to or removes from the selected toolset. Alternatively, an all-unprefixed list replaces the toolset roster:

```yaml
tools:
  - read
  - ls
  - grep
  - glob
  - todo_write
  - todo_read
```

Do not mix prefixed and unprefixed entries. In a mixed list, unprefixed entries are not replacement entries and are ignored by the merge. A replacement list must retain tools required by enabled capabilities; the two Todo tools above keep the earlier `todo: {}` declaration valid. Capability-provided same-name replacements, such as the ACI edit implementation, are compiled separately; inspect **Scope > Provenance bill** rather than trying to encode replacement records in `tools`.

`capabilities` is an override map. `{}` enables a capability with default configuration, while `false` disables one; `true` is invalid. Common examples are:

```yaml
capabilities:
  skills: false
  todo: {}
  experience: {}
```

`skills` auto-applies to native agents and can be vetoed. `todo` adds its paired task tools, hooks, prompt section, and persistent per-session Todo store. `experience` is separately opt-in and bundles its tool, review hook, prompt injection, storage, and curator. External agents are structurally outside this native capability path and must not declare a non-empty capabilities block.

## Persistence And Memory

The persistence backend is `sqlite` by default and can be changed to `file` with top-level `persistence.backend` in `config/bot_config.yml`; a workspace declaration may override the same backend under `workspace.persistence`. This choice covers workspace runtime and session-related stores.

Session context memory is enabled by default for native agents. It keeps the active conversation context, while `memory.session.max_context_tokens` controls when compression starts. Archive is an opt-in native-root layer for compressed history summaries. Core Memory is an opt-in native-root layer for durable agent and user context, and it requires Archive. Experience is independently opt-in through `capabilities.experience`; it reviews useful completed interactions into reusable experience records and makes them available to later turns.

This agent-level example enables Archive, Core Memory, and Experience together:

```yaml
memory:
  archive_enabled: true
  core_enabled: true
capabilities:
  experience: {}
```

Choose these features by purpose. Session context remains available without any of the optional features. Archive/Core can be enabled without Experience, and Experience can be enabled without Archive/Core; only Core requires Archive.

## MCP Servers

If `registry.json` does not exist yet, start from the tracked, secret-free template. On macOS or Linux:

```bash
test -e config/mcp/registry.json || cp config/mcp/registry.example.json config/mcp/registry.json
```

On Windows PowerShell:

```powershell
if (-not (Test-Path "config/mcp/registry.json")) {
  Copy-Item "config/mcp/registry.example.json" "config/mcp/registry.json"
}
```

If the file already exists, use **Settings > MCP** so you do not overwrite local values.

The tracked template currently contains this stdio server:

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp"]
    }
  }
}
```

This entry launches `npx`, so install Node.js and npm first. On its first run, `npx` may contact the npm registry, download a package, and execute its code. Add only MCP packages and commands you trust, review `command` and `args`, and pin package versions when reproducible installations are required.

Registering a server does not expose it automatically. Select its registry name on each **native** agent, which writes the agent's `mcp` list in `config/scopes/bot.yml`:

```yaml
mcp:
  - playwright
```

Restart after changing the registry or an agent's selection so connections and tool rosters are rebuilt. External providers own their own tools and do not consume this framework-native MCP selection.

## Skills

In **Settings > Skills**, upload a folder containing `SKILL.md`; the WebUI writes the repository library under `local_skills/<skill>/`. Then select a pool and agent and assign it. Assignment creates a link, or a Windows junction fallback, at `skills/<pool>/<agent>/<skill>/`.

The per-agent directory is the assignment authority; there is no `skills:` list in `bot.yml`. Upload, delete, assign, and unassign are read live in normal operation. Most other Settings changes require restart, as indicated by the restart banner.

You can also place a real Skill copy directly under `skills/<pool>/<agent>/<skill>/`. The native `skills` capability must remain effective for that agent. User-installed `~/.agents/skills/<skill>/` entries augment the global library, while repository `local_skills/` wins a same-name conflict.

## IM Adapters

**Settings > IM Adapters** writes independent `qq` and `telegram` sections to `config/im.yml`. QQ is built when both `app_id` and `secret` are present. Telegram is built when its setting is enabled and `token` is present; `proxy` is optional. Both support `allow_from`, where `"*"` allows any sender.

Save, then restart to create or stop adapters. Keep the file local and enter credentials through the masked Settings fields; do not publish an example containing real values.

## Approval And Sandbox

Human approval and sandbox enforcement are independent, opt-in controls. Approval is declared only on a pool root. The reference `bot.yml` uses this exact native-root fragment:

```yaml
interceptors:
  - +sandbox_guard
interceptor_configs:
  sandbox_guard:
    sandbox:
      backend: host
      exclusive:
        write_surface: workspace
approval:
  enabled: true
```

This is a reference fragment, not a promise of complete isolation. `backend: host` activates guards but provides no kernel isolation. The sandbox decision path covers known framework-native tools and targets; unknown tools have no boundary coverage, and external-provider tools bypass the framework `ToolNode`. MCP, shell networking, inherited credentials, and provider-side tools must be assessed on their own boundaries.

Use **Settings > Pools**, select the root agent, and configure Approval and Sandbox there. Review the effective tools and **Scope > Provenance bill**, save, restart, and test a harmless inside-workspace action plus an outside-workspace action before relying on the policy.

## Check Changes Before Restart

Use **Pools** for guided editing or **Scope** for direct YAML changes. Settings checks the declaration before saving; if it reports an error, fix the named pool, agent, or field and save again. An invalid edit does not replace the current declaration.

After a successful save, check **Scope > Declaration tree** for the intended root/child shape and **Scope > Provenance bill** for the tools and capabilities each agent will receive. Use **Declaration (YAML)** when you need to compare the saved source. Then restart from the banner. If an adapter, model, prompt, MCP server, or pool does not start as expected, inspect `modexbot logs -f` for the affected name and configuration field.

---

[Getting started](getting-started.md) · [Workflows](workflows.md) · [Extensions](extensions.md)

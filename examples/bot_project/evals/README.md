# evals: golden-case conventions

> **Status (2026-08-18):** the committed golden suite (`evals/golden/`) was
> removed — the four v1 cases were too weak to anchor a standard (three
> file-shape-only assertion sets, one zero-assertion case). The cassette
> MECHANISM (record/replay, four gates, this contract) is unchanged and
> retained. The CI regression workflow is paused to manual dispatch until a
> v2 suite is committed. Rebuild standard: see "Golden v2 (TODO)" at the end
> of this file; the removal decision is logged in DECISIONS.md.

Golden cases are recorded LLM transcripts that replay offline, bit-identically,
to detect agent behavior drift. This file is the contract that the eval
harness implements (Wave 3 replay, Wave 4 record-golden). It documents
conventions only: no executable logic lives in this directory.

Path citations below are relative to the repo root, except `bot/...` paths
which are relative to `examples/bot_project/`.

## Layout

```
evals/
  golden/<case>/
    item.json                    # the task, EvalItemSpec schema (bot/eval/task_spec.py:84)
    meta.json                    # recording fingerprint (schema below)
    cassette/<trace_id>/         # content-addressed LLM call recordings
      index.json                 # CassetteManifest (src/modex_agent/trace/cassette.py:87)
      <sha256-key>.json          # one payload file per llm_call_key (cassette.py:325-334)
  runs/                          # local run archives, gitignored (never committed)
```

- `golden/` is committed: item + meta + cassette together define one case.
- The cassette save layout is `<base_dir>/<trace_id>/{index.json, <key>.json}`
  (cassette.py:12-17, `CassetteRecorder.save` at cassette.py:310-347). Each
  `<key>.json` holds the recorded request+response for one content-addressed
  LLM call; `index.json` holds the manifest (`trace_id`, `entries`,
  `created_at`, cassette.py:87-94).
- `runs/` holds per-run replay reports and diffs. It is local evidence only
  (see `.gitignore`) and may be deleted at any time.
- `evals/DECISIONS.md` is the flywheel log: one dated entry per golden
  add/refresh/remove decision, so every cassette change has a recorded why.
  See DECISIONS.md for the flywheel decision log.

## meta.json schema

Frozen seven-field fingerprint recorded alongside every cassette. Wave-3/4
code implements exactly this; do not add fields.

```json
{
  "model": "deepseek-v4-flash",
  "temperature": 0.0,
  "tool_names": ["edit", "grep", "ls", "read", "write"],
  "tool_schema_sha256": "<sha256 of canonical JSON of the sorted tool schemas>",
  "prompt_sha256": "<sha256 of the static system prompt>",
  "platform": "win32",
  "recorded_at": "2026-08-15T12:34:56+08:00"
}
```

Replay role of each field: these are the inputs of fingerprint gate 1, checked
before any cassette lookup.

- `model`: replay must request the identical model string. It is part of
  `llm_call_key` (cassette.py:184-203), so any other model can never match a
  recorded key.
- `temperature`: same keying role; sampling config must be identical.
- `tool_names`: the sorted effective tool list AFTER `deny_tools` filtering
  (EvalItemSpec.deny_tools, bot/eval/task_spec.py:92). This is the list the
  ReAct loop actually saw, not the preset name.
- `tool_schema_sha256`: SHA-256 over the canonical JSON (sort_keys) of the
  sorted tool schemas. Any schema edit (renamed parameter, changed
  description) changes this hash and fails the gate before replay starts.
- `prompt_sha256`: SHA-256 of the static eval prompt; detects prompt drift
  between record and replay.
- `platform`: `sys.platform` at recording. Gates platform-sensitive cases;
  v2 shell goldens pin replay to it.
- `recorded_at`: ISO timestamp. Provenance only, never compared.

The only additional key ever allowed is the optional `baseline: true` flag
described under the replay gates below.

## Static prompt constraint

Eval prompts are static and path-free. The reason is the cassette key:

- `RuntimeProvider` injects the current date/hour into the system prompt and
  refreshes it hourly, and folds the working directory into its version key
  (src/modex_agent/memory/prompt_pipeline/providers.py:333-350).
- Eval workspaces are temp directories that differ per run.
- `llm_call_key` content-addresses the FULL message list: every message,
  including the system prompt, feeds the SHA-256 (cassette.py:184-203).

Any per-run variable in the prompt (timestamp, hour, platform line, absolute
temp path) changes every recorded key and makes the cassette unmatchable.
Therefore eval prompts contain no timestamps, no injected runtime metadata,
and no absolute paths.

## Workspace token contract

File tools echo absolute workspace paths in their results:
`WorkspaceScopedFileTool._scoped_args` rewrites the `path` argument against
the resolved workspace root (src/modex_agent/tools/workspace_scoped.py:131-146),
and results such as `WriteFileTool`'s `Wrote N bytes to {path}` echo the
resolved path back (src/modex_agent/tools/standard/file_tool.py:620-638).
Those paths land in tool results, which land in the message list, which feeds
`llm_call_key`.

The harness therefore normalizes `str(workspace.resolve())` to the literal
token `<workspace>` in tool results, identically in record AND replay, so
keys stay stable across different temp directories. A golden that leaks a raw
absolute path into a recorded message is defective and must be re-recorded.

## Cassette contract & four replay gates

Contract:

- Provider-only wrap. The harness wraps ONLY the LLM provider
  (`CassetteRecorder.wrap_provider` / `CassetteReplayEngine.wrap_provider`,
  cassette.py:247-249 and 516-518). Tools are NEVER cassette-wrapped: they
  execute for real in both record and replay, so world assertions check
  genuine side effects.
- Replay lookup is keyed by
  `llm_call_key(messages, model, temperature, max_output_tokens, tools, kwargs)`
  (cassette.py:184-203). The replay provider never falls back to the wrapped
  provider; a miss raises `KeyError` inside the provider (cassette.py:524-530).
- That `KeyError` does not crash the run: `ReActAgent.run` catches all
  exceptions and returns `AgentResult(error=..., stop_reason=ERROR)`
  (src/modex_agent/agents/react/agent.py:383-395). A cassette miss surfaces
  as a seemingly ordinary errored turn.

Hence the FOUR GATES every golden case must pass. All four are required;
no gate substitutes for another:

1. Fingerprint match: every `meta.json` field above matches the replay
   environment (model, temperature, tool_names, tool_schema_sha256,
   prompt_sha256, and for platform-pinned cases platform).
2. Zero cassette misses: the engine-level miss counter reads 0 across all
   turns. Gate 1 cannot predict every key drift; this gate catches it.
3. Clean turns: every turn's `AgentResult.error` is None and its stop_reason
   is COMPLETED. Because of the catch-all at agent.py:383, errors never
   raise; they must be checked on the result object.
4. Non-vacuous oracle: at least one world assertion (`world_assertions`,
   bot/eval/task_spec.py:94) executes and passes, OR the case meta carries
   `baseline: true`, where the oracle IS gates 1-3 plus determinism and world
   assertions are intentionally absent.

## Maintenance rules

- Record only via `record-golden` with a real LLM. Credentials come from the
  `TEST_LLM_*` environment variables. Never hand-edit a cassette.
- Replay is keyless and offline: no API keys, no network. CI replay jobs
  need no secrets.
- EVERY golden diff (new case, refreshed cassette, edited item.json or
  meta.json) is human-reviewed before commit.
- Refresh is manual. A failing replay is investigated first; re-recording is
  a reviewed decision logged in DECISIONS.md, never an automated overwrite.
- The nightly CI record job runs a secret preflight: missing `TEST_LLM_*`
  fails the job loudly. It never silently skips to green.

## v1 scope: platform-neutral cases

- Agent terminal/shell tools are DENIED in all v1 goldens via `deny_tools`
  (bot/eval/task_spec.py:92). Choosing a narrower toolset is not enough: the
  framework `READ_ONLY` preset still includes bash
  (src/modex_agent/tools/presets.py:163-168, restated in the
  bot/eval/task_spec.py:3-8 docstring), so v1 cases must deny shell tools
  explicitly.
- Reason: `SubprocessTool` schema and semantics vary by OS shell. Its
  description is selected per `ShellFamily` with per-family syntax rules
  (BASH vs Windows CMD vs ZSH vs SH, src/modex_agent/tools/terminal/subprocess_tool.py:236-251),
  so a shell-using golden recorded on one OS cannot replay on another.
- Shell cases are v2, platform-pinned via the meta `platform` field.
- Harness-side `command_exit` assertions (bot/eval/task_spec.py:47-54) run
  OUTSIDE the agent: the harness executes the command itself, so they never
  touch agent tools or the cassette and are fine in v1.

## Golden v2 (TODO — not scheduled)

The v2 suite is deferred until the eval-integration effort
(docs/design/eval-integration/MAP.md) lands its judge and benchmark pieces.
When it is built, the standard is:

- **Assertion layering**: behavior assertions first — execute the artifact
  and compare stdout/exit (e.g. `command_exit` running a `python -c` wrapper
  over the produced file), not file-shape checks alone. Zero-assertion cases
  are forbidden (`baseline: true` remains the only exception, and only for
  cases whose oracle IS determinism).
- **Composition guidance**: at least one case per sensor class —
  execute-to-verify repair (broken input, agent must read→locate→edit→
  self-verify), multi-turn state pipeline (each turn consumes the previous
  turn's product), read-only discipline (analysis without side effects),
  and one governance/compression-sensitive long trajectory (many tool
  results; the only replay sensor for lossy-compaction and tool-chain-repair
  regressions — v1 had none, which left the double-run sabotage check
  unusable).
- **Environment neutrality + platform pinning**: tasks live entirely inside
  the temp workspace; shell-using cases pin replay via the meta `platform`
  field (v1 scope rules above still apply to non-shell cases).
- **Freeze discipline**: the suite is frozen once committed; a bad case is
  fixed by adding to a v(n+1) set, never by editing committed cases.
- **Flywheel logging**: every add/refresh/remove lands in DECISIONS.md.
- **Rubric assertions**: a subjective-dimension assertion layer is expected
  once the judge architecture (eval-integration ticket 03) is settled; do
  not design around it yet.

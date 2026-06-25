# Pool mode is the only assembly mode

The framework supports only pool-mode assembly. The previous `create_app` /
`App` pipeline-mode entry point was dead code — no caller in the repo used it,
and the business layer (`BotService`) always assembled via pool mode. Rather
than maintain two paths, we delete the pipeline-mode path and make pool
assembly the framework's sole assembly interface.

## Considered Options

1. **Pool only (chosen).** Delete `create_app`/`App`. The framework's
   assembly entry point builds pool-mode runtime from `AppConfig`. Simpler,
   honest about what the system actually does.

2. **Keep both, converge later.** Leave `create_app` as a stub, add pool
   assembly alongside. Rejected: maintaining a dead path is a liability, and
   "converge later" never happens.

3. **Abstract over mode.** `create_app(mode="pool" | "pipeline")`. Rejected:
   one adapter means a hypothetical seam. No real consumer needs pipeline
   mode; the abstraction would be speculative.

## Consequences

- Single-agent use cases must still go through pool assembly (one pool with
  one main agent, no subagents). This is slightly heavier but removes the
  dual-path maintenance burden.
- Future single-agent-only deployments would need to either reuse pool
  assembly or reintroduce a lightweight path — but only if a real need emerges.
- `modex_agent/ioc/factories/app.py` and its exports are deleted.

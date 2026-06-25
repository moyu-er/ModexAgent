# Keep zero-usage deep modules when they carry a real seam

An architecture review flagged `modex_agent/sandbox/` as deletable on the grounds
that it has zero live callers (only its own self-tests import it). That
conclusion is rejected. `sandbox/` is an important capability that is
*currently unused by the example bot*, not dead. Its `SandboxAdapter` base has
four real subclass implementations (Subprocess / Landlock / Docker / E2B) plus
a guard chain built to close known security gaps. Deleting it would discard a
designed seam and its implementations, not concentrate complexity.

This is a general principle, not a sandbox exception.

## Considered Options

1. **Retain zero-usage deep modules that carry a real seam (chosen).** The
   deletion test is refined: a module with a real adapter seam — a base
   class/interface with two or more genuine subclass implementations, or a
   designed extension point with clear subclass potential — earns its keep at
   zero callers. Deleting it loses the seam and its implementations; the
   complexity does not vanish, it would have to be rebuilt. Zero-usage is a
   signal to *document and slim the interface*, not to delete.

2. **Delete anything with zero callers (raw deletion test).** Rejected for
   seam-bearing modules: it confuses "not wired into the example" with
   "dead." A deep module behind a clean seam is an asset; its absence of
   callers today is a wiring/product question, not an architecture defect.

3. **Quarantine zero-usage modules out of the framework.** Rejected for the
   same reason: moving `sandbox/` out of `modex_agent/` hides a first-class
   capability and severs it from the security surface it is meant to serve.

## Consequences

- `sandbox/` stays in the framework as a first-class module. Action on it is
  limited to ADR-0005 work: slim the 92-line `__init__` to a real seam
  (selection entry points + the adapter/guard ABCs behind `sandbox.adapters` /
  `sandbox.guard`), keep all four adapters and the guard chain. Wiring it into
  `CommandTool`/`SubprocessTool` is a future product decision, not a refactor
  prerequisite.
- The same rule applies elsewhere: terminal's heavier `TerminalManager`
  subclass (LRU / persistence / memory-pressure) is not deleted as "unused" —
  its capability is preserved; the duplication with `managers.py` is resolved
  by clarifying roles or folding capability inward, decided during that
  candidate's grilling, never by dropping the subclass.
- Future architecture reviews must distinguish *zero-callers-but-real-seam*
  (retain, per this ADR) from *genuinely dead* (the event bus, the durable
  command store, reserved interceptor scopes — those still go).
- CONTEXT.md gains no new domain term from this; it is a maintenance
  governance rule, recorded here so reviewers do not re-suggest deletion.

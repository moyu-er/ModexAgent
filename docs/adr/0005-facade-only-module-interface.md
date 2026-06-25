# Every module's public interface is its `__init__.py` (facade-only)

Today module interfaces are inconsistent: `adapters/__init__.py` is empty
(public classes only reachable via deep paths), `ioc/__init__.py` is a bare
docstring, while `sandbox`, `core`, and `memory` re-export large surfaces from
their `__init__`. Callers also reach across module seams into sibling files
(`from modex_agent.memory.core.layers import ...`) instead of through the module
interface. There is no single, predictable place to learn a module's interface.

## Considered Options

1. **Facade-only contract (chosen).** Each module's `__init__.py` is its sole
   public interface: it re-exports exactly the public symbols (declared in
   `__all__`), private helpers stay `_`-prefixed and unexported, and modules
   import one another only through `from modex_agent.<module> import ...` —
   never through sibling internal files. Directory *shape* may still vary (a
   module is as deep as its domain needs); the *interface contract* is uniform.

2. **Facade-only + enforce `<module>/base.py` for every ABC (deferred).**
   Adds a second rule: every module's ABC/interface lives in
   `<module>/base.py` (today ABCs are scattered — `core` keeps `Agent` /
   `LLMProvider` in topic files, only `sandbox`/`adapters` use `base.py`).
   Chosen as the eventual target, but deferred: too costly to do everywhere at
   once. Modules that already satisfy it can adopt it now; option 1 is shaped
   so the `base.py` convention can be layered on later without rework.

3. **Soft "stay thin" guidance.** Rejected: vague guidance yields inconsistent
   interfaces, which is the current state.

## Consequences

- The module `__init__` becomes the literal test surface and learning surface —
  a reader understands a module by reading its `__init__` only (deep module,
  thin shell).
- All cross-module imports that currently reach into sibling internal files
  must be redirected to the module facade. This is a per-module migration, done
  incrementally.
- The `base.py` ABC convention (option 2) remains open: nothing in this rule
  fixes ABC location, so a later ADR can require `<module>/base.py` without
  contradicting this one.
- `__all__` becomes load-bearing — every public symbol is listed, everything
  else is private by convention.

# Capability Author Guide

**Audience**: plugin authors shipping a set of components that belong together — a tool, its hook, its prompt guidance, its pool-level store — as one opt-in unit.

**Status**: implemented (ADR-0047; SPEC `docs/design/capability-bundles/SPEC.md`). The canonical runnable example is the integration test `tests/integration/plugins/test_tcap2_third_party_capability.py` — a complete third-party four-element capability (1 tool + 1 hook + 1 prompt section + 1 pool supply) reaching the production assembly product through plugin registration + a YAML reference alone, with zero framework code naming anything in the plugin.

---

## 1. When to write a Capability (and when not to)

Write a **Capability** when your components bind together: enabling one should enable the set, and the set has an internal coherence your users should not have to re-assemble by hand (a tool whose hook reads the tool's store; a prompt section that documents the tool). A capability packages:

- tool names (plus optional same-name tool replacements and tree-derived entries),
- hook names,
- prompt sections,
- a pool-level supply (stores/services/background workers),
- per-agent wiring objects,

behind one registration name that is also the declaration key.

**Do not write one for a single component.** A lone hook (or lone tool) continues to be a plain slot registration — that is a legitimate, complete plugin shape. The reference walkthrough is `examples/bot_project/plugins/reference_collector.py`: a `Plugin` subclass registering one HOOK-slot factory under the name `reference_collector`, referenced from YAML with `hooks: [+reference_collector]`. No capability, no enablement predicate — nothing about it needs bundling. Promote to a capability only when the second bound element appears.

The framework's own five bundled capabilities (`aci`, `ast_grep`, `todo`, `experience`, `subagents` — `src/modex_agent/plugins/defaults/capabilities/`) are worked examples of every shape: tools-only (`ast_grep`), tool+replacement (`aci`), full four-element (`todo`, `experience`), and tree-derived dynamic enablement (`subagents`).

## 2. The five-phase protocol

A capability is a subclass of `Capability` (`modex_agent/plugins/capability.py`). Only `assemble` is abstract; every other phase has a default. Two of the phases run at **compile time** and must be deterministic pure functions (they feed the spec-hash byte-stability contract — same declaration + same registry → identical compile product); two run at **assembly time** and may read the workspace/context chain.

| Phase | Method | When | Purity | Output |
|---|---|---|---|---|
| C0 enablement | `applies(view: AgentDeclarationView) -> bool` | compile, per agent | pure | default `False` (pure opt-in) |
| C1 contribution | `contribute(tree: TreePositionView, config) -> CapabilityContribution` | compile, before roster merge | pure | default empty |
| C2 binding | `bind(tree, config, final: FinalRosterView) -> CapabilityBinding` | compile, after roster merge | pure | default: contribution IS the binding |
| S supply | `supply(view: PoolSupplyView) -> CapabilitySupply \| None` | pool assembly | may read workspace | default `None` (no pool-level need) |
| A wiring | `assemble(binding, ctx) -> CapabilityWiring` (async) | per-agent assembly | may read the chain | **abstract — you must override** |

Key rules that follow from the purity contract:

- `applies`/`contribute`/`bind` must never read the clock, do IO, or touch global state. Their inputs are only the frozen views the compiler hands them.
- `applies` sees the agent's **declared** state (`AgentDeclarationView`: tree position `is_root`/`parent`/`children`/`peers`, plus the agent's own declared fields via `view.declared` — `tools`, `toolset`, `memory`, `mcp`, `use_terminal`, `execution_strategy`, …). It never sees the final roster or other capabilities' contributions — reading those would create an enablement loop and break hash determinism.
- External agents never run your predicate — they are structurally excluded from capability resolution (a non-empty explicit `capabilities:` block on an external agent is a V12 boot error).

## 3. The worked example (the T-CAP2 shape)

What follows is the four-element third-party capability proven end-to-end by `tests/integration/plugins/test_tcap2_third_party_capability.py`. One plugin file, four pieces.

### 3.1 Config model — frozen, closed

```python
from typing import ClassVar
from pydantic import BaseModel, ConfigDict

class ThirdPartyDemoConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")   # mandatory discipline

    greeting: str
```

Every capability declares `config_model: ClassVar[type[BaseModel]]`. It is validated **at compile time**: an unknown key in the YAML mapping is a loud boot failure, not a silent default. `frozen=True, extra="forbid"` is the discipline — the validated config also travels into the compile product (`AssemblySpec.capabilities`), so it must be a serializable value object. A knob-free capability reuses the shared empty `CapabilityConfig`.

### 3.2 The capability

```python
from modex_agent.plugins.capability import (
    Capability, CapabilityBinding, CapabilityContribution,
    CapabilitySupply, CapabilityWiring, PoolSupplyView,
    PromptSectionSpec, TreePositionView,
)

CAPABILITY_NAME = "thirdparty_demo"
TOOL_NAME = "thirdparty_note"
HOOK_NAME = "thirdparty_after_turn"
SECTION_ID = "thirdparty_demo.greeting"          # namespace convention: <cap>.<section>

class ThirdPartyDemoCapability(Capability):
    name = CAPABILITY_NAME                        # registration name = declaration key
    config_model: ClassVar[type[BaseModel]] = ThirdPartyDemoConfig

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        greeting = ThirdPartyDemoConfig.model_validate(config.model_dump()).greeting
        return CapabilityContribution(
            tools=(TOOL_NAME,),                   # enters the roster merge base
            hooks=(HOOK_NAME,),                   # enters the merged hook roster
            sections=(PromptSectionSpec(
                section_id=SECTION_ID,
                order=45,                         # order inside the selected fixed anchor
                config={"greeting": greeting},    # feeds assemble()
            ),),
        )
    # applies: inherited default False — pure opt-in
    # bind:    inherited default — no anchor, the contribution IS the binding

    def supply(self, view: PoolSupplyView) -> DemoSupply:
        greeting = view.entries[0].config["greeting"] if view.entries else ""
        return DemoSupply(store=DemoStore(greeting=str(greeting)))

    async def assemble(self, binding: CapabilityBinding, ctx) -> CapabilityWiring:
        for section in binding.active_sections:
            if section.section_id == SECTION_ID:
                return CapabilityWiring(
                    prompt_providers=(ThirdPartySectionProvider(greeting=str(section.config["greeting"])),),
                )
        return CapabilityWiring()
```

What the compiler does with this, in order:

1. **C0**: the agent's effective set = auto-apply ∆ the `capabilities:` override map. This capability's predicate is the default `False`, so it is present only where declared.
2. **C1**: `contribute` runs; the tool and hook names flow into the roster merge **base** — which means the ordinary `tools:`/`hooks:` `±` merge sees them (see §5).
3. **C2**: `bind` runs (here the inherited default); the surviving sections land in `binding.active_sections` and the vouched hooks in `binding.hooks`.
4. **S** (pool assembly): `supply` runs once per pool — but only if the capability is effective on at least one agent in that pool. Nobody enables it → no store, no service, nothing built.
5. **A** (per-agent assembly): `assemble` runs once per capability-effective agent; each active section produces exactly one `prompt_provider`, positionally. Providers render at the section's fixed `HEAD` or `TAIL` anchor, ordered by `order` within that anchor.

### 3.3 The pool supply and the loud read

```python
class DemoSupply(CapabilitySupply):
    def __init__(self, store: DemoStore) -> None:
        self.store = store

def require_demo_supply(pool_runtime: PoolRuntimeDeps | None) -> DemoSupply:
    supply = (pool_runtime.capability_supply.get(CAPABILITY_NAME)
              if pool_runtime is not None else None)
    if not isinstance(supply, DemoSupply):
        raise ValueError(
            f"{CAPABILITY_NAME} components require the pool supply "
            f"capability_supply[{CAPABILITY_NAME!r}] (DemoSupply); declare "
            f"capabilities: {{{CAPABILITY_NAME}: {{...}}}} on the referencing agent"
        )
    return supply
```

The supply is your pool-level singleton: build stores, services, and background workers here, keyed off the `PoolSupplyView` (which carries the effective `(agent, config)` entries plus distilled pool resources — `data_dir`, `runtime_dir`, `persistence`, `default_llm_provider`, the pool handle, the session tree, and more; raise loudly if a handle you need is `None`). If the supply owns a live background task, implement the async `start()`/`stop()` lifecycle: pool assembly starts every supply, and both teardown roads stop them.

**The loud read helper is the pattern to copy.** Every TOOL/HOOK factory and every `assemble` that consumes the supply type-checks it through a `require_*` helper whose error names the capability and the repair path. A roster-referenced component is never silently skipped — a bare reference to your tool without the capability fails at assembly with a message that tells the user exactly what to declare.

### 3.4 Registration

```python
from modex_agent.plugins.loader import Plugin, PluginRegistrationContext

class ThirdPartyDemoPlugin(Plugin):
    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_capability(CAPABILITY_NAME, ThirdPartyDemoCapability())
        ctx.register_tool(TOOL_NAME, ThirdPartyNoteToolFactory())      # reads the supply
        ctx.register_hook(HOOK_NAME, ThirdPartyAfterTurnHookFactory()) # reads the supply
```

Registration is one plugin, all slots: the capability **plus** the ordinary TOOL/HOOK factories for the components it contributes. Contributed names resolve through the same existing slots as any hand-declared name — there is no second resolution path. A contributed name whose factory is missing fails at assembly with the standard `ComponentNotFoundError` (late-binding); a `capabilities:` key with no registered capability fails **at boot** (V13 — the CAPABILITY slot is the only compile-time-resolved slot).

### 3.5 The declaration

```yaml
pool:
  name: thirdparty_pool
  agents:
    demo_main:
      toolset: none
      llm_provider: thirdparty_llm
      capabilities:
        thirdparty_demo:
          greeting: "hello from third-party YAML"
```

That is the whole user-facing surface: one mapping entry, config validated by your `config_model` at compile time.

## 4. Auto-apply: field and tree predicates

Leaving `applies` at `False` makes the capability declaration-only (the right default — availability ≠ enablement). If the capability should decide for itself, override `applies` with a pure predicate over the declaration view. The two landed shapes:

**Field predicate** — enable on a declared agent field:

```python
def applies(self, view: AgentDeclarationView) -> bool:
    return view.declared.use_terminal is True
```

**Tree predicate** — enable on topology participation (this is `subagents`, the shipped flagship):

```python
def applies(self, view: AgentDeclarationView) -> bool:
    return bool(view.children) or not view.is_root or bool(view.peers)
```

Auto-apply is auditable, not silent: every capability lands in the scope bill's per-agent `capabilities` list with a three-state entry — `auto` (predicate hit), `declared` (explicit override), `vetoed` (predicate hit but forced off) — and auto/declared entries carry the registration source (bundled / project / user / entry_points). A user can always force an auto-applying capability off per agent:

```yaml
capabilities:
  subagents: false      # beats auto-apply in both directions
```

## 5. The two veto altitudes

Users dismantle capabilities at two explicit altitudes, and your capability defines how each behaves:

| Altitude | Syntax | Effect |
|---|---|---|
| Capability-level | `capabilities: {cap: false}` | the package does not compile in — zero contributions, no supply, no boot-fail anchors |
| Component-level | `tools: [-x]` / `hooks: [-y]` | the package stays enabled; the named roster entry is vetoed by the ordinary ± merge (a wholesale unprefixed `tools:` list replaces the whole base, contributions included) |

Component-level veto is where your **C2 anchors** matter. `bind` runs after the merge and sees the final rosters (`FinalRosterView`), so it can enforce package coherence:

- **Boot-fail anchor** — `todo` requires both `todo_write` and `todo_read` to survive; `tools: [-todo_write]` raises `CapabilityError` at compile time (boot fail) naming the pool, agent, capability, the vetoed anchor, and the repair path.
- **Silent withdrawal** — `experience` anchors its review hook and injection section on the `experience` tool; when the tool dies (`tools: [-experience]` or a wholesale list) the binding drops them too, matching the historical minus-wins shapes without raising.

Pick deliberately and document the choice — both are correct; only silence is not.

### binding.hooks: vouching contributed hooks

`CapabilityBinding.hooks` is the tuple of hook names your capability vouches for after anchor gating. The compiler's hook gating runs after all binds: a **contributed** hook name survives `merged_hooks` iff at least one contributing capability's binding vouches it — gating only removes, never adds, and a user's handwritten `+name` entry is never removed by gating (the declaration owns its entries; the binding owns only its contributions). The inherited default `bind` vouches everything it contributed, which is right when your hooks have no anchor. Override `bind` to withdraw a hook when its anchor died (the `experience` shape above).

## 6. Sections: what to promise

A section is one `PromptSectionSpec` — `<capability>.<name>` id, integer `order`, `SectionPlacement`, and optional config dict. `HEAD` is the default fixed anchor after fork context and before core memory; `TAIL` is the fixed final system-prompt anchor for volatile reference catalogs. Sections are sorted by ascending `order` within their anchor; arbitrary insertion positions are not supported. When you write `assemble`:

- Build exactly one `SystemPromptProvider` for each active section and return providers in the same positional order as `binding.active_sections`; assembly validates the one-to-one mapping before placement and sorting.
- Derive the provider's **version from stable data** — a constant for static content, a content hash when content tracks a store. An unchanged version means zero re-fetches (prefix-cache hit semantics); wall-clock or per-turn state in the version defeats the cache.
- Emit empty content rather than raising when the section's data is empty — the pipeline skips empty sections.

## 7. The bare-tool degraded mode

Because contributed names are ordinary roster names, a user can reference your tool **without** the capability:

```yaml
tools: [+experience]        # no capabilities: {experience: {...}}
```

This compiles the tool and only the tool — no hook, no section, no supply, no background workers. `tools:` means "reference a single tool", nothing more. Your tool factory's own validation still runs at assembly: the factory reads the pool supply through the loud `require_*` helper, so a bare tool reference in a pool where nobody enabled the capability fails loudly at boot-assembly with the repair message — it never silently degrades into a half-working package. A bare tool in a pool where some *other* agent enabled the capability works (the supply exists), which is exactly the "shared pool store, surgical roster" use case.

## 8. Testing your capability

The T-CAP2 test is the template — it proves the full road with no mocks in compile/assembly:

1. Register the plugin (bundled defaults + project plugins through the real `ComponentRegistryLoader`, your plugin through `PluginRegistrationContext` — the same registration face the loader drives).
2. Load an inline YAML declaration through `load_scope_declaration`, compile through `compile_scope` with the registry.
3. Assemble through the real `AssemblyPipeline` (all four stages).
4. Assert against the assembled product: the tool executes against the pool store, the hook fires through the real `HookRunner` dispatch, the section renders in the context manager's prompt (byte-stable across loads), the supply is the same instance the tool and hook used.

Also cover your failure paths — they are contracts:

- unknown config key → boot fail (`config_model` validation at compile time),
- unregistered capability name in YAML → V13 `ComponentNotFoundError` at boot,
- explicit `capabilities:` on an external agent → V12,
- your anchor vetoed (e.g. `tools: [-your_anchor]`) → `CapabilityError` naming the repair path,
- a component reference without the capability effective → the loud `require_*` raise at assembly.

## 9. Quick reference

| You write | The framework does |
|---|---|
| `name` + `config_model` (frozen, `extra="forbid"`) | compile-time config validation; config travels in the compile product |
| `applies(view)` (default `False`) | C0: effective set = auto ∆ `capabilities:` overrides; three-state bill entries |
| `contribute(tree, config)` (default empty) | C1: names enter the roster merge base; replacements applied post-merge; derived entries carry origin+targets |
| `bind(tree, config, final)` (default pass-through) | C2: anchors + `binding.hooks` vouching; `CapabilityError` = boot fail |
| `supply(view)` (default `None`) | once per pool, iff effective somewhere; start/stop lifecycle; indexed in `capability_supply` |
| `assemble(binding, ctx)` (abstract) | per capability-effective agent; sections → prompt anchor; artifacts → factories via the context chain |
| `ctx.register_capability(name, cap)` + slot factories | registry storage, source priority, late-binding resolution for components |

**References**: protocol types — `src/modex_agent/plugins/capability.py`; the five bundled packages — `src/modex_agent/plugins/defaults/capabilities/`; design SPEC — `docs/design/capability-bundles/SPEC.md`; decision record — ADR-0047; the runnable proof — `tests/integration/plugins/test_tcap2_third_party_capability.py`.

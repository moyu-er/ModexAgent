# Closure Matrix — Scope Assembly Design

Inputs: ADR-0042, ADR-0043, SPEC.md, CONTEXT.md terms. Structure map: `scope-assembly-closure-map.md`.
Dimension selection: data-flow / interface / lifecycle / convergence selected (user-requested logic/data-flow/convergence + lifecycle objects); state-machine skipped — no >2-value new states, no transition verbs, no new recovery flows (S1-S3 binary/static, S4 untouched by design).
Ground truth verified via codegraph/grep by four parallel dimension tracers; findings re-verified by main agent against committed design docs.

> **Implementation status (2026-08-22)**: implemented — tickets 01-19 all closed. Every design-time gap/ambiguity recorded below was resolved pre-implementation (see the Resolution log at the end): F2/F3/F4/F7/F8/SG1/A1-A4 by SPEC edits, F5+F1 by user decision (lazy materialization + `graphs:` dropped + V10), F6 as the documented D1 deferred TODO (§10 — turn-bracket completion is the hard precondition before any `max_materialized` activation; capacity stays dormant). Wave completion: W1 (tickets 01-04), W2 (05), W3 (06/07/09/10), W4 (08/12/13), W5 (14/16/18), W6 contract (11/15/17), W7 doc-sync (19). Implementation errata: SPEC §13 (Errata-1~8). This file is the tracked copy of the pre-implementation closure audit; the original working copy lives in `docs/handoff/` (gitignored).

## Final net accounting (2026-08-22, F1 close-out — supersedes the "net-zero accounting closed at W6" reading)

Final ledger vs BASELINE_SHA `4dafb4b` (dual pathspec, `git diff --numstat` cumulative): src/modex_agent **+4114/−2390 (net +1724)**; examples/bot_project/bot **+2869/−2111 (net +758)**; **total net +2482**. SPEC §9.1-5 "总账净减或持平" (P5 / plan Must-have) **NOT met** — the only unmet acceptance criterion. Deletion ledgers fully cashed (grep-clean) and convergence substantively delivered (single road + guards); the net increase is the new declaration subsystem's permanent face (scope/ ~2,350 LOC + context chain + derived-communication FW factories + scope-path machinery) exceeding the deleted glue (−4501) — a plan forecast failure, not an incomplete migration. Per-wave increments (plan-wave basis): W1 +922 (exempt) / W2 +552 / W3 +801 / W4 +741 / W5 +1394 / W6 −1928 / W7 0. Full record: SPEC §13 Errata-8.

## Closure matrix

| dimension | path (map item) | checkpoints | status | note |
|---|---|---|---|---|
| data-flow | D1 ScopeSpec tree | YAML parse → validate → compile → AssemblySpec; retention for WebUI | closed | WebUI data source = recompute-from-YAML (see F2 fix); no in-memory holder required |
| data-flow | D2 unified AgentSpec | parse → position derivation → compile | closed | Main/Sub deletion ledgers W1/W3 |
| data-flow | D3 position defaults | derivation at compile → consumed by boot/materialize | assumption-closed | assumption: `eager:` flag consumed by existing pool-boot and dispatch-materialize machinery (design keeps both, unnamed explicitly) |
| data-flow | D4 task availability list | compile derivation → materialize target store → tool consumption | closed | mechanism shape ambiguity A2 (registry component vs materialize injection) |
| data-flow | D5 Profile | boot load → compile merge (defaults ← profile ← local) | closed | V7 enforces single level; holder ambiguity A3 (minor) |
| data-flow | D6 context chain | frozen carriers → factory signatures → typed reads | closed | extends verified 3-layer AssemblyContext (context.py:97); carriers transient |
| data-flow | D7 AssemblySpec | compile output → pipeline stages → assemble_native_agent | closed | anchors verified (SpecBuilder, native_core.py:239) |
| data-flow | D8 YAML declaration | WebUI write-back (PoolEditor pattern verified) → boot reload | closed | dynamic scope creation = documented future path (deferred) |
| data-flow | D9 spec-hash + generation | computed; no consumer in v1 | deferred | N2 hot reload; hash-stability test is the only consumer |
| data-flow | D10 validation outcomes | validator → startup error → boot abort | gap | **F3** (approval rule missing from §7); **F4** (V6 input) |
| data-flow | D11 pool `graphs:` list | declared; transport/consumer/validation undefined | gap | **F1** |
| data-flow | D12 provenance | merge-time origin; consumer = WebUI bill view; transport undefined | gap | **F2** |
| data-flow | D13 peer links | declaration → V5 → send_to_peer registration → bus_ref delivery | closed | ADR-0019 anchors verified (CommunicationTarget.bus_ref, PeerNormalStrategy); **SG1** for post-W5 acquisition home |
| lifecycle | S1 eager/lazy | existing machinery verified; design adds position default + override | closed | |
| lifecycle | S2 restart-effective | verified `restart_required` machinery; config static per run | closed | |
| lifecycle | S3 LRU + turn-protection | machinery verified (registry.py:121); protection call-site submit-only; cap never set in bot wiring | gap | **F6** |
| lifecycle | S4 session tree states | untouched by design | closed | verified SessionTreeManager/stores |
| interface | I1 ScopeTreeValidator | caller = boot (W3 wiring, module unnamed); params = tree + profile store + derived toolsets | gap | **F4** (V6 param), **F3** (missing rule); caller assumption recorded |
| interface | I2 ScopeCompiler | caller = create_pool successor (W3); output = per-agent AssemblySpecs | closed | |
| interface | I3 layered ComponentFactory | extends verified ABC (plugins/abc.py:95); all 28 impls migrate in W1/W3 | closed | |
| interface | I4 registry + loader | verified: 10 ComponentSlot values, 4 PluginSource values, fault-isolated loader | closed | |
| interface | I5 pipeline + native core | verified: 4 main stages + unified core, 16 callers | closed | doc nit: SPEC says "7 槽位解析", code resolves 5 in native_core |
| interface | I6 ExecutionStrategy | verified ABC + StrategyAssembly; external fields None for memory/dream | closed | doc nit: design says `assemble()`, code is `assemble_main()` |
| interface | I7 BotAgentNode | references agent by name → pool.get() → RuntimeError if not resident | gap | **F5** (lazy leaf × graph reference) |
| interface | I8 register_resident | verified pool.py:168; eager + lazy + external paths all flow through it | closed | |
| interface | I9 SessionTreeStore | verified ABC + InMemory/LocalFile/Sqlite; manager per-pool | closed | |
| interface | I10 WebUI write-back | PoolEditor pattern verified (writes config/pools YAML); new scope-tree editor = W5 | assumption-closed | assumption: new editor follows existing write-back pattern |
| interface | I11 ResourceFactory seam | verified generic seam; future second-host entry | closed | N4 deferred |
| lifecycle | O1 ScopeRegistry | generalizes verified WorkspaceRegistry[R]; inherits submit-only protection window + dormant cap | gap | **F6** |
| lifecycle | O2 scope-path resolution | analog of verified per-workspace resources; routing store stays service-level | closed | PoolRouter's session-routing store ownership untouched (service-level, verified) |
| lifecycle | O3 resident cache | verified: one instance per agent name; release at pool shutdown; no accumulation; evict-failure retains for retry | closed | |
| lifecycle | O4 shared infra | MCP process-level (verified); inbox/bus per-pool, broker per-workspace, rebuilt on eviction | gap | **F7** (P2 misclassification) |
| lifecycle | O5 per-agent target store | materialize-time creation for nodes with children; release with agent instance | gap | **F8** (send_to_agent + pool-store fate) |
| lifecycle | O6 PoolContext payload | assembly-window carrier; fields' lifetimes owned by layers | assumption-closed | assumption: factories capture fields, carrier not retained post-assembly |
| lifecycle | O7 profile store | loaded at boot; holder unnamed | ambiguity | **A3** |
| convergence | C1 tool assembly | current dual paths verified (builders.py:423-469 direct construction; `if assembly_spec is None` fallback); W2 deletes | closed-in-design | note: W2 ledger must include the `assembly_spec is None` branch |
| convergence | C2 comm tool registration | current: BIZ main-only (communication.py:100) + FW per-subagent store (template.py:510); target: tree-derived per-agent | gap | **F8** |
| convergence | C3 addressing ×3 | verified all three; W5 converges; deletion ledger covers | closed-in-design | |
| convergence | C4 memory presets | verified presets.py:25/66; position derivation replaces | closed-in-design | |
| convergence | C5 external/native | 3 branch points verified + architecture test documents them; strategy-owned target | closed-in-design | |
| convergence | C6 bash fallback | verified FW factory: bash degrades to SubprocessTool; process/terminal raise; PoolAssembleStage enforces | closed | justified capability degradation, same Tool interface |
| convergence | C7 defaults resolution | tool_preset vs Profile | ambiguity | **A1** |
| convergence | C8 registration timing | caller-side branch today; position default + override target | closed-in-design | |
| convergence | C9 same-pool NORMAL | zero shipped users verified; removal safe | closed-in-design | note: comm_kind.py re-export shim residue to sweep with C9 work |
| convergence | C10 spec type split | verified specs.py:89/167; W1 unifies | closed-in-design | |
| convergence | C11 declaration vs session tree | permission (compile-time) vs record (runtime) — different execution models, explicitly documented | closed | justified divergence |
| convergence | C12 graph/session mode | ADR-0039 pipeline verified as residence; orthogonality justified (same agent, two consumption modes) | gap | **F1** (graphs: list) |
| convergence | C13 special agents | code-constructed (pool_wiring.py:96 verified); shared assembly core; N8 documented | closed-under-interpretation | bounded divergence, user decision, flip condition documented; strict deletion-test reading noted |
| convergence | C14 use_terminal | verified: flag builds manager only; roster drives tools; factories enforce invariant | closed | |

## Seams (Phase 2)

| seam | check | result |
|---|---|---|
| data-flow ↔ lifecycle | eviction releases holders while data in-flight | → F6 (turn cancellation); F2 (provenance retention vs restart) |
| interface ↔ lifecycle | callers hold references across releases | BotAgentNode name-resolution across eviction/lazy → F5 + F6; factory-captured refs consistent under same-workspace eviction invariant |
| data-flow ↔ interface | interface returns feed consumption | V6 consumes compiler-derived toolset → F4; task tool shape → A2 |
| state-machine ↔ data-flow / lifecycle | (skipped dimension; S1-S3 covered above) | n/a |
| error propagation | startup validation → boot abort (closed); eager factory errors → pool-creation failure at boot (closed, PoolAssembleStage); lazy materialize factory errors → first-dispatch turn error via existing turn-error machinery (assumption-closed); eviction mid-turn → cancelled task (F6); graph miss → RuntimeError (F5); WebUI write-back errors → existing PoolEditor pattern (closed) | all abnormal paths land |

## Findings (verified against cited locations)

**F1 — `graphs:` list has no consumer, transport, or validation.**
Location: SPEC §4 axis 3; §7 (V1-V8 contain no graphs rule).
Consequence: users declare `graphs: [X]` and nothing reads it — dead configuration; graph specs referencing pool P's agents are never cross-checked against P's list, so the declaration has no runtime effect at all.
Fix: define the consumer (WebUI graph-picker per pool in W5, and/or startup cross-check against GraphSpecLoader output) + add validation rule V9 (entries must resolve to existing graph specs). Or drop the field until a consumer exists.

**F2 — provenance has no transport to the WebUI; stale-view risk.**
Location: SPEC §3.4 rule 3; W5 deliverable "profile 账单视图".
Consequence: if provenance is retained from boot-time compile, a WebUI edit (write-back to YAML, not yet restarted — S2) makes the bill view show stale provenance contradicting on-disk declaration; if nothing is retained, the W5 deliverable has no data source.
Fix: specify transport = recompute-from-YAML per request (pure function per P1 makes this cheap and always-fresh), served via a REST endpoint in W5. One sentence in §3.4.

**F3 — non-root approval validation referenced but missing from the "complete" rule set.**
Location: SPEC §11 (ADR-0008 row: "非根声明 approval → v1 校验报错") vs §7 ("校验规则全集" V1-V8, no approval rule).
Consequence: an implementer building ScopeTreeValidator from §7's complete list omits the check; unified AgentSpec exposes approval to all nodes (today the type split enforces main-only) → a subagent with approval enabled boots, its turn suspends awaiting an approval that the main-only approval renderer never surfaces → subagent turn hangs.
Fix: add the rule to §7 (V9/V10: 非根节点声明 approval → 启动报错).

**F4 — V6 requires compiler-derived data but sits in the pre-compilation validator.**
Location: ADR-0042 Decision ¶2 (validated THEN compiled — sequential) vs SPEC §7 V6 ("最终生效工具集") + §3.2 ("推导发生在 ScopeCompiler 编译期").
Consequence: as specified, V6 cannot see the effective toolset at validation time; the implementer either duplicates merge logic in the validator or silently weakens the check to declared-only tools — the profile-wholesale-list case (exactly what V6 was added for: a `tools:` replacement dropping task) is missed → silent orphan subtree, V6's purpose defeated.
Fix: specify that V6 runs on the derived effective toolset — ScopeCompiler emits effective toolsets and V6 consumes them (post-derivation check), or the validator's input contract includes (tree, profile store, derived toolsets).

**F5 — graph-referenced lazy agents fail resolution at graph execution time.**
Location: SPEC §9.1 acceptance 1 ("叶子被业务图引用") + ADR-0043 (BotAgentNode bridge) vs code: BotAgentNode._resolve_agent_instance → pool.get(name) → RuntimeError when not resident.
Consequence: a graph referencing a never-dispatched lazy leaf raises RuntimeError at first graph execution — late, runtime-visible failure instead of startup validation; the design's own acceptance criterion exercises this exact path.
Fix: pick one in SPEC §4/§9: (a) declaration-level rule — every agent referenced by a graph spec in the pool's `graphs:` list derives `eager: true` (or startup error); (b) BotAgentNode materializes via template on miss. (a) aligns with declaration philosophy; reinforces F1's fix.

**F6 — turn-protection window is submit-only; cap dormant; design claims protection.**
Location: SPEC §3.1 ("同一台机器，作用域通用" — ScopeRegistry inherits turn-protection) + S3 claim vs code: `WorkspaceMessageDispatcher.dispatch_once` brackets only `route_one` (= `submit_input`, fire-and-forget inbox write); agent turns run afterward in InboxPoller tasks with no begin_turn; `stop_poller` cancels in-flight turn tasks; `max_materialized` is never set in bot wiring (machinery dormant in production).
Consequence: once ScopeRegistry gets a cap (the point of multi-live memory management), an in-flight agent turn — poller-driven session turn, async subagent turn, or graph-orchestrated turn — has in-flight count 0, workspace is evictable, and `_stop_pools` cancels the running turn mid-execution. The design's "turn-protection exists" claim is machinery-true but call-site-incomplete.
Fix: define turn bracketing at scope level in SPEC §3.1/W5 — InboxPoller (and graph orchestrator) call begin_turn/end_turn around turn execution, or eviction checks poller in-flight tables; specify the cap knob before activating ScopeRegistry eviction.

**F7 — P2 misclassifies inbox/bus/broker/memory handles as process-level never-rebuilt.**
Location: SPEC §2 P2 ("进程级单例（MCP 连接 / inbox / memory 句柄，永不重建）") vs code: inbox/bus are per-pool (built in create_pool), broker per-workspace (built in _build_resources), memory system per-pool — all rebuilt on workspace eviction + re-materialization; only MCP connections are process-level (McpConnectionRegistry on BotService; facade release detaches, real close at registry shutdown).
Consequence: an implementer following P2 hoists inbox/bus/broker to process singletons (breaking per-workspace isolation: cross-workspace message bleed) or hits a spec-vs-reality contradiction in W5; the load-bearing property is "shared across assemblies within the owning scope, reused across calls" — not "process-level, never rebuilt".
Fix: reword P2's first layer: "scope-owned shared infra — MCP connections (process-level), broker (workspace-level), inbox/bus/memory handles (pool-level); shared across all assemblies within their scope, rebuilt only on scope eviction".

**F8 — send_to_agent and the pool-level CommunicationTargetStore have no place in the communication convergence.**
Location: SPEC §5.2 (before/after covers task + send_to_peer only) vs code: subagent consultation already uses a per-subagent `CommunicationTargetStore(for_subagent=True)` registered at template.materialize (template.py:510-515, FW baked default); pool-level store populated by BIZ wiring (resources.py Phase 2).
Consequence: W4 deletes "register_communication_tools 的 main 独占逻辑" and W5 deletes the wiring modules that populate the pool-level store — send_to_agent's registration path and the pool store's fate are unspecified; risk that subagent consultation loses parent resolution (leaf agents have no per-agent store under O5) or that two store populations diverge.
Fix: add send_to_agent to §5.2's derivation table (non-root nodes get send_to_agent with parent target from AgentContext parent reference / per-agent store); name the pool-level store's fate in W4's deletion ledger.

## Suspected gaps

**SG1 — peer link → bus_ref acquisition home after W5.** Location: SPEC §5.2 (links derive send_to_peer registration at materialize) + §11 (ADR-0019 semantics kept) vs W5 deletion (workspace wiring modules that currently acquire bus references die). Traced: derivation named, acquisition site unnamed. Missing: where link declarations resolve to live peer bus/tree references in the target state. Why consequence unclear: several natural homes exist (PoolContext carrying peer refs via scope-path resolution; assembly-time link resolution) — a missing specification rather than a broken path, but W5 cannot complete without it.

## Ambiguities

**A1 — tool_preset fate.** Location: SPEC §3.4 ("推广到整个 agent 配置") vs §3.2 table ("工具集默认: pool 级 preset") + deletion ledgers (tool_preset absent from W1-W5). Interpretations: (a) field dies, values ship as FW standard profiles; (b) field survives alongside Profile. Impact: (b) fails the deletion test (a profile carrying tools does what tool_preset does) → two defaults mechanisms for one concern; V6's effective-toolset computation must consult both. Recommend stating (a) explicitly in W1's ledger.

**A2 — task tool mechanism shape.** Location: SPEC §3.2 (compile-time derivation; V6 checks final toolset) vs O5/§5.2 (materialize-time target store + task instance). Interpretations: (a) task is a named TOOL-slot component in compiled spec.tools (registry-resolved); (b) task injected at materialize outside the toolset. Impact: (a) needs a task factory in the TOOL slot + store wiring at assembly; (b) needs V6 to define "effective toolset" as including injected tools; W4's implementation shape differs materially.

**A3 — profile store holder.** Location: SPEC §3.4/O7. Interpretations: process singleton vs compiler-owned load-time input. Impact: minor — provenance query surface (F2) needs a queryable path either way.

**A4 — same-workspace peer rule for pool-rooted declarations.** Location: §3.1 ("单 pool 无 workspace？声明 pool 为根作用域") × V5 (same-workspace). Interpretations: pool-as-root declarations cannot have peers vs implicit default workspace. Impact: V5's applicability to workspace-less roots; one-line fix in §7.

## Completion check

1. Every map item (D1-D13, S1-S4, I1-I11, O1-O7, C1-C14) has ≥1 matrix row ✓
2. Every row has a status ✓
3. Every gap row links a finding meeting the bar (F1-F8) ✓
4. Every assumption-closed row records its assumption ✓
5. Seams traced for all selected dimension pairs + error propagation ✓
6. Every finding re-verified against cited locations ✓ (design docs in-context + subagent code verification)

**Verdict: the design does NOT fully close. 8 findings, 1 suspected gap, 4 ambiguities.** All are specification-completeness gaps in the design text (fixable by SPEC edits), not structural contradictions — the architecture (scope tree, dual execution models, convergence direction) survives closure intact.

## Resolution log (post-review fixes applied)

Resolved by SPEC/ADR edits (autonomous — unique answer or mechanically derived from the design's own principles):

- **F2** → §3.4 rule 3: provenance recomputes from YAML per request via W5 REST endpoint; no boot-time cache (staleness with S2).
- **F3** → §7 stage 2 gains V9 (non-root approval → startup error).
- **F4** → §7 restructured two-phase: declaration-shape rules pre-derivation, effective-value rules (V6/V9) consume compiler output; ADR-0042 V6 sentence amended.
- **F7** → §2 P2 reworded: scope-owned shared infra (MCP process-level / broker workspace-level / inbox+bus+memory pool-level, rebuilt on scope eviction).
- **F8** → §5.2 rewritten: three communication tools (task/send_to_agent/send_to_peer) converge to one derivation path; pool-level CommunicationTargetStore added to W4 deletion ledger.
- **SG1** → §5.2: peer link bus/tree references acquired at materialize via scope-path resolution from the owning workspace's resource bundle (FW-ization of existing Phase 2 wiring).
- **A1** → §3.4: tool_preset field dies (deletion test); values land as position-derived defaults (root=full, non-root=read-write); W1 ledger updated.
- **A2** → §5.2: communication tools are compiler-injected effective-toolset entries resolved via TOOL-slot FW factories reading per-agent stores from AgentContext; V6 checks derived spec.tools.
- **A3** → §3.4: profile store boot-loaded, process-lifecycle holder.
- **A4** → §7 V5: pool-rooted declarations (no workspace layer) cannot declare peers in v1.
- Doc nits: "7 槽位"→5; `assemble()`→`assemble_main()`.

Resolved by user decision (F5/F1 — graph-referenced agent materialization):

- **F5 + F1** → user decision: graph-referenced agents materialize on inbox consumption (same lazy pattern as session mode — mechanism already implemented); BotAgentNode's upfront `_resolve_agent_instance` pre-flight relaxes (deliver anyway, poller cold-starts, description from compiled declaration); `graphs:` pool field **dropped** (no consumer — F1 resolved by deletion); V10 startup cross-check (graph node references × declaration tree) catches typo'd names at boot. Supersedes the originally recommended option (a) (eager derivation) — the user's semantics is strictly more convergent: it deletes the graph-entry divergence instead of adding an eager-derivation rule, and preserves lazy semantics. SPEC §4 axis 3, §7 V10, W4 updated.

- **F6** → user decision: deferred as documented TODO — SPEC §10 reserved-seams table gains an "逐出保护窗口补全" row (turn bracketing must move from message submission to turn execution before any capacity activation; the capacity knob is the hard precondition trigger); W5 wave table gains a D1 deferred row clarifying W5's acceptance criteria do NOT include capacity activation. Design text no longer claims protection as-is; the latent hazard is recorded. Not resolved in code — deliberately out of scope for current waves.

All 8 findings now closed (6 by SPEC/ADR edits — F2/F3/F4/F7/F8 plus SG1; 2 by user decisions — F5+F1 bundled, F6 as documented TODO). 4 ambiguities resolved (A1-A4). Design closure status: **closed**.

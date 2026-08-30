# Dual Execution Models — Actor Sessions and Dataflow Graphs

## Context

Once the scope declaration tree (ADR-0042) unifies agent definitions, the natural question is whether modex_graph should also schedule and orchestrate agents in session mode — inter-agent dispatch as graph edges, the engine as the orchestrator. Examined over two rounds; the answer is no.

## Decision

**Session mode stays an actor model; graph mode stays a dataflow model; the declaration is shared; `BotAgentNode` is the only bridge.**

Session-mode agents are long-lived actors: inbox (mailbox), session memory (state), human input admitted at arbitrary boundaries, LRU eviction, days-long lifetimes. The `task` tool is asynchronous dispatch — a sender may dispatch to any number of subagents without waiting; results flow back through the inbox (fold-in mid-turn, new turn between). Parallel dispatch already exists; there is no limitation to remove. Graph-mode instances are declared DAGs with terminal states, deliver-based routing, and engine-driven recovery. When a workflow needs a compile-time-declared join ("wait for these three results"), it is a business graph whose nodes reference the same agents by name — the `BotAgentNode` bridge already provides this.

Unifying session orchestration onto the engine would require capabilities it deliberately lacks — dynamic edge minting (dispatch targets are runtime decisions), immortal instances, admission at arbitrary boundaries — i.e., an actor model re-implemented badly on a dataflow engine.

Flip condition: tree dispatch itself needing compile-time-declared control flow (forks/joins inside conversation trees). Even then, prefer an orchestrator node invoking a business graph over graphifying the orchestration layer.

## Considered Options

- **Unify session-mode orchestration onto modex_graph** — rejected: semantic mismatch above; would rewrite the multi_agent runtime.
- **A new pipeline-style config scheme for inter-agent chaining** — rejected as a false dichotomy: the actor machinery already exists and the declaration tree already carries the chain; "chaining" lives in the declaration, not in a scheduler.

## Consequences

modex_graph receives zero changes from the assembly redesign. Turn-internal execution remains the ReAct graph (status quo). Declaration trees and business graphs may render in similar WebUI canvases but never merge — one is compile-time declaration, the other runtime execution.

Implemented 2026-08-22 (tickets 01-19 closed); the code-anchor audit confirming every claim above is in `docs/design/scope-assembly/SPEC.md` §13 (Errata-7).

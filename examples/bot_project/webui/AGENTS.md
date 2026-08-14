<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-08 -->

# webui

React frontend for the ModexAgent bot. Vite + TypeScript + Tailwind CSS. Connects to the bot's WebUI backend (aiohttp) via REST API and WebSocket. All color tokens live in `src/index.css` `:root` / `.dark` blocks, mapped through CSS variables — edit colors once, never in the tailwind config. The current design system is "Teal & Ember Console" (see the Design System section below). Graph visualization IA redesign spec: `docs/design/graph-visualization-redesign/PRD.md` (project-level).

## Key Files

| File | Description |
|------|-------------|
| `package.json` | Dependencies and scripts (`dev`, `build`, `preview`, `test`) |
| `vite.config.ts` | Vite build config with proxy to backend |
| `vitest.config.ts` | Vitest test configuration (React hook / reducer unit tests) |
| `tailwind.config.js` | Tailwind CSS configuration — maps CSS variable tokens to utility classes |
| `postcss.config.js` | PostCSS plugins |
| `tsconfig.json` | TypeScript configuration |
| `index.html` | HTML entry point (preloads Geist + Geist Mono, dark-mode FOUC guard) |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `src/` | Application source (see below) |
| `dist/` | Built static assets (auto-generated, served by backend at `/webui/`) |

## src/ Structure

| File | Description |
|------|-------------|
| `App.tsx` | Root component — manages conversations, pools, workspace state, sidebar resize |
| `main.tsx` | React entry point |
| `index.css` | Global styles + single source of truth for the Teal & Ember palette (CSS variables in `:root` / `.dark` blocks) — tokens, radii, shadows, motion, component classes |
| `vite-env.d.ts` | Vite type declarations |
| `components/ChatView.tsx` | Chat area — message list + input box |
| `components/MessageBubble.tsx` | Individual message rendering (text, reasoning, tool calls) |
| `components/ReasoningBlock.tsx` | Collapsible reasoning/thinking block |
| `components/ToolTraceCard.tsx` | Tool call result card |
| `components/TodoPanel.tsx` | Task list panel derived from tool_call_end events |
| `components/Sidebar.tsx` | Conversation list, pool selector dropdown, workspace indicator, recent workspaces |
| `components/SessionTree.tsx` | Hierarchical session tree with parent–child relationship display |
| `components/WorkspaceBrowser.tsx` | Modal directory browser for workspace switching |
| `components/settings/SettingsView.tsx` | Settings shell — sidebar nav + routed sub-views (ModelEditor, PoolEditor, GlobalMcpView, GlobalSkillsView, ConfigForm) |
| `components/settings/ModelEditor.tsx` | Multi-provider/multi-model editor with nested card hierarchy |
| `components/settings/PoolEditor.tsx` | Pool tree editor — main agent + subagent cards |
| `components/settings/PoolsView.tsx` | Two-pane pool list + editor with filter search |
| `components/settings/GlobalMcpView.tsx` | Global MCP server registry — collapsible cards with KeyValueEditor |
| `components/settings/GlobalSkillsView.tsx` | Global skill manager — upload, search, inline detail pane |
| `components/settings/AgentMcpSelector.tsx` | Compact popover MCP checklist per agent |
| `components/settings/AgentSkillSelector.tsx` | Compact popover skill checklist per agent |
| `components/settings/ConfigForm.tsx` | Generic field renderer for singleton config domains |
| `components/ui/SectionLabel.tsx` | Shared section eyebrow (Geist Mono 10px uppercase) — used across all settings tabs |
| `components/ui/KeyValueEditor.tsx` | Postman-style key/value row editor (controlled component) |
| `components/graphs/GraphSpecListPage.tsx` | Graph spec list — MiniTopology thumbnail + metadata per row |
| `components/graphs/GraphSpecEditor.tsx` | Split-pane spec editor — CodeMirror YAML + live topology preview + run |
| `components/graphs/GraphSpecDetail.tsx` | Spec detail view — topology preview + instance list + FAB new-instance modal |
| `components/graphs/GraphInstanceDetail.tsx` | Instance detail view — conversation flow + re-invoke composer + Run Graph modal (live topology, controls, event timeline, deliver) |
| `components/graphs/GraphSpecInstanceRow.tsx` | Instance row for spec detail's instance list — #id + colored status badge + progress/elapsed + relative time + status-colored MiniTopology (session-row hover) |
| `components/graphs/shared.tsx` | Shared graph UI — GraphStatusBadge + buildNodeStatusMap + formatGraphApiError |
| `components/graphs/topology/TopologyCanvas.tsx` | SVG canvas — viewBox auto-fit, wheel zoom, drag pan, legend overlay |
| `components/graphs/topology/GraphNode.tsx` | SVG node — glyph + name + sub-label + status dot, dual-channel status coloring |
| `components/graphs/topology/GraphEdge.tsx` | SVG edge — border-strong stroke + arrowhead + highlight state |
| `components/graphs/topology/DeliverPulse.tsx` | Deliver pulse animation — brand-bright dot travels along edge path, reduced-motion fallback |
| `components/graphs/topology/ActiveNodeRing.tsx` | Running-node pulsing ring — outer rect stroke, CSS `graph-ring-pulse` animation |
| `components/graphs/topology/MiniTopology.tsx` | 80×24px thumbnail — simplified topology, >8 nodes fold to `···` |
| `components/graphs/topology/layout.ts` | dagre TB layout — spec → node positions + edge paths |
| `components/graphs/topology/miniLayout.ts` | Simplified layout for MiniTopology (fixed 80×24 coordinate space) |
| `components/graphs/detail/NodeDetailPanel.tsx` | Sidebar — selected node details (type, pool, status, invocation, open session) |
| `components/graphs/detail/InstanceSummary.tsx` | Sidebar — instance summary + progress ring + graph-level result |
| `components/graphs/detail/EventTimeline.tsx` | Sidebar — vertical event timeline with inferred/real events |
| `components/graphs/yaml/YamlCodeEditor.tsx` | CodeMirror 6 wrapper — lazy import, YAML highlight, lint gutter, Teal & Ember theme |
| `components/graphs/yaml/parseGraphSpec.ts` | YAML → structured topology model (ParsedGraphTopology), structured parse errors |
| `hooks/useWebUIStream.ts` | WebSocket hook — manages connection, `request_id`-based optimistic message dedup, streaming events, message history |
| `hooks/useWebUIStream.reducer.ts` | Pure reducer — session-scoped event filtering, `error` event → system notice, `user_message` echo dedup via `_request_id` metadata |
| `hooks/useWebUIStream.reducer.test.ts` | Reducer unit tests (session isolation, error handling, request_id matching) |
| `hooks/useGraphExecution.ts` | Graph execution hook — 2s polling + WS mode, node status diff, deliver pulse triggers, derived event timeline |
| `hooks/useGraphExecution.diff.ts` | Pure diff logic — `diffNodeStatuses` transition table (§9.3), separated from React lifecycle |
| `lib/api.ts` | REST API client (fetchSessions, fetchPools, createConversation, etc.) |
| `lib/ws-client.ts` | WebSocket client with action/attach protocol |
| `lib/graphsApi.ts` | Graph REST API client — specs CRUD, instance lifecycle, events, deliver, topology |
| `types/events.ts` | TypeScript event type definitions matching backend events.py |

## Scripts

| Command | Purpose |
|---------|---------|
| `npm run dev` | Start Vite dev server with backend proxy |
| `npm run build` | Production build to `dist/` |
| `npm run preview` | Preview production build |
| `npm test` | Run Vitest unit tests — 1018 tests across 96 files |

## For AI Agents

### Working In This Directory
- `npm run dev` starts the dev server with proxy to backend.
- `npm run build` outputs to `dist/`, which is served by the backend at `/webui/`.
- `npm test` runs Vitest — 1018 tests across 96 files covering hooks, reducers, UI primitives, dropdowns, boot, settings views, and all graph visualization components (topology, deliver pulse, diff, YAML editor, run-graph/new-instance modals, spec editor, instance/spec list pages).
- The frontend has **no direct pool switching** for existing conversations — it's purely a display filter in the sidebar dropdown.
- Workspace switching is done via `WorkspaceBrowser` → `POST /api/workspace/cd`.
- `useWebUIStream.ts` is the core hook — it handles WebSocket lifecycle, optimistic messages (`request_id`-based dedup), and streaming state.
- **Message dedup**: The hook generates a `crypto.randomUUID()` as `request_id`, adds it to the WS payload and an optimistic message. When the server echoes the `user_message` event with matching `_metadata._request_id`, the reducer updates timestamps instead of adding a duplicate.
- **Error display**: Backend errors (unsupported commands, rejected operations) arrive as `error` events. The reducer surfaces them as system-role messages with `⚠` prefix — visible in-chat, not persisted.
- **Session isolation**: `useWebUIStream.reducer.ts` filters every incoming event by `conversation_id`. Events for a non-selected conversation are buffered in `sessionMessages`, preventing streaming output from leaking between conversations.

### Common Patterns
- Events from backend are typed in `types/events.ts` — must match `bot/webui/events.py`.
- Sidebar pool selector is **local state only** — actual routing is determined by `PoolSessionStore` on the backend.
- New conversation creation sends `pool` param to pin the conversation to a pool.
- Streaming state (`isStreaming`) is managed atomically with messages via `StreamState`.
- **Design tokens**: All colors defined as CSS variables in `src/index.css` `:root` / `.dark` blocks. `tailwind.config.js` maps tokens via `var(--color-*)` — components use single classes (`bg-canvas`, `text-ink`) that auto-flip for dark mode.
- **SectionLabel**: All settings tabs share `<SectionLabel>` for consistent Geist Mono 10px uppercase section eyebrows. Never hard-code `text-[11px] font-semibold uppercase` in settings views.
- **Card hierarchy**: Settings items use a two-level pattern: outer `<Card>` per item (provider/pool/MCP server), inner nested card for sub-items (models/subagents). Search inputs above lists with >5 items. Dashed-border "Add" buttons at list bottoms.

### Internationalization (i18n)

- All display strings (JSX text, `placeholder`, `aria-label`, `title`, `alt`, toast/error messages) resolve via `useT()` + a typed `MessageKey` — never hardcode display strings in components. The catalog lives at `src/i18n/en.ts`; keys are dotted paths (`"settings.models.defaultModel"`), and typos are compile errors.
- `useT()` returns a `t(key, params?)` function with `{name}` interpolation. The default context value is a working English translator, so tests render components without wrapping `<I18nProvider>`.
- To add a locale: create a catalog (same shape as `en`), register it in `catalogs` (`src/i18n/index.ts`), and pass `locale` to `<I18nProvider>`. No locale-switcher UI exists yet — the infrastructure is ready.
- Universal proper nouns (`MCP`, `Skill`, `Skills`) live in `src/i18n/terms.ts` as `TERMS`, are used directly at render sites (not via `t()`), and are never translated. Sentences that embed these terms (e.g. `"No MCP servers configured."`) stay in the catalog — only the surrounding words translate.
- Section-local protocol/product labels (`stdio`, `SSE`, `OpenAI Compatible`, `Anthropic`) stay in the catalog and are never translated in any locale.

## Specs & Issue Tracker

WebUI specs/PRDs live at the **project level** under `docs/design/<feature-slug>/` (not under `webui/docs/`). The graph visualization IA redesign spec is at `docs/design/graph-visualization-redesign/PRD.md` + `tickets.md`. ADR-0040 (`docs/adr/0040-*.md`) covers the backend re-invocation + spec immutability decisions.

## Design System

The WebUI ships the **Teal & Ember Console** design system. The normative spec lived in the now-removed `docs/design/teal-ember-redesign/`; this section is the surviving record.

- **Palette source of truth**: `src/index.css` `:root` / `.dark` blocks — all colors as CSS variables (`--color-*`), mapped through `tailwind.config.js` via `var(--color-*)`. Edit colors once in `index.css`; never in the tailwind config or components. Brand teal `#2DD4BF` (dark) / `#0D9488` (light) — one teal hue across both themes, only lightness shifts; ember `#F59E0B` / `#B45309` secondary accent; warm-paper (light) / neutral warm-graphite (dark, "Warm Graphite") canvas. Dark borders/scrollbars are hue-free white-alpha ("neutral shimmer"). Dark is the default theme; light remains first-class.
- **Typography**: Inter (display + body) + JetBrains Mono (code, section eyebrows). CJK fallback via Noto Sans SC. Loaded from Google Fonts in `index.html` with `font-display: swap`. (Geist / Geist Mono / Space Grotesk were removed in the typography unification.)
- **Tokens**: color (`brand`/`ember`/`danger`/`success`/`warning`/`error`, ink ladder `ink`/`bright`/`body`/`mute`/`faint`, canvas layers `canvas`/`canvas-sidebar`/`canvas-elevated`/`canvas-popover`, hairline family, per-category `cat-*` accents), radius scale (`xs`/`sm`/`md`/`lg`/`pill`/`full`), elevation (`--shadow-card`/`-card-hover`/`-popover`), motion (`--dur-fast`/`--dur`/`--dur-slow` + `--ease-out`). Derived shades use `color-mix()` — no raw hex in components.
- **Component language**: single primary CTA (`.btn-primary` gradient + glow), converged `DropdownPanel` (all dropdowns), eyebrow-header + severity-left-bar trace cards, floating composer with brand focus glow, branded checkbox check-draw, modal/toast/crossfade motion. All motion is transform/opacity only; the global `prefers-reduced-motion` guard zeroes every animation.
- **Accessibility floor**: WCAG AA contrast both themes, 2px brand focus rings on every interactive element, ≥44px touch targets on coarse pointers (hit-area expansion), `100dvh` mobile layout, safe-area-inset composer clearance, static boot frame under reduced-motion.
- **Legacy aliases**: `link`/`primary`/`signal` → brand family; `error` → `danger`; `hairline-strong` → `border-strong`. Existing components keep working untouched.

## Dependencies

### External
- `react` 18.x — UI framework
- `vite` — Build tool
- `vitest` — Test runner
- `@testing-library/react` — React component/hook testing utilities
- `happy-dom` — DOM environment for tests
- `tailwindcss` — Utility CSS

<!-- MANUAL -->

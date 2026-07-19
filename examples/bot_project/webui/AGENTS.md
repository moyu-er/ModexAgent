<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-07-18 -->

# webui

React frontend for the ModexAgent bot. Vite + TypeScript + Tailwind CSS. Connects to the bot's WebUI backend (aiohttp) via REST API and WebSocket. All color tokens live in `src/index.css` `:root` / `.dark` blocks, mapped through CSS variables — edit colors once, never in the tailwind config. The current design system is "Teal & Ember Console" (see `docs/design/teal-ember-redesign/` and the Design System section below).

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
| `docs/` | Handoff documents and design records |

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
| `hooks/useWebUIStream.ts` | WebSocket hook — manages connection, `request_id`-based optimistic message dedup, streaming events, message history |
| `hooks/useWebUIStream.reducer.ts` | Pure reducer — session-scoped event filtering, `error` event → system notice, `user_message` echo dedup via `_request_id` metadata |
| `hooks/useWebUIStream.reducer.test.ts` | Reducer unit tests (session isolation, error handling, request_id matching) |
| `lib/api.ts` | REST API client (fetchSessions, fetchPools, createConversation, etc.) |
| `lib/ws-client.ts` | WebSocket client with action/attach protocol |
| `types/events.ts` | TypeScript event type definitions matching backend events.py |

## Scripts

| Command | Purpose |
|---------|---------|
| `npm run dev` | Start Vite dev server with backend proxy |
| `npm run build` | Production build to `dist/` |
| `npm run preview` | Preview production build |
| `npm test` | Run Vitest unit tests (currently reducer tests) |

## For AI Agents

### Working In This Directory
- `npm run dev` starts the dev server with proxy to backend.
- `npm run build` outputs to `dist/`, which is served by the backend at `/webui/`.
- `npm test` runs Vitest — 563 tests across 67 files covering hooks, reducers, UI primitives, dropdowns, boot, and all settings views.
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

WebUI specs/PRDs live as local markdown under `docs/design/<feature-slug>/` (mirrors the repo-root convention in `docs/agents/issue-tracker.md`):

- One feature per directory: `docs/design/<feature-slug>/`
- The PRD is `docs/design/<feature-slug>/PRD.md`; accompanying design records (token/component specs) live alongside it (e.g. `DESIGN.md`)
- Triage state is a `Status:` line near the top of the PRD (label vocabulary: repo-root `docs/agents/triage-labels.md`)

Spec reference: `docs/design/teal-ember-redesign/` — the "Teal & Ember Console" redesign (PRD + DESIGN.md). The redesign is shipped (T01–T07); the directory is now the design-system record, not an active spec.

## Design System

The WebUI ships the **Teal & Ember Console** design system (redesign landed 2026-07-18, T01–T07). Full normative spec: `docs/design/teal-ember-redesign/DESIGN.md`.

- **Palette source of truth**: `src/index.css` `:root` / `.dark` blocks — all colors as CSS variables (`--color-*`), mapped through `tailwind.config.js` via `var(--color-*)`. Edit colors once in `index.css`; never in the tailwind config or components. Brand teal `#2DD4A8` (dark) / `#0D9488` (light); ember `#F5A524` / `#B45309` secondary accent; warm-paper (light) / warm-graphite-with-teal-undertone (dark) canvas. Dark is the default theme; light remains first-class.
- **Typography**: Geist (body) + Geist Mono (code, section eyebrows) + Space Grotesk (display — brand wordmark, page titles, boot headline). CJK fallback via Noto Sans SC. Loaded from Google Fonts in `index.html` with `font-display: swap`.
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

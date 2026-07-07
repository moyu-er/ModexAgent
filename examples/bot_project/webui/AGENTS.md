<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-07-07 -->

# webui

React frontend for the ModexAgent bot. Vite + TypeScript + Tailwind CSS. Connects to the bot's WebUI backend (aiohttp) via REST API and WebSocket. Uses a Notion-inspired warm palette (`src/index.css` `:root` / `.dark` blocks) with all color tokens mapped through CSS variables — edit colors once, never in the tailwind config.

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
| `index.css` | Global styles + single source of truth for Notion palette (CSS variables in `:root` / `.dark` blocks) |
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
- `npm test` runs Vitest — 301 tests across 51 files covering hooks, reducers, UI primitives, and all settings views.
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

## Design System

- **Palette source of truth**: `src/index.css` — Notion palette (warm paper `#f6f5f4` canvas, near-black `#000000` ink, single `#0075de` blue accent). Dark mode keeps warm stone backgrounds and shifts text/symbol colors to a sky-blue accent (`#62aef0`).
- **Design reference**: `docs/DESIGN-notion.md` — the Notion design analysis this palette is derived from.
- **Typography**: Geist (body) + Geist Mono (code, section eyebrows). Loaded from Google Fonts in `index.html`.

## Dependencies

### External
- `react` 18.x — UI framework
- `vite` — Build tool
- `vitest` — Test runner
- `@testing-library/react` — React component/hook testing utilities
- `happy-dom` — DOM environment for tests
- `tailwindcss` — Utility CSS

<!-- MANUAL -->

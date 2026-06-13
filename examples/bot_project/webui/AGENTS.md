<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-13 -->

# webui

React frontend for the ModexAgent bot. Vite + TypeScript + Tailwind CSS. Connects to the bot's WebUI backend (aiohttp) via REST API and WebSocket.

## Key Files

| File | Description |
|------|-------------|
| `package.json` | Dependencies and scripts (`dev`, `build`, `preview`, `test`) |
| `vite.config.ts` | Vite build config with proxy to backend |
| `vitest.config.ts` | Vitest test configuration (React hook / reducer unit tests) |
| `tailwind.config.js` | Tailwind CSS configuration |
| `postcss.config.js` | PostCSS plugins |
| `tsconfig.json` | TypeScript configuration |
| `index.html` | HTML entry point |

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
| `index.css` | Global styles (Tailwind directives) |
| `vite-env.d.ts` | Vite type declarations |
| `components/ChatView.tsx` | Chat area — message list + input box |
| `components/MessageBubble.tsx` | Individual message rendering (text, reasoning, tool calls) |
| `components/ReasoningBlock.tsx` | Collapsible reasoning/thinking block |
| `components/ToolTraceCard.tsx` | Tool call result card |
| `components/Sidebar.tsx` | Conversation list, pool selector dropdown, workspace indicator |
| `components/WorkspaceBrowser.tsx` | Modal directory browser for workspace switching |
| `hooks/useWebUIStream.ts` | WebSocket hook — manages connection, streaming events, message history |
| `hooks/useWebUIStream.reducer.ts` | Pure reducer for applying server events with conversation-scoped filtering |
| `hooks/useWebUIStream.reducer.test.ts` | Reducer unit tests (session isolation) |
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
- `npm test` runs Vitest — currently covers `useWebUIStream.reducer.ts`.
- The frontend has **no direct pool switching** for existing conversations — it's purely a display filter in the sidebar dropdown.
- Workspace switching is done via `WorkspaceBrowser` → `POST /api/workspace/cd`.
- `useWebUIStream.ts` is the core hook — it handles WebSocket lifecycle, optimistic messages, and streaming state.
- **Session isolation**: `useWebUIStream.reducer.ts` filters every incoming event by `conversation_id`. Events for a non-selected conversation are ignored, preventing another conversation's streaming output from leaking into the current view.

### Common Patterns
- Events from backend are typed in `types/events.ts` — must match `bot/webui/events.py`.
- Sidebar pool selector is **local state only** — actual routing is determined by `PoolSessionStore` on the backend.
- New conversation creation sends `pool` param to pin the conversation to a pool.
- Streaming state (`isStreaming`) is managed atomically with messages via `StreamState`.

## Dependencies

### External
- `react` 18.x — UI framework
- `vite` — Build tool
- `vitest` — Test runner
- `@testing-library/react` — React component/hook testing utilities
- `happy-dom` — DOM environment for tests
- `tailwindcss` — Utility CSS

<!-- MANUAL -->

# Settings Modal — Design Decisions (Prototype v1)

**Status:** ready-for-review · branch: prototype only · date: 2026-07-20

**Open the prototype:** `webui/prototype/settings-modal.html` — drag the file
into a browser. Standalone, no build step. All tokens lifted verbatim from
`src/index.css` (Teal & Ember Console, dark default).

## What changed

The current Settings view is a **view-swap inside `App.tsx`'s `<main>`**. The
App-level Sidebar (200–480px, conversations + pool selector + workspace
indicator) stays visible while the SettingsView renders its own 208px nav
aside. Effective config content width on a 1440px screen: ~880px. Two sidebars
stacked, neither belongs to the configuration task.

This prototype moves Settings into a **near-full-screen portal modal** that
covers the App Sidebar entirely. Settings now owns the full viewport — minus
a sliver of chat shell at the top/bottom so users keep context.

## Layout

```
┌──────────────────────────────────────────────────────────────────────┐
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ Configuration                                                  │  │
│  │ Settings  [§ Models]            [☀ Light] [✎ Mark dirty] [×]  │  │ ← header
│  ├──────────────┬─────────────────────────────────────────────────┤  │
│  │ CONFIGURATION │                                                 │  │
│  │  • IM         │                                                 │  │
│  │  • Models  ◀  │   ── page content ──                            │  │ ← body
│  │               │                                                 │  │
│  │ POOLS & AGENTS│                                                 │  │
│  │  • Pools      │                                                 │  │
│  │  • MCP        │                                                 │  │
│  │  • Skills     │                                                 │  │
│  │  • Prompts    │                                                 │  │
│  ├──────────────┴─────────────────────────────────────────────────┤  │
│  │  • Unsaved changes               [Cancel]  [Save]              │  │ ← footer (im/model only)
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

- **Panel size:** `min(1280px, 95vw) × min(860px, 92vh)` — wide screens cap
  at 1280px, small screens get 95vw. Height leaves a 4vh sliver of the chat
  shell visible top/bottom so the modal reads as floating, not black-void.
- **Scrim:** `--color-overlay` (rgba(0,0,0,0.5), both themes) — already the
  project's modal scrim token.
- **Animation:** `modal-scrim-enter` (180ms fade) + `modal-panel-enter`
  (180ms scale .96→1) — verbatim from `src/index.css` §5.4.
- **Portal:** `createPortal(... , document.body)` is mandatory. The App
  Sidebar's mobile slide transform would otherwise become the containing
  block for `position: fixed` and trap the modal on the left. WorkspaceBrowser
  already proves this pattern.

## Header policy — what replaces "← Back"

The old SettingsView had a "← Back" button top-left of its own sidebar.
Removed. New header (top of modal):

| Slot | Element | Purpose |
|---|---|---|
| Left | `eyebrow` (mono uppercase "Configuration") + `title` "Settings" + current-tab chip with cat-* color | Anchors the modal's identity, mirrors the eyebrow pattern from settings sub-views |
| Right | theme toggle (prototype-only) · "Mark dirty" toggle (prototype-only) · **close X** | Real impl ships only the X |

**Close X** is a 32×32 ghost icon button. Hover: `bg-hairline-soft`. Active:
`scale(0.98)`. Same hit area as the WorkspaceBrowser X.

## Escape routes — three ways to close, one policy

`escape-routes` (Apple HIG) and `sheet-dismiss-confirm` (Apple HIG) say every
modal needs a clear close affordance AND must confirm before discarding
unsaved work. All three close paths route through the same `requestClose()`
intent:

| Trigger | Behavior |
|---|---|
| Click close X | `requestClose()` |
| Click scrim (outside panel) | `requestClose()` |
| Press `Esc` | `requestClose()` |

`requestClose()`:

```
if dirty AND current tab is persisted (im/model):
  show ConfirmDialog("Discard unsaved changes?",
                     "Closing now will lose your edits to the current view.",
                     discard / cancel)
else:
  closeModal()
```

The ConfirmDialog is the existing component from
`components/settings/ConfirmDialog.tsx` — same visual, same keyboard policy
(Esc cancels the close, keeps modal open), same backdrop-click cancels.

**Footer Cancel/Save does NOT close the modal.** Cancel resets the form to
the last-saved state (existing behavior); Save persists (existing behavior).
The user can keep editing other tabs after a Save. The X is the only "I'm
done with settings" affordance — this matches the user's stated requirement.

## Footer policy — per-tab

SettingsView's `PERSISTED_DOMAINS = {"im", "model"}` already encodes which
tabs share the outer save footer. This prototype keeps that split:

| Tab | Outer ActionBar (Cancel/Save) | Persistence |
|---|---|---|
| im | ✅ shown | `/api/config` shared save |
| model | ✅ shown | `/api/config` shared save |
| pools | ❌ hidden | `PoolsView` owns its own persistence + toasts |
| mcp | ❌ hidden | `GlobalMcpView` owns its own |
| skills | ❌ hidden | `GlobalSkillsView` owns its own |
| prompts | ❌ hidden | `PromptsView` owns its own |

When the outer footer is hidden, the modal body extends to the bottom edge —
no empty strip. The non-persisted tabs carry their own inline save controls
inside their content area (unchanged from today's implementation).

**Dirty indicator** in the footer is the existing `.unsaved-dot` (6px ember
glow) — wired to the parent view's dirty state, no new state invented. When
the outer footer is hidden, dirty state of non-persisted tabs is invisible at
the modal level; this matches today's behavior and is acceptable because each
of those tabs has its own inline save UX.

## Dirty state — close-time check is the only new gate

Today the dirty state has two consumers:

1. `ActionBar` shows the unsaved-dot + enables Cancel/Save.
2. `switchView()` between persisted tabs opens a `ConfirmDialog` if dirty.

Modal migration adds one more consumer:

3. `requestClose()` (X / scrim / Esc) opens a `ConfirmDialog` if dirty.

The existing `useDirty(form, original)` hook and `discardView` state machine
are reused unchanged. The `ConfirmDialog` for the close path uses the same
copy as the switch-view path (`settings.common.discardUnsavedTitle` +
`settings.common.discardSwitchView`) — same wording signals same operation.

## Interaction states to verify in the prototype

1. **Open:** click "Open Settings" in the simulated sidebar.
2. **Tab switch:** click each of the 6 nav items. The current-tab chip in the
   header updates color + label.
3. **Footer visibility:** only `IM Adapters` and `Models` show the bottom
   Cancel/Save bar. Switch to Pools/MCP/Skills/Prompts → footer disappears.
4. **Dirty toggle:** click "Mark dirty" in the header. The footer's
   unsaved-dot lights up, "All changes saved" → "Unsaved changes", Cancel
   and Save enable.
5. **Close while dirty:** click X (or scrim, or Esc). ConfirmDialog appears.
   - `Discard` → modal closes, dirty state cleared.
   - `Cancel` / Esc / backdrop click → confirm closes, modal stays.
6. **Close while clean:** click X. Modal closes instantly.
7. **Theme toggle:** click "Light" → flips to light theme tokens. The
   prototype default is dark (matches the project default).

## Open questions for review

These are the forks I want your call on before implementation:

### Q1. Modal size — comfortable or immersive?

Prototype ships `min(1280px, 95vw) × min(860px, 92vh)`. Alternatives:

| Option | Size | Tradeoff |
|---|---|---|
| A. Comfortable (current) | `min(1280px, 95vw) × min(860px, 92vh)` | Sliver of chat visible top/bottom — modal reads as floating, keeps context. Loses ~4vh of vertical space. |
| B. Immersive | `min(1440px, 96vw) × 96vh` | Near-full-screen. Maximum config canvas. Loses context — the chat shell is barely visible. |
| C. Adaptive | Comfortable on ≥1280px wide, immersive below | Best of both, more CSS branches. |

My recommendation: **A**. The sliver of chat visible at top/bottom is what
makes the modal feel like an overlay rather than a route change — and the
1440px-wide default already gives ~1280px of config width, which is more
than double the current effective ~880px.

### Q2. Header — show current tab or just "Settings"?

Prototype shows `Settings  [§ Models]` with a colored chip reflecting the
current tab. Alternatives:

| Option | Header shows |
|---|---|
| A. Settings + current-tab chip (current) | "Settings  [§ Models]" — gives the modal a tab identity, cat-* color threads through |
| B. Just "Settings" | "Settings" — cleaner, less info; users rely on the nav highlight |
| C. Current tab as title | "Models" — promotes the tab; loses the "Settings" framing |

My recommendation: **A**. The chip gives the modal a per-tab color identity
matching the page-head pattern in each settings sub-view. Cheap signal.

### Q3. Confirm-on-close — always, or only for persisted tabs?

Prototype only shows the confirm when the current tab is persisted (im/model)
AND dirty. For non-persisted tabs (pools/mcp/skills/prompts), the modal
closes immediately even if there are unsaved inline edits inside the tab.

Alternatives:

| Option | Confirm trigger |
|---|---|
| A. Only persisted tabs (current) | im/model dirty → confirm. Others close immediately. |
| B. Always confirm if any tab has unsaved state | Requires every non-persisted tab to expose its dirty state upward. More wiring; matches `sheet-dismiss-confirm` more strictly. |
| C. Never confirm — just close | Snappiest; loses work on accidental close. Violates `sheet-dismiss-confirm`. |

My recommendation: **A for the initial migration.** Each non-persisted tab
already owns its own save/toast UX; asking them to also surface dirty state
upward is scope creep. If a user closes the modal with unsaved inline edits
in Pools, that's the same UX as today (today: switching view away from
Pools doesn't prompt either). **Revisit if users report data loss.**

### Q4. Theme toggle in header — keep or drop?

The prototype's "Light / Dark" toggle is a demo affordance (so you can see
both themes). The real app already has a theme toggle in the Sidebar — putting
one in the modal header duplicates it.

My recommendation: **drop**. Real implementation ships only the close X in
the header-actions slot. Theme toggle stays in the Sidebar.

### Q5. Status bar — visible or hidden behind modal?

Today the App has a 32px-tall statusline (logo + workspace + pool) at the top.
Modal covers it. Alternatives:

| Option | Behavior |
|---|---|
| A. Modal covers status bar (current) | Modal is the only thing visible. Cleanest. Loses the brand mark + workspace indicator while configuring. |
| B. Modal leaves status bar visible | Modal top aligns below status bar. Workspace indicator stays readable. Modal is ~32px shorter. |

My recommendation: **A**. The status bar's info (workspace, pool) is
irrelevant while configuring — configuration is global across workspaces and
pools. Covering it makes the modal feel like a proper route.

### Q6. Mobile — full-screen sheet instead of modal?

Prototype is desktop-first. On mobile, `min(1280px, 95vw)` collapses to 95vw
but the panel is still centered with scrim — feels wrong on a phone.

Real implementation should add a mobile variant: bottom sheet or full-screen
takeover below `md` breakpoint. WorkspaceBrowser already does this implicitly
(`max-w-[90vw] max-h-[70vh]`). For Settings the more appropriate pattern is a
full-screen sheet (no scrim on mobile) because configuration is a deep task,
not a quick pick.

My recommendation: defer mobile to implementation. Desktop modal first,
mobile sheet as a follow-up ticket. The CSS infrastructure (`min-h-dvh`,
safe-area-inset) already exists in the project.

## Implementation impact (preview, not done)

| File | Change | LoC |
|---|---|---|
| `App.tsx` | `view: "chat" \| "settings"` → `settingsOpen: boolean`; remove `<ViewCrossfade>` swap; render `<SettingsModal open={settingsOpen} onClose={...} />` via portal alongside `<ChatView>` | ~15 |
| `SettingsView.tsx` → `SettingsModal.tsx` | Wrap top-level in portal + scrim + panel; remove the "← Back" button; add header with title + close X; rename `onExit` → `onClose`; reuse all internal logic (form state, switchView, dirty, discardView) | ~40 |
| `Sidebar.tsx` | `onOpenSettings` calls `setSettingsOpen(true)` instead of `setView("settings")` | 1 |
| `i18n/en.ts` | Add `settings.close: "Close"`, `settings.header.title: "Settings"`, `settings.header.eyebrow: "Configuration"` | ~3 |
| `index.css` | **Zero changes** — reuses `modal-scrim-enter` / `modal-panel-enter` / `action-bar` / `unsaved-dot` / `nav-item` / `category-chip` / `bg-overlay` / `shadow-popover` | 0 |
| Tests | Existing SettingsView tests need to mount via the portal + assert `role="dialog"`. Switch-view discard tests still pass. | TBD |

No backend changes. No config API changes. No `ConfirmDialog` changes (reused
as-is).

## UX rules applied

From the ui-ux-pro-max skill:

- `escape-routes` (Apple HIG) — X, scrim, Esc all close. ✅
- `sheet-dismiss-confirm` (Apple HIG) — dirty state → ConfirmDialog before close. ✅
- `modal-escape` — clear close affordance (X is always visible top-right). ✅
- `modal-motion` — scale + fade from 0.96, 180ms (existing token). ✅
- `exit-faster-than-enter` — N/A here, both use the same 180ms token (could tighten exit to ~120ms if desired).
- `focus-on-route-change` — close button autofocuses on open; after close, focus returns to the "Open Settings" trigger.
- `keyboard-nav` — Tab order: header actions → nav items → content → footer. Esc closes.
- `color-not-only` — the unsaved-dot is amber AND has `aria-label="Unsaved changes"`.
- `reduced-motion` — global guard in `index.css` zeroes all animations.

## Not in scope (call out before implementing)

- Mobile full-screen sheet variant (Q6).
- Persisting `?tab=` URL sync still works — `writeTabToUrl` runs unchanged.
  The browser back button while modal is open will change the URL but not
  close the modal; this matches today's behavior (view swap also doesn't
  close on URL change).
- Deep-linking `?settings=open` — not adding. Modal opens via UI only.

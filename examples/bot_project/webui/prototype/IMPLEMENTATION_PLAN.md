# Settings Modal — Implementation Plan

**Status:** ready-for-agent · branch: develop_gyt · date: 2026-07-20

## Goal

Convert the current Settings view-swap (`App.tsx` `view: "chat" | "settings"` + `<ViewCrossfade>`) into a **portal modal** that covers the App Sidebar entirely. The 6 settings tabs and all their fields stay byte-identical — only the shell changes.

## Reference

- Prototype (visual + interaction spec): `webui/prototype/settings-modal.html`
- Design decisions: `webui/prototype/DESIGN.md`
- Original components: `webui/src/components/settings/SettingsView.tsx` (427 lines), `webui/src/App.tsx` (318 lines)

## Non-Goals (CRITICAL — do NOT do these)

- **Do not touch any settings tab content**: ConfigForm, ModelEditor, PoolsView, PoolEditor, GlobalMcpView, GlobalSkillsView, PromptsView, AgentMcpSelector, AgentSkillSelector, ExternalMainAgentFields, etc. are 100% unchanged. Their fields, props, behavior stay identical.
- **Do not change ConfirmDialog** — reuse as-is for the dirty-close confirm.
- **Do not change ActionBar** — reuse as-is.
- **Do not change any i18n keys that already exist** — only ADD new ones.
- **Do not change `index.css`** — all needed classes (`modal-scrim-enter`, `modal-panel-enter`, `action-bar`, `unsaved-dot`, `bg-overlay`, `shadow-popover`, `bg-canvas-popover`) already exist.
- **Do not change `Sidebar.tsx`** — the gear button + `onOpenSettings` prop stay; only the caller (`App.tsx`) changes what that callback does.

## Work Breakdown

### Task A — `SettingsView.tsx` → portal modal shell + `i18n/en.ts` keys

**File: `webui/src/components/settings/SettingsView.tsx`**

1. **Rename the export** from `SettingsView` to `SettingsModal` (keep the file name OR rename file to `SettingsModal.tsx` — pick one; if renaming file, update all imports). **Recommendation: keep file name `SettingsView.tsx` to minimize churn, just rename the exported function.** Update the re-export `export type { ViewKey }` line stays.

2. **Change the Props interface:**
   ```tsx
   // BEFORE
   interface Props { onExit: () => void; }
   // AFTER
   interface Props { open: boolean; onClose: () => void; }
   ```

3. **Wrap the entire returned JSX in a portal + scrim + panel.** Use `createPortal(..., document.body)` (escape the Sidebar transform). The panel gets:
   - `className="modal-scrim-enter fixed inset-0 z-50 flex items-center justify-center bg-overlay p-6"` on the scrim
   - `className="modal-panel-enter flex w-[min(1280px,95vw)] h-[min(860px,92vh)] flex-col rounded-lg border border-hairline bg-canvas-popover shadow-popover overflow-hidden"` on the panel
   - Scrim `onClick`: if `e.target === e.currentTarget` → `requestClose()`. Panel `onClick`: `e.stopPropagation()`.

4. **When `open === false`, return `null`** (do not render the portal). This is the standard modal pattern.

5. **Replace the "← Back" button** (currently top-left of the `<aside>`, lines 227-235) with a **modal header** at the top of the panel:
   ```tsx
   <div className="flex items-center gap-4 border-b border-hairline bg-canvas px-5 py-3.5 flex-shrink-0">
     <h2 className="text-lg font-bold text-bright tracking-tight leading-tight">
       {t("settings.modal.title")}
     </h2>
     <div className="ml-auto flex items-center gap-2">
       <IconButton
         icon={<XIcon />}
         label={t("settings.modal.close")}
         variant="ghost"
         size="sm"
         onClick={requestClose}
       />
     </div>
   </div>
   ```
   Import `XIcon` from `../ui/icons` (already exists, used elsewhere).

6. **Body wrapper:** the existing `<div data-testid="settings-shell" className="flex h-full flex-col md:flex-row">` becomes `<div data-testid="settings-shell" className="flex min-h-0 flex-1 flex-row">`. The `flex-row` is always row now (modal is wide enough on all breakpoints). The inner `<aside>` keeps `w-52` etc. but drop the `md:` prefix and the `border-b md:border-b-0 md:border-r` responsive dance — just `border-r border-hairline`.

7. **Add a `requestClose()` function** inside the component:
   ```tsx
   const requestClose = useCallback((): void => {
     if (isPersisted && dirty) {
       setDiscardView("__close__" as ViewKey); // sentinel
     } else {
       onClose();
     }
   }, [isPersisted, dirty, onClose]);
   ```
   And in the `ConfirmDialog` `onConfirm` handler, check for the sentinel:
   ```tsx
   onConfirm={() => {
     const next = discardView;
     setDiscardView(null);
     if (next === "__close__" as ViewKey) {
       onClose();
     } else {
       setView(next);
     }
     setError("");
   }}
   ```
   Use a proper string sentinel `__close__` (not a ViewKey cast — use a separate state `discardForClose: boolean` is cleaner; pick whichever).

8. **Esc key handler:** add a `useEffect` that listens for `keydown` Escape when `open` is true:
   ```tsx
   useEffect(() => {
     if (!open) return;
     const onKey = (e: KeyboardEvent) => {
       if (e.key === "Escape") {
         e.stopPropagation();
         if (discardView !== null) return; // confirm dialog open, let it handle Esc
         requestClose();
       }
     };
     window.addEventListener("keydown", onKey);
     return () => window.removeEventListener("keydown", onKey);
   }, [open, discardView, requestClose]);
   ```

9. **Body scroll lock** when open (optional but nice):
   ```tsx
   useEffect(() => {
     if (!open) return;
     const prev = document.body.style.overflow;
     document.body.style.overflow = "hidden";
     return () => { document.body.style.overflow = prev; };
   }, [open]);
   ```

10. **Keep `?tab=` URL sync, switchView, dirty, discardView, onSave, onCancel** — all unchanged. The `writeTabToUrl` effect still works inside a modal.

11. **Update the existing `ConfirmDialog` message** for the close path: the current `settings.common.discardSwitchView` text says "Switching now will lose your edits to the current view." That's fine for both switch and close — no new key needed. Title `settings.common.discardUnsavedTitle` stays.

**File: `webui/src/i18n/en.ts`**

Add 2 new keys under `settings.modal`:
```ts
settings: {
  // ... existing ...
  modal: {
    title: "Settings",
    close: "Close settings",
  },
},
```

### Task B — `App.tsx` state change + test adapters

**File: `webui/src/App.tsx`**

1. **Replace the view state:**
   ```tsx
   // BEFORE (line 122)
   const [view, setView] = useState<"chat" | "settings">("chat");
   // AFTER
   const [settingsOpen, setSettingsOpen] = useState<boolean>(false);
   ```

2. **Remove `<ViewCrossfade>` import** (line 16) and its usage (lines 241-263). Replace with always-rendered `<ChatView>` + conditionally-rendered `<SettingsModal>`:
   ```tsx
   <main className="flex flex-1 flex-col min-w-0">
     <ChatView
       messages={messages}
       isStreaming={isStreaming}
       isPending={isPending}
       todos={todos}
       pendingApprovals={pendingApprovals}
       isApprovingBatch={isApprovingBatch}
       submitApproval={submitApproval}
       onApproveAll={onApproveAll}
       sessionId={selectedId}
       workspace={streamWs}
       onSend={handleSend}
       onPause={pause}
       readOnly={isSelectedSubagent}
       onOpenSidebar={() => setSidebarMobileOpen(true)}
       agentName={agentName}
     />
   </main>

   <SettingsModal
     open={settingsOpen}
     onClose={() => setSettingsOpen(false)}
   />
   ```
   The `<SettingsModal>` renders via portal to `document.body`, so its DOM position in the tree doesn't matter — but placing it as a sibling of `<main>` (inside the `<ToastProvider>`) keeps the toast context available.

3. **Update the Sidebar prop:**
   ```tsx
   // BEFORE (line 220)
   onOpenSettings={() => setView("settings")}
   // AFTER
   onOpenSettings={() => setSettingsOpen(true)}
   ```

4. **Remove the `ViewCrossfade` import** and the `view === "settings"` / `view === "chat"` branches. The `ViewCrossfade` component itself stays in `ui/ViewCrossfade.tsx` (unused is fine; may be reused later).

**File: `webui/src/components/RestartIndicator.test.tsx`**

Line 41 has `onOpenSettings={noop}` — this is a Sidebar prop, unchanged. No edit needed.

**File: `webui/src/components/settings/SettingsView.test.tsx`** (391 lines, 11 test cases)

After rename + prop change, the tests need:
- Import `SettingsModal` instead of `SettingsView` (line 3)
- All `<SettingsView onExit={() => {}} />` (9 occurrences) → `<SettingsModal open={true} onClose={() => {}} />`
- **Test "stacks settings navigation above Pools content on narrow screens" (lines 73-91):** the assertions on `flex-col` + `md:flex-row` + `md:w-52` are NO LONGER VALID because the modal is always `flex-row` (it's wide enough). Update the test to:
  - Rename to `"renders settings navigation beside content in the modal"`
  - Assert `shell.className` contains `flex-row` (not `flex-col`)
  - Assert `navigation.className` contains `w-52` (not `md:w-52`)
  - Keep the "Add pool" assertion unchanged
- **No "Back" button test exists** — confirmed by reading the full file. No test clicks the Back button. So no close-X test needs to replace an existing one.
- **Add 1 new test** at the top of the `describe` block:
  ```tsx
  it("renders null when open is false", () => {
    vi.stubGlobal("fetch", routeFetch());
    const { container } = render(
      <ToastProvider>
        <SettingsModal open={false} onClose={() => {}} />
      </ToastProvider>,
    );
    expect(container.firstChild).toBeNull();
  });
  ```
- All other tests (load config, save/cancel, dirty dot, tab switching, Models validation) pass unchanged — they assert on tab content, not on the shell shape.

Run `npm test` after edits — all 11 existing tests pass (1 updated) + 1 new = 12 tests.

### Task C (parallel with A+B) — Verify

After A and B land:
1. `cd webui && npm test -- --run` — all 563 tests pass (existing + 1 new).
2. `cd webui && npx tsc -p tsconfig.check.json` — no type errors.
3. `cd webui && npm run build` — Vite build succeeds.
4. Manually open the app, click gear in sidebar, verify:
   - Modal opens, covers sidebar
   - 6 tabs switch correctly
   - Footer shows only on IM/Models
   - Editing a field in Models → unsaved dot appears → click X → ConfirmDialog → Discard closes, Cancel stays
   - Esc closes (with confirm when dirty)
   - Scrim click closes (with confirm when dirty)

## Delegation

- **Subagent 1 (Task A)**: `SettingsView.tsx` + `i18n/en.ts`. Scope: 2 files, ~80 lines added/changed. Category: `unspecified-high`. Load skills: `frontend-design`.
- **Subagent 2 (Task B)**: `App.tsx` + `SettingsView.test.tsx`. Scope: 2 files, ~30 lines changed. Category: `unspecified-high`. Load skills: `frontend-design`.

Run both in **parallel** (background). They touch disjoint files — no merge conflict.

After both complete, I verify (Task C).

## Key Files Reference

| File | Lines | Change |
|---|---|---|
| `webui/src/components/settings/SettingsView.tsx` | 427 | Rename export, wrap in portal+scrim+panel, replace Back with header+X, add requestClose + Esc handler, body scroll lock. ~80 lines changed. |
| `webui/src/App.tsx` | 318 | `view` state → `settingsOpen` boolean; remove ViewCrossfade; render ChatView always + SettingsModal conditionally. ~15 lines changed. |
| `webui/src/i18n/en.ts` | 531 | Add `settings.modal.title` + `settings.modal.close`. 2 lines added. |
| `webui/src/components/settings/SettingsView.test.tsx` | 391 | Update import + render props; add 1 new "open=false renders null" test. ~15 lines changed. |

**Total: 4 files, ~110 lines changed. Zero changes to any settings tab component, any CSS, any backend.**

## Risk Assessment

- **Low risk**: portal pattern is already proven (WorkspaceBrowser). All CSS classes exist. No API changes.
- **Medium risk**: the `?tab=` URL sync inside a modal — if the user hits browser back while modal is open, the URL changes but the modal stays. This matches today's behavior (view swap also doesn't close on URL change). Acceptable.
- **Test risk**: `SettingsView.test.tsx` has ~20 tests. Most render the component and assert on tab content — those pass unchanged after the prop swap. The "Back button" test (if present) needs updating to "close X". I'll inspect the test file during delegation to give the subagent the exact list.

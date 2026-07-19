# Tickets: Teal & Ember Console Redesign

Vertical slices implementing the WebUI redesign. Source spec: `PRD.md` + `DESIGN.md` (same directory).

Work the **frontier**: any ticket whose blockers are all done. After T01, tickets 02–04 can proceed in parallel; 05–06 follow; 07 closes.

## T01 — Design token foundation + dark default

**What to build:** The entire app switches to the Teal & Ember token set (DESIGN.md §2–§4): new color/radius/elevation/motion CSS variables for both themes, tailwind mapping, Space Grotesk loaded as display font, dark theme becomes the default for new users. The app builds and renders with the new palette end-to-end, even before any component is restyled.

**Blocked by:** None — can start immediately.

- [x] `:root` and `.dark` define the full new token set (colors incl. brand/ember/danger, hairline incl. dark teal-alpha borders, radius scale, shadows, motion durations/easing); no legacy emerald tokens remain
- [x] `tailwind.config.js` maps the new tokens (incl. radius/motion) via `var(--*)`; no raw hex in components
- [x] Space Grotesk (500/700) loaded with `font-display: swap`; display/brand elements use it; CJK fallback present
- [x] Dark is the default theme; saved user preference still respected; FOUC guard applies the correct theme pre-React
- [x] Token-completeness test: both themes define every required token
- [x] `npm run build` and `npm test` pass; visual sweep at 1440 both themes shows the new palette without broken contrast

## T02 — Boot experience (particle morph)

**What to build:** Waiting for the backend becomes branded and alive: pre-React loader paints the correct theme background with a breathing logo (no white flash, no text); BootScreen runs a particle-morph canvas — loose drift → particles gather into the star-topology logo while connecting → on ready they disperse and the app fades in; staged status copy and an error state with Retry. Reduced-motion users get a static frame; touch devices get no pointer repel.

**Blocked by:** T01 — Design token foundation + dark default.

- [x] Pre-React loader: theme-aware background, centered logo mark with breathing glow, no spinner/text
- [x] Particle engine ported as a self-contained vanilla module (zero deps): idle drift → gather-to-logo → disperse phases; density ≤600 desktop / ≤300 mobile; DPR ≤2; paused when tab hidden
- [x] Boot→app handoff: disperse plays, app fades in (~250ms), one-time stagger on chrome
- [x] Staged status copy (starting/connecting/long/error) via i18n catalog; error card shows raw error + Retry button that re-polls
- [x] `prefers-reduced-motion` renders one static logo frame; no pointer-repel on touch
- [x] Phase state machine and reduced-motion branches unit-tested (mocked canvas); boot logic (staging/retry) tested as pure function/hook
- [x] `npm run build` and `npm test` pass

## T03 — Dropdown convergence (DropdownPanel)

**What to build:** All three dropdowns — form selects, sidebar pool picker, model selector — share one visual spec and one interaction model: a shared panel primitive (popover surface, teal-shimmer border, open animation, accent-bar selection, full keyboard nav), two trigger variants (form field, inline pill). The native `<select>` wrapper is gone. ModelSelector keeps provider grouping; the pool picker keeps its density.

**Blocked by:** T01 — Design token foundation + dark default.

- [x] One shared dropdown panel primitive used by all three dropdowns; two trigger variants per DESIGN.md §5.3
- [x] Native `<select>` wrapper removed; no OS-rendered option lists remain
- [x] Keyboard nav (arrows/Home/End/Esc/typeahead) works identically in all three; panel opens with 150ms fade+translate from trigger direction
- [x] Selected state = 2px brand bar + check everywhere; dark panels show teal-shimmer border
- [x] Interaction tests for the panel and its three consumers (open/close, keyboard, selection)
- [x] `npm run build` and `npm test` pass

## T04 — Core primitives restyle

**What to build:** Buttons, inputs, textareas, cards, checkboxes, dialogs/modals and toasts all match DESIGN.md §5: primary gradient buttons with glow, ghost with teal-shimmer hover border, semantic danger; labeled inputs with brand focus ring and helper/error placement; unified radius scale; modals scale+fade from trigger; toasts with severity dots. Every screen using these primitives immediately looks like one product.

**Blocked by:** T01 — Design token foundation + dark default.

- [x] Button variants (primary gradient+glow / ghost / danger) with unified radius, hover lift, press scale, disabled opacity; single primary CTA styling
- [x] Input/Textarea: label above, 36px field, brand focus ring, helper/error below; no placeholder-only labels
- [x] Card, Checkbox (brand fill + check-draw), ConfirmDialog/modals (scrim, scale+fade entry), Toast (severity dot, auto-dismiss, aria-live) restyled per spec
- [x] Radius scale enforced: no component uses off-scale radii (no 6px button vs 16px card drift)
- [x] Existing ui/primitive tests updated to the new markup; states (hover/selected/disabled) covered
- [x] `npm run build` and `npm test` pass

## T05 — Chat surface redesign

**What to build:** The conversation view matches DESIGN.md §6: assistant messages render bubble-less as full-width prose with a small logo rail mark; user messages in branded bubbles; reasoning blocks, tool-trace and approval cards share the eyebrow-header + severity-left-bar card language; composer is a floating rounded surface with focus glow; no-conversation empty state shows logo watermark + headline + hints; typing indicator uses brand dots. Typewriter streaming unchanged.

**Blocked by:** T04 — Core primitives restyle.

- [x] Assistant = bubble-less prose + 16px logo rail mark; user = branded bubble (per-theme tint/border treatment), max-width 72ch
- [x] Reasoning/tool-trace/approval cards: mono eyebrow header + 3px severity left bar + status icon; severity never conveyed by color alone
- [x] Composer: floating, unified large radius, hairline border, brand ring + glow on focus; send = primary gradient
- [x] Empty state: logo watermark + display headline + mono-eyebrow hints (replaces bare one-liner); typing indicator = staggered brand dots
- [x] All new copy in the i18n catalog; chat renders correctly in both themes
- [x] Component tests updated; `npm run build` and `npm test` pass

## T06 — Chrome & settings polish

**What to build:** App chrome matches the new language: statusline shows the logo mark + Space Grotesk wordmark; sidebar session rows get the new hover/active treatment with accent bar; settings group headers use the mono eyebrow; domain category colors harmonized (DESIGN.md §2.3); ActionBar sticky with backdrop blur and an unsaved-changes ember dot; chat↔settings switches crossfade instead of swapping instantly.

**Blocked by:** T04 — Core primitives restyle.

- [x] Statusline: logo mark (brand) + Space Grotesk wordmark; connection dot unchanged behaviorally
- [x] Sidebar: session rows with hover tint + active accent bar; "New Conversation" is the only gradient button in chrome
- [x] Settings: eyebrow group headers, harmonized domain chip colors, sticky blurred ActionBar + unsaved-changes indicator
- [x] Chat↔settings crossfade (~200ms), no instant swap
- [x] Both themes verified across all settings views; tests updated; `npm run build` and `npm test` pass

## T07 — Polish, accessibility & docs

**What to build:** The redesign is verified end-to-end and the docs tell the truth: mobile sweep (drawer sidebar, 44px targets, safe-area composer, 100dvh), reduced-motion audit (everything zeroed/static), WCAG AA contrast audit both themes, `webui/AGENTS.md` design section finalized to describe the shipped system, stray `webui/nul` file deleted.

**Blocked by:** T02 — Boot experience; T03 — Dropdown convergence; T05 — Chat surface redesign; T06 — Chrome & settings polish.

- [x] Mobile sweep passes at 375px: drawer + scrim, touch targets ≥44px, composer clear of safe area, boot particles don't fight touch scroll
- [x] Reduced-motion audit: particles static, all UI motion zeroed; nothing animates width/height/top/left
- [x] Contrast audit both themes: body ≥4.5:1, secondary ≥3:1; dividers/borders visible in both; focus rings visible everywhere
- [x] Visual sweep at 375/768/1024/1440 × both themes recorded in the PRD comments
- [x] `webui/AGENTS.md` "Design System" section describes the shipped token set (proposed-status caveat removed); `webui/nul` deleted
**Carried over from step reviews (fix here):**
- [x] Replace all `/alpha` modifiers on `var()`-token colors — they silently generate NO CSS in this Tailwind setup (verified T04 review), leaving missing rings/borders incl. several focus rings (a11y): `App.tsx` (bg-link/50), `ChatView.tsx` (border-error/40, bg-error/10), `ConversationSpine.tsx` (ring-link/25), `ReasoningBlock.tsx` (focus-visible:ring-link/50), `SessionTree.tsx` (border-hairline/60, ring-link/50 x2), `TodoPanel.tsx` (ring-warning/50 x2, border-warning/30, dark:border-warning/20), `ToolTraceCard.tsx` (ring-link/50), `PoolEditor.tsx` (text-link-deep/70), `PromptsView.tsx` (border-hairline/50), `DropdownPanel.tsx` (ring-error/30, ring-link/30). Replace with solid token colors or a color-mix-based utility.
- [x] particles.ts: guard `startLoop()` against `phase === "done"` (visibilitychange can restart a finished loop)
- [x] Reduced-motion: shorten/skip the DISPERSE_MS 800ms hold before BootScreen unmounts
- [x] DropdownPanel typeahead: buffer keystrokes (~500ms window) and accept any printable char, not just `[a-z0-9]` (CJK labels)
- [x] Harmonize disabled opacity (Input/Textarea .60 vs buttons/checkbox .45)
- [x] `.btn-primary` light-theme text color: extract token instead of hardcoded `#ffffff`
- [x] Pre-existing dead class `from-warning-soft` in TodoPanel.tsx; pre-existing raw rgba in `boxShadow.floating` (tailwind.config.js)
- [x] Dedup `.boot-eyebrow` vs `.eyebrow` (byte-identical declarations in index.css)
- [x] Remove unused i18n keys `chat.selectConversation`, `reasoning.thinking`
- [x] ChatView.tsx:387 mixes `border-danger` with `text-error` — unify to one token
- [x] Logo exists in 3 copies (public/logo-icon.svg, index.html inline, LogoMarkIcon) — name one source of truth in T06
- [x] TodoPanel `text-white` on category-chip check (T01 review note) — verify still correct on brand fill
- [x] `npm run build` and `npm test` pass

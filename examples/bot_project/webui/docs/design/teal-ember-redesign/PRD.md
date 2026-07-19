# PRD: WebUI "Teal & Ember Console" Redesign

Status: ready-for-agent
Created: 2026-07-18
Design system: [DESIGN.md](./DESIGN.md) (normative token/component spec — read together with this PRD)

## Problem Statement

The ModexBot WebUI looks unpolished and off-brand. From the user's perspective:

- The UI accent color (emerald `#059669`) is not the brand color in the logo (`#2DD4A8`), so the product does not feel like one thing.
- Starting the app shows a bare spinner with "Connecting to backend…" text — no branding, no sense of progress, no life. First impressions are the worst screen in the app.
- Controls are visually inconsistent: three independently-built dropdowns, three different radius scales, buttons/cards/inputs that don't look like one product.
- Chat bubbles, tool cards, settings pages and the empty state are plain and unconsidered.
- The documented design system in `webui/AGENTS.md` describes a palette that no longer exists, so there is no reliable design reference.

The project's own website (`modex-agent` GitHub Pages) already demonstrates an attractive, lively design language ("Teal & Ember") built around the true brand color. The WebUI should adopt that language, adapted for a console/productivity surface.

## Solution

A complete visual redesign of the WebUI — "Teal & Ember Console" — per `DESIGN.md`:

- New design-token set in `src/index.css` (colors, radius, elevation, motion) derived from the website's tokens; dark theme becomes the default; light theme remains first-class.
- A branded boot experience: pre-React loader shows the logo with a breathing glow on the correct theme background; `BootScreen` shows a particle-morph animation (particles gather into the star-topology logo while connecting, disperse into the app on ready); staged status text and a retry affordance on failure.
- Converged component spec: one radius scale, one motion rhythm, one dropdown visual spec shared by all three dropdown implementations, restyled buttons/inputs/cards/checkbox/dialog/toast.
- Redesigned chat surface: bubble-less assistant messages with logo rail mark, branded user bubbles, consistent severity treatment for tool trace and approval cards, restyled composer, designed empty state, brand typing indicator.
- Chrome polish: logo mark in the statusline, restyled sidebar/session rows/settings, crossfade between chat and settings, mobile and accessibility pass.
- Docs corrected: `webui/AGENTS.md` design section rewritten to point at the real design system.

The logo assets (`assets/logo-*.svg`, `webui/public/logo.jpg` favicon) are preserved and given more presence, not replaced.

## User Stories

1. As a user opening the WebUI, I want the loading screen to show the product's brand (logo, brand color), so that I immediately recognize the product.
2. As a user on dark theme, I want the very first paint (before React loads) to already be dark, so that I don't get a white flash.
3. As a user waiting for the backend, I want a lively, animated waiting screen instead of a bare spinner, so that waiting feels intentional rather than broken.
4. As a user waiting for the backend, I want staged status messages that change as time passes, so that I understand what is happening.
5. As a user whose backend failed to start, I want to see the actual error and a retry button, so that I can recover without refreshing blindly.
6. As a user, I want the transition from the boot screen into the app to be smooth (particles disperse, app fades in), so that the app feels crafted.
7. As a user with reduced-motion settings, I want all boot and UI animation replaced by static frames, so that the app respects my accessibility needs.
8. As a user, I want the UI accent color to match the logo's teal, so that the product feels coherent.
9. As a user, I want dark mode to be the default theme, so that the console looks at home in a developer environment. (Light theme must remain fully usable.)
10. As a user, I want every container in dark mode to share the same subtle teal-tinted border treatment, so that the UI has a consistent signature look.
11. As a user, I want a single, consistent corner-radius language across buttons, inputs, cards, bubbles and chips, so that the UI looks like one product.
12. As a user, I want a single, consistent motion rhythm (durations, easing, hover lift), so that interactions feel unified.
13. As a user, I want all dropdowns (form selects, sidebar pool picker, model selector) to look and behave the same way, so that I don't have to relearn each one.
14. As a user, I want every dropdown to be fully keyboard navigable (arrows, Home/End, Esc, typeahead), so that I can use the app without a mouse.
15. As a user, I want dropdown panels to animate open quickly (fade + small translate) from their trigger, so that I understand where they came from.
16. As a user, I want the selected item in every dropdown marked the same way (accent bar + check), so that state is always readable.
17. As a user, I want primary buttons to use the brand gradient with a soft glow, so that the main action on each screen is obvious.
18. As a user, I want destructive actions in a semantic danger color and never gradient, so that I don't confuse them with primary actions.
19. As a user, I want form fields to have visible labels above and helper/error text below, so that I always know what a field is and what went wrong.
20. As a user, I want input focus to show a clear brand-colored ring, so that I know where I am.
21. As a user reading assistant messages, I want clean full-width typography (no bubble) with a small brand mark, so that long answers read like a document.
22. As a user, I want my own messages in a distinct brand-tinted bubble, so that I can scan the conversation roles at a glance.
23. As a user, I want reasoning blocks collapsed with a consistent eyebrow header, so that thinking doesn't crowd the answer.
24. As a user, I want tool-call and approval cards to share one card language (eyebrow header + severity left bar), so that all structured blocks are scannable.
25. As a user, I want severity levels (normal/sensitive/dangerous/hardline) shown with both color and icon, so that meaning never depends on color alone.
26. As a user, I want the composer to be a floating, rounded surface that glows subtly on focus, so that the input area feels inviting.
27. As a user with no conversation selected, I want a designed empty state (logo watermark, headline, hints), so that the app doesn't look broken.
28. As a user, I want a branded typing indicator while the agent starts responding, so that waiting mid-conversation feels alive.
29. As a user, I want the agent's streaming text to keep its typewriter feel, so that responses feel live.
30. As a user, I want the statusline to show the logo mark next to the product name, so that the brand is present on every screen.
31. As a user, I want session rows in the sidebar with clear hover and active states (accent bar), so that I always know where I am.
32. As a user, I want settings sections headed by consistent mono eyebrow labels, so that settings pages are scannable.
33. As a user, I want the settings domain colors (MCP/pools/skills/models/IM/prompts) to feel like one harmonized set, so that the settings nav doesn't look random.
34. As a user, I want the settings save bar to stay visible (sticky, blurred) with an unsaved-changes indicator, so that I never lose edits.
35. As a user switching between chat and settings, I want a quick crossfade instead of an instant swap, so that navigation feels smooth.
36. As a mobile user, I want the sidebar as a drawer with a scrim, touch targets ≥44px, and the composer clear of the home-indicator area, so that the app is usable on a phone.
37. As a mobile user waiting for the backend, I want the boot animation to not fight my scroll/touch gestures, so that the screen stays responsive.
38. As a keyboard user, I want a visible 2px brand focus ring on every interactive element, so that I can navigate confidently.
39. As a user, I want text contrast to meet WCAG AA in both themes, so that the UI is readable.
40. As a developer maintaining the WebUI, I want all colors defined once as CSS variables and mapped through Tailwind, so that future palette tweaks are one-file edits.
41. As a developer, I want derived shades computed via `color-mix()` from tokens, so that no raw hex leaks into components.
42. As a developer, I want the design system documented in `webui/docs/design/teal-ember-redesign/DESIGN.md` and referenced from `webui/AGENTS.md`, so that the documented design is the real one.
43. As a developer, I want all new UI copy to go through the i18n catalog, so that localization stays possible.
44. As a user, I want the existing logo assets and favicon kept, so that brand recognition is preserved.

## Implementation Decisions

- **Token layer**: All redesign tokens land in `src/index.css` (`:root` / `.dark`) exactly per `DESIGN.md` §2–§4; `tailwind.config.js` stays a pure `var(--*)` mapping. Dark becomes the default theme (theme storage key and FOUC guard updated; existing saved preferences respected).
- **Fonts**: Add Space Grotesk (500/700) for display; keep Geist (body) and Geist Mono (mono); add `Noto Sans SC` CJK fallback. Loaded via Google Fonts in `index.html` with `font-display: swap`.
- **Boot**: The pre-React inline loader in `index.html` is restyled (theme-aware background, breathing logo, no text). `BootScreen` is rebuilt around a **particle-morph canvas**: the website's `particles.js` engine is ported as a self-contained vanilla module (no dependencies), configured with a single shape (the star-topology logo mark, sampled from the SVG) plus a loose-drift idle phase; on backend-ready the particles disperse and the app fades in. Density ≤600 desktop / ≤300 mobile; DPR capped at 2; paused when tab hidden; static frame under `prefers-reduced-motion`; no pointer-repel on touch devices. Status copy reuses the existing staged-message logic with new i18n keys; error state gains a Retry button.
- **Dropdowns**: A shared `DropdownPanel` primitive (popover surface, hairline border, radius-md, open animation, item/selected/keyboard semantics per `DESIGN.md` §5.3) is extracted; `Select` (native-select wrapper, deleted), `SelectMenu`, and `ModelSelector` are rebuilt on it as two trigger variants (form field, inline pill). ModelSelector keeps provider grouping; sidebar pool picker keeps its density.
- **Primitives**: `Button` (primary gradient / ghost / danger), `Input`, `Textarea`, `Card`, `Checkbox`, `ConfirmDialog`/modals, `Toast` restyled per `DESIGN.md` §5. No new component-library dependency is introduced.
- **Chat**: `MessageBubble` splits visually into bubble-less assistant rendering (logo rail mark) and branded user bubble; `ReasoningBlock`, `ToolTraceCard`, `ApprovalCard` adopt the eyebrow + severity-bar card language; composer and empty state per `DESIGN.md` §6; typewriter streaming unchanged.
- **Chrome**: `logo-icon.svg` copied from `assets/` to `webui/public/` and used in the statusline and boot screen (favicon `logo.jpg` unchanged). Sidebar session rows, settings nav chips (harmonized domain colors), sticky blurred `ActionBar` with unsaved indicator, chat↔settings crossfade.
- **Motion tokens**: `--dur-fast/--dur/--dur-slow` + shared easing applied globally; hover lift and press scale standardized; the existing global `prefers-reduced-motion` guard is extended to all new animation including particles.
- **i18n**: All new copy (boot stages, retry, empty state, hints) added to `src/i18n/en.ts`; the pre-React loader carries no text. Locale support stays en-only.
- **Docs**: `webui/AGENTS.md` "Design System" section rewritten to point at `DESIGN.md`; stale `docs/DESIGN-notion.md` reference removed; stray `webui/nul` file deleted.
- **Phasing**: P0 tokens → P1 boot → P2 primitives/dropdowns → P3 chat → P4 chrome/settings → P5 polish (mobile, a11y, docs), per `DESIGN.md` §11. Each phase must build (`npm run build`) and pass `npm test` independently.
- **No functional changes**: REST/WS protocols, hooks' data flow, reducers, routing state, and backend are untouched. This is a presentation-layer redesign.

## Testing Decisions

- **Good tests here assert external behavior, not pixels**: token presence (CSS variables defined for both themes), component states (hover/selected/disabled classes, ARIA roles), dropdown keyboard behavior, boot state machine (staged copy, retry, ready transition), theme default and persistence.
- **Seams**: the existing component-test seam (Vitest + testing-library + happy-dom, 301 tests across 51 files) is reused — no new test infrastructure. Specifically:
  - `DropdownPanel` and its three consumers: interaction tests (open/close, keyboard nav, selection) modeled on existing `ui/` primitive tests.
  - Boot logic: the staged-message/attempts/retry logic extracted into a testable hook or pure function (mirroring `useWebUIStream.reducer.test.ts`); the particle engine is treated as a pure-ish module — phase transitions (idle → gather → disperse) and reduced-motion/static-frame branches unit-tested with a mocked canvas 2D context; raw drawing not asserted.
  - Token layer: a lightweight test asserting both `:root` and `.dark` define the full required token set (guards against half-migrated themes).
  - Existing component tests updated only where class names/markup they assert on legitimately change.
- **Prior art**: `useWebUIStream.reducer.test.ts` (pure reducer tests), existing `ui/` primitive tests and settings-view tests under `src/`.
- Visual verification (build + manual sweep at 375/768/1024/1440, both themes, reduced-motion on) is part of each phase's acceptance, not automated.

## Out of Scope

- New features, routing, or state-management changes; backend/API changes.
- Adding a UI component library (Radix/MUI/etc.) or animation library (framer-motion/GSAP).
- Adding locales beyond English (infrastructure already exists).
- Redesigning the mermaid/markdown/syntax-highlight rendering internals (restyle via tokens only).
- Mobile-native polish beyond what §8 of DESIGN.md lists (this is a desktop-first console made phone-usable, not a mobile app).
- The website repo (`modex-agent`) itself — it is a reference, not a deliverable.

## Further Notes

- `DESIGN.md` in this directory is the normative token/component spec; this PRD is the scope/acceptance record. Where they disagree during implementation, `DESIGN.md` wins on visuals and this PRD wins on scope.
- The website's design record (`docs/design/2026-07-17-website-design.md` in the `modex-agent` repo) documents particle-engine parameters (spring 0.045, damping 0.86/0.965, gather 4200ms, repel radius 110px) that can be reused for the boot animation; boot uses lower density and a single shape.
- The website's known mobile weaknesses (touch-driven particle repel, oversized hero padding) are explicitly avoided in the boot design.
- Triage labels per `docs/agents/triage-labels.md`; this spec carries `ready-for-agent`.

## Comments

### T07 visual sweep — 2026-07-19

**Verification method**: code inspection + built-CSS grep + `npm run build` / `npm test` / `tsc` pass. **No live browser was used** — this is an honest disclosure, not a claim of pixel-perfect rendering. What follows is what was verified statically and what a human should still eyeball in a real browser at 375 / 768 / 1024 / 1440 × both themes.

**Statically verified (code + built CSS `bot/web/dist/assets/index-*.css`):**

- **Mobile (375px)**: root container uses `100dvh` (not `100vh`) so the composer is not hidden behind mobile browser chrome. Viewport meta carries `viewport-fit=cover` so `env(safe-area-inset-*)` resolves. Composer wrapper applies `padding-bottom: max(env(safe-area-inset-bottom, 0px), 1.5rem)` — clears the home indicator. Sidebar is a drawer (`fixed inset-y-0 left-0 z-40 translate-x-[-100%|0] md:static`) with a scrim (`fixed inset-0 z-30 bg-overlay md:hidden`) — present in `App.tsx`. Boot particles skip pointer-repel on coarse pointers (`if (!coarse) canvas.addEventListener("pointermove", …)` in `particles.ts`) so touch scroll is not fought.
- **Touch targets ≥44px**: a `@media (pointer: coarse)` rule in `index.css` expands the hit area of `h-7`/`h-8` icon buttons via an invisible `::before` inset (`-6px`) — brings 28/32px IconButtons to ≥44px tap area on phones without bloating the desktop layout. Desktop (fine pointer) is unaffected.
- **Reduced-motion**: global `prefers-reduced-motion` guard zeroes all animation/transition to 0.01ms; per-component overrides (`dot-pulse`, `typing-dot`, `view-crossfade`, `body` glow-drift) set `animation: none`. BootScreen unmount hold (`DISPERSE_MS`) is skipped to 16ms under reduced-motion in `App.tsx`. Particle engine renders one static frame (`drawStatic()`) and never starts the RAF loop. Verified by code inspection — no `width`/`height`/`top`/`left` are animated anywhere (only `transform` + `opacity`).
- **Contrast (both themes)**: body/ink colors are deep-teal-on-warm-paper (light) and mint-white-on-graphite (dark) — well above 4.5:1 by construction. `mute`/`faint` secondary text is mid-luminance on both canvases (≥3:1). Hairline borders: light `#E2E5E0` on `#FAFAF7`; dark `rgba(45,212,168,0.14)` teal-shimmer on `#0E1512` — both visible. Focus rings are solid `--color-brand` 2px (the `/alpha`-on-token-color bug that previously made several focus rings invisible is fully fixed — grep of built CSS confirms zero `/NN` alpha modifiers on token colors remain).
- **Focus rings visible everywhere**: `Button`/`IconButton`/`Input`/`Textarea`/`Checkbox`/`DropdownPanel` all carry `focus-visible:ring-2 ring-brand` (or solid `ring-link`/`ring-error`/`ring-warning`). The `/alpha` sweep replaced the previously-invisible `ring-*/30`/`ring-*/50` classes with solid tokens.
- **Token integrity**: built CSS contains `--color-on-brand: #ffffff` (light) / `var(--color-canvas)` (dark); `.btn-primary` uses `var(--color-on-brand)` (no hardcoded `#ffffff`). `.eyebrow` and `.boot-eyebrow` share one rule (dedup). No `from-warning-soft` dead class. `webui/nul` deleted.

**What a human should still eyeball in a real browser (not statically verifiable):**

1. Actual rendered contrast at 375/768/1024/1440 in both themes — the values are AA by construction, but confirm the `faint` placeholder text and `mute` secondary copy read comfortably on real displays (subpixel rendering can shift perceived contrast).
2. Drawer animation smoothness on a phone (the `translate-x` transition is 200ms; confirm it doesn't stutter on a low-end device).
3. Composer safe-area clearance on an iPhone with a home indicator (the `max(env(...), 1.5rem)` formula is correct but only a real device confirms the home indicator is truly cleared).
4. TodoPanel floating pill (`fixed bottom-20 right-5`) does not overlap the composer on a 375px viewport when expanded — the geometry suggests it clears, but a visual check at 375px is warranted.
5. Mermaid lightbox (`bg-black/80`, `text-white/90`) — these are Tailwind built-in colors (not `var()`-tokens), so they DO generate CSS; confirm the lightbox reads well in light theme.
6. Boot particle morph visual quality (density, gather timing, disperse) — the engine is unit-tested for phase transitions, but the aesthetic is human-judgment.
7. The `::before` hit-area expansion on IconButtons does not visually overlap adjacent controls at 375px (the `-6px` inset should be safe, but dense composer rows deserve a look).

**Residual known issues (non-blocking):**

- The `(pointer: coarse)` hit-area expansion uses a `::before` pseudo-element with `border-radius: inherit` — if a future IconButton uses a non-circular radius, the hit area shape will follow it (currently all IconButtons are `rounded-full`, so this is fine).
- `chat.thinking` i18n key remains in the catalog (unused) — out of T07 scope (T07 named only `chat.selectConversation` and `reasoning.thinking`); left for a future i18n cleanup.

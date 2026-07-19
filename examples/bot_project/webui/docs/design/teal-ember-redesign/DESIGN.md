# ModexBot WebUI Design System — "Teal & Ember Console"

Status: implemented
Last revised: 2026-07-19 — dark theme replaced "Midnight Ink" (cyan/slate) with
"Warm Graphite" (neutral warm-grey canvas + the light theme's teal brand hue).
Reference: `docs/design/2026-07-17-website-design.md` in the `modex-agent` website repo
(implementation: `docs/stylesheets/extra.css`, `docs/stylesheets/home.css`, `docs/javascripts/particles.js`)

## 1. Design intent

The website speaks "Teal & Ember": a teal spine with an amber heartbeat. The WebUI
adopts the same language, tuned for a console: denser, content-first, motion serving
state changes rather than decoration.

Design principles:

1. **One brand, two themes** — the SAME teal hue family carries both themes
   (`#0D9488` light / `#2DD4BF` dark). Only lightness shifts between themes; the
   brand never changes hue. (The interim "Midnight Ink" cyan `#22D3EE` was dropped
   because it made the two themes read as two different products.)
2. **Neutral canvas, colored thread** — both canvases are near-neutral (warm paper
   light / warm graphite dark). The teal accent is the single colored thread;
   borders and scrollbars stay hue-free so they never tint every surface.
3. **Dark is the default** (developer console), light is a first-class equal, not
   an inversion.

## 2. Color tokens

Single source of truth: `src/index.css` (`:root` / `.dark`), mapped by `tailwind.config.js`.
Derived shades are computed with `color-mix(in srgb, …)` from these tokens — never
hard-code raw hex in components. `src/index.tokens.test.ts` pins the canonical
values so the palette cannot silently drift.

### 2.1 Core palette

| Token | Light (DAY) | Dark (NIGHT, default) | Usage |
|---|---|---|---|
| `--color-canvas` | `#F6F7F5` | `#1B1B1D` | app background (warm paper / warm graphite) |
| `--color-canvas-sidebar` | `#F0F1EF` | `#141416` | sidebar, recessed zones |
| `--color-canvas-elevated` | `#FFFFFF` | `#232326` | cards, bubbles, panels |
| `--color-canvas-popover` | `#FFFFFF` | `#1F1F22` | dropdowns, modals, toasts |
| `--color-ink` | `#1A2B26` | `#F5F5F4` | primary text |
| `--color-bright` | `#0E1512` | `#FFFFFF` | highest-contrast headings |
| `--color-body` | `#2E443C` | `#D6D3D1` | secondary text |
| `--color-mute` | `#5A6E66` | `#A8A29E` | tertiary text |
| `--color-faint` | `#8AA096` | `#78716C` | placeholders, disabled |
| `--color-brand` | `#0D9488` | `#2DD4BF` | primary accent — same teal hue, lifted for dark |
| `--color-brand-deep` | `#0F766E` | `#14B8A6` | pressed/hover accent |
| `--color-brand-bright` | `#14B8A6` | `#5EEAD4` | glows, highlights |
| `--color-ember` | `#B45309` | `#F59E0B` | secondary accent: badges, metric values, rare highlights |
| `--color-danger` | `#DC2626` | `#F87171` | errors, destructive |
| `--color-warning` | `#B45309` | `#FBBF24` | warnings (= ember family) |
| `--color-success` | `#0D9488` | `#2DD4BF` | success (= brand) |
| `--color-on-brand` | `#FFFFFF` | `var(--color-canvas)` | text on brand fill/gradient |

The dark canvas ladder (`#1B1B1D` / `#141416` / `#232326` / `#1F1F22`) is the
Linear/Raycast warm-graphite family: neutral, no blue or teal cast, easy on the
eyes for long sessions. Dark text uses the stone family (warm neutrals).

### 2.2 Borders — the "neutral shimmer" rule

- Light: neutral `--color-hairline: #E1E4E0`, `--color-border-strong: #CBD3CE`,
  `--color-hairline-soft: #EFF1ED`.
- Dark: **hue-free white-alpha borders** — `--color-hairline: rgba(255,255,255,.08)`,
  `--color-border-strong: rgba(255,255,255,.16)`,
  `--color-hairline-soft: rgba(255,255,255,.04)`. Dark scrollbars follow the same
  rule (`rgba(255,255,255,.12/.2)`). No border or scrollbar in dark mode carries a
  brand tint; this keeps the teal accent reading as the single colored thread.
  (Supersedes the original "teal shimmer" brand-tinted border rule.)

### 2.3 Functional / domain colors

- Approval severities: normal = brand 30%/40% alpha, sensitive = ember,
  dangerous = danger, hardline = `#9F1239` light / `#FB7185` dark.
- Selection: `rgba(20,184,166,.25)` light / `rgba(45,212,191,.28)` dark.
- Settings domain category colors — 6 distinct hues, rebalanced per theme to sit
  beside brand/ember in one saturation/lightness family:

| Category | Light | Dark | Hue |
|---|---|---|---|
| `--color-cat-mcp` | `#C26A3E` | `#E89B6B` | terracotta |
| `--color-cat-pools` | `#2A8E9E` | `#3B82F6` | blue (kept distinct from brand teal) |
| `--color-cat-skills` | `#6D5AD0` | `#A78BFA` | indigo |
| `--color-cat-models` | `#C5377A` | `#F472B6` | magenta |
| `--color-cat-im` | `#6B9F2E` | `#A3D94C` | olive / lime |
| `--color-cat-prompts` | `#0D9488` | `#2DD4BF` | brand teal — prompts are the "home" domain |

- Code syntax highlighting: `vscDarkPlus` (Prism) in dark — a neutral-grey code
  palette that sits cleanly on the warm-graphite canvas; `oneLight` in light.
  (The interim `oneDark` was dropped: its purple cast clashed with the canvas.)
  The block chrome (header bar, borders) comes from tokens, only the syntax
  colors come from the Prism theme.

## 3. Typography

### 3.1 Font families

Single-family system (Google Fonts, `font-display: swap`, `Noto Sans SC` CJK
fallback). One sans family carries both display and body roles; weight +
tracking differentiate tiers, not family. This keeps payload low and visual
unity high.

| Role | Font | Weights | Usage |
|---|---|---|---|
| Display + Body | **Inter** | 400 / 500 / 600 / 700 | all UI text, messages, forms, brand wordmark, page titles, section headers, boot headline |
| Mono | **JetBrains Mono** | 400 / 500 / 600 | eyebrow labels, chips, paths, badges, inline code, code blocks, statusline path values |

- `font-display` / `font-sans` in `tailwind.config.js` both resolve to Inter
  (display is a semantic alias kept so existing `font-display` usages work).
  Pair `font-display` with `font-bold` + `tracking-tight` for heading tiers.
- `font-mono` resolves to JetBrains Mono.
- **Removed**: Geist, Geist Mono, Space Grotesk — replaced by the Inter system.
  `index.tokens.test.ts` guards against their return.

### 3.2 Type scale — 7 tiers, token-driven

Single source of truth in `index.css` `:root` / `.dark` as `--text-*` CSS
variables; `tailwind.config.js` maps `text-xs/sm/base/md/lg/xl/2xl` to them.
**No `text-[Npx]` arbitrary values, no hardcoded `font-size:` in components.**

| Token class | `--text-*` | px | Default line-height | Used for |
|---|---|---|---|---|
| `text-xs` | `--text-xs` | 11 | `--leading-tight` (1.2) | eyebrow, statusline, badge, tiny meta, tier labels |
| `text-sm` | `--text-sm` | 12 | `--leading-snug` (1.45) | page-sub, helper text, chip, secondary meta |
| `text-base` | `--text-base` | 13 | `--leading-snug` (1.45) | default UI, nav-item, form label, inline code, button text, list items |
| `text-md` | `--text-md` | 15 | `--leading-relaxed` (1.6) | chat body, prose, composer textarea, message text |
| `text-lg` | `--text-lg` | 19 | `--leading-tight` (1.2) | page-title, card title, modal title, H2 in prose |
| `text-xl` | `--text-xl` | 24 | `--leading-tight` (1.2) | section header, empty-state headline, H1 in prose |
| `text-2xl` | `--text-2xl` | 32 | `--leading-tight` (1.2) | boot headline |

### 3.3 Line-height — 4 tiers

`--leading-tight` (1.2) · `--leading-snug` (1.45) · `--leading-relaxed` (1.6) ·
`--leading-prose` (1.7). The prose tier is reserved for `.prose-chat` long-form
markdown; all other body text uses relaxed or snug.

### 3.4 Letter-spacing — 4 tiers

`--tracking-tight` (-0.01em, display headings) · `--tracking-normal` (0, body) ·
`--tracking-wide` (0.02em, brand wordmark) · `--tracking-eyebrow` (0.28em,
eyebrow signature). The eyebrow tracking is the single widest tracking in the
system and pairs exclusively with `font-mono` + `uppercase` + `text-xs`.

### 3.5 Signature eyebrow label

JetBrains Mono, `text-xs` (11px), uppercase, `tracking-eyebrow` (0.28em), brand
color. Used for section labels, reasoning/tool-trace headers, boot status,
settings group headers. The `.eyebrow-muted` variant swaps brand color for
`--color-mute`. Both derive from the same token set — only color differs.

### 3.6 Ink-color ladder — semantic roles

Four-tier text-color ladder (`--color-*`), each with a fixed semantic role.
**Body text is always `ink`/`bright`; auxiliary is always `mute`; only
non-interactive decoration uses `faint`.** `faint` never carries information
the user must read. Dark values are the stone family (see §2.1).

### 3.7 Markdown prose (`.prose-chat`)

Chat message body uses `--text-md` (15px) + `--leading-prose` (1.7) for
long-answer readability. Heading hierarchy (H1–H6) uses the display font +
tight tracking, scaled: H1 = `text-xl`, H2 = `text-lg`, H3–H6 = `text-md`.
Inline code drops to `--text-base` (13px) mono so it reads as part of the
sentence; code blocks use `--text-base` mono with `--leading-snug`. Tables use
`--text-base` + `--leading-snug` for density. Blockquotes, links, and `strong`
all derive color from the ink ladder.

## 4. Shape, elevation, motion tokens

### Radius — one scale, no exceptions

| Token | Value | Used for |
|---|---|---|
| `--radius-xs` | 6px | checkboxes, tiny chips, bubble tail corners |
| `--radius-sm` | 8px | small buttons, inputs, session rows |
| `--radius-md` | 12px | buttons, cards, dropdown panels |
| `--radius-lg` | 16px | message bubbles, composer, modals |
| `--radius-pill` | 999px | chips, tags, status dots containers, ModelSelector trigger |

### Elevation (dark: hover glow carries a teal tint)

- `card`: `0 1px 3px rgba(0,0,0,.05)` light / `0 1px 3px rgba(0,0,0,.35)` dark
- `card-hover`: + `0 0 1.5rem color-mix(brand 12%)` glow on hoverable cards
- `popover`: `0 8px 24px rgba(0,0,0,.12)` light / `0 12px 32px rgba(0,0,0,.5)` dark

### Motion — same rhythm everywhere

- `--dur-fast: 150ms`, `--dur: 220ms`, `--dur-slow: 350ms`
- `--ease-out: cubic-bezier(.2,.7,.2,1)` (enter), exits at ~65% duration ease-in
- Hover lift: `translateY(-1px)` buttons; press: scale .98
- Stagger: 50ms per sibling (lists, boot elements)
- Only `transform` + `opacity` animate; a global `prefers-reduced-motion` guard
  zeroes all of it.

## 5. Component spec (convergence)

### 5.1 Buttons (one `Button`, restyled)

- Primary: gradient `linear-gradient(120deg, brand, brand-bright)`,
  `--color-on-brand` text (white light / dark-canvas-ink dark), glow shadow
  `0 .5rem 1.5rem color-mix(brand 18%)`, hover lift -1px + brighter glow.
  Radius `md`. One primary CTA per area.
- Ghost: elevated bg + 1px hairline; hover border → `border-strong`.
- Danger: semantic danger, never gradient. Disabled: opacity .45, no pointer.

### 5.2 Inputs

Label above (13px medium), input 36px, radius `sm`, elevated bg, hairline border;
focus: 2px brand ring + border-brand; helper text below (12px mute); error below field
(12px danger + icon). No placeholder-only labels.

### 5.3 Dropdowns — one visual spec

- Trigger: radius `sm` (form) or `pill` (inline/model), chevron rotates 180° on open.
- Panel: popover bg, 1px hairline, radius `md`, popover shadow, opens with 150ms
  fade + `translateY(±4px→0)` from the trigger direction (`.dropdown-panel-enter`).
- Item: 32px row, hover = brand tint; selected = 2px brand bar left + check icon.
- Group headers: sticky mono eyebrow. Full keyboard nav.

### 5.4 Checkbox / dialog / toast

Checkbox: custom SVG check, radius `xs`, brand fill, 180ms check-draw; the check
mark is white on the deep light-theme teal, dark-canvas ink on the bright
dark-theme teal (stroke hex mirrors `--color-canvas`, hardcoded inside the
data-URI — the one deliberate exception to the no-hex rule). Modals: popover bg,
radius `lg`, scrim `rgba(0,0,0,.5)`, enter = scale .96→1 + fade 180ms. Toasts:
popover bg + hairline, brand/ember/danger icon dot, auto-dismiss, `aria-live="polite"`.

## 6. Chat surface

- **Assistant messages**: elevated surface (`--color-canvas-elevated`) with a 1px
  hairline border and a 2px brand-alpha left rail, `radius-lg` with a `tl-xs`
  tail corner, max-width 72ch — a "document" feel, distinct from both the canvas
  and the user's branded tint bubble.
- **User bubble**: right-aligned, radius `lg` with `xs` tail corner; light =
  brand 8% tint bg + brand-deep text + brand 22% border; dark = elevated bg +
  brand-bright text + `rgba(45,212,191,.28)` border. Max-width 72ch.
- **Reasoning block**: collapsible, mono eyebrow + chevron, 2px brand-alpha left
  border, dim text.
- **ToolTraceCard / ApprovalCard**: elevated card, radius `md`, severity color
  only in a 2px inset left bar + status icon — no full-bleed color washes.
  Approval actions = primary (approve) + ghost (reject), `deny_reason` as helper text.
- **Composer**: floating, radius `lg`, elevated bg, hairline border; focus-within
  → 2px brand ring + subtle brand glow. Send = primary gradient.
- **Typing indicator**: three brand dots, 1.2s staggered pulse.

## 7. Boot experience

Three phases, all branded, smooth hand-off between them:

1. **Pre-React (index.html inline)**: background already set to the *saved theme's*
   canvas color (no white flash in dark mode), centered logo-icon with a slow
   breathing glow — no spinner, no text.
2. **BootScreen**: full-canvas **particle morph** (`src/lib/particles.ts`, vanilla,
   zero-dep, DPR-capped, IO/visibility paused, reduced-motion → static frame).
   Particles drift → gather into the logo mark (hold while connecting) → on
   `ready`, disperse radially as the app fades in. Palette: dark = firefly teal
   (`#5EEAD4` / `#2DD4BF` / `#99F6E4`, matching the dark brand ladder) + ember
   (`#FBBF24` / `#F59E0B`) with glow; light = deepened teals, no glow.
   Below the stage: mono eyebrow status line; 24s+ error state with retry.
3. **Entry**: app fades in 250ms with sidebar/session list staggering at 50ms
   (one-time per cold start).

## 8. Layout & chrome

- **Statusline**: 32px, chrome tone halfway between canvas and sidebar; brand
  wordmark in display font (Inter 700, `tracking-wide`) + connection dot
  (brand glow / dim).
- **Sidebar**: `canvas-sidebar` bg, hairline right border. Session rows: radius
  `sm`, hover = brand 6–8% tint over sidebar, active = brand 10–12% tint + 2px
  brand left bar (solid tokens — `/alpha` modifiers on `var()` colors generate
  no CSS in this Tailwind setup).
- **Settings**: two-column shell; category chips recolored per §2.3; active nav
  item = inset 2px `--cat` bar + 9% `--cat` gradient; `ActionBar` sticky bottom
  with backdrop blur + top hairline; unsaved-changes = ember dot.
- **Page transitions**: chat ↔ settings crossfade ~200ms.
- **Mobile**: drawer sidebar with scrim; composer respects `safe-area-inset-bottom`;
  touch targets ≥44px (invisible `::before` hit-area expansion on coarse pointers);
  layout uses `100dvh`.

## 9. Accessibility floor

- Contrast: body ≥4.5:1, secondary ≥3:1 in both themes. The dark brand `#2DD4BF`
  on canvas `#1B1B1D` and stone-family text on the graphite ladder were chosen
  to meet this; `brand-bright` is for large text/glow only.
- Focus-visible: 2px brand ring everywhere.
- Color never the only signal: severity/status always pair icon + text.
- `prefers-reduced-motion`: particles render one static frame; all UI motion zeroed.
- `color-scheme: light/dark` set per theme so native controls follow.

## 10. i18n

All user-facing copy goes through the i18n catalog (`src/i18n/`). No hardcoded
display strings (the pre-React loader uses no text at all).

## 11. Revision history

| Date | Change |
|---|---|
| 2026-07-18 | Initial "Teal & Ember Console" spec (P0–P5 implemented): token system, boot experience, component convergence, chat surface, chrome. Dark = warm graphite w/ teal undertone + "teal shimmer" borders + logo teal `#2DD4A8`. |
| 2026-07-19 | Dark theme iteration "Midnight Ink" (slate-blue canvas + cyan `#22D3EE`) — prototyped, then rejected: brand hue split across themes, cold blue cast, `oneDark` purple clash. Never shipped to main. |
| 2026-07-19 | Dark theme finalized as "Warm Graphite": neutral warm-grey canvas, brand unified on the light theme's teal hue (`#2DD4BF`), neutral white-alpha borders/scrollbars, `vscDarkPlus` code theme. Chosen from a 6-variant interactive prototype reviewed against the live homepage layout. |

# ModexBot WebUI Design System — "Teal & Ember Console"

Status: proposed (2026-07-18)
Supersedes: the stale "Notion-inspired" section in `webui/AGENTS.md`
Reference: `docs/design/2026-07-17-website-design.md` in the `modex-agent` website repo
(implementation: `docs/stylesheets/extra.css`, `docs/stylesheets/home.css`, `docs/javascripts/particles.js`)

## 1. Design intent

The website speaks "Teal & Ember": a teal spine (brand `#2DD4A8`, the exact logo color)
with an amber heartbeat. The WebUI adopts the same language, tuned for a console:
denser, content-first, motion serving state changes rather than decoration.

Three problems this redesign fixes:

1. **Brand drift** — UI accent was emerald `#059669`; the logo is `#2DD4A8`. Now unified.
2. **Dead boot experience** — a bare spinner saying "Connecting to backend…". Replaced by
   a branded particle-morph boot sequence (§7).
3. **Fragmented control language** — three independent dropdown implementations, three
   radius scales, buttons/inputs/cards drifting apart. Converged to one spec (§5, §6).

Dark theme is the default (developer console, and the brand glow lives best in the dark);
light theme is a first-class equal, not an inversion.

## 2. Color tokens

Single source of truth: `src/index.css` (`:root` / `.dark`), mapped by `tailwind.config.js`.
Derived shades are computed with `color-mix(in srgb, …)` from these tokens — never
hard-code raw hex in components.

### 2.1 Core palette

| Token | Light (DAY) | Dark (NIGHT, default) | Usage |
|---|---|---|---|
| `--color-canvas` | `#FAFAF7` | `#0E1512` | app background (warm paper / warm graphite w/ teal undertone) |
| `--color-canvas-sidebar` | `#F4F5F2` | `#0B1210` | sidebar, recessed zones |
| `--color-canvas-elevated` | `#FFFFFF` | `#18201D` | cards, bubbles, panels |
| `--color-canvas-popover` | `#FFFFFF` | `#131B18` | dropdowns, modals, toasts |
| `--color-ink` | `#1A2B26` | `#E6F2ED` | primary text (mint-white in dark) |
| `--color-body` | `#2E443C` | `#B9CFC7` | secondary text |
| `--color-mute` | `#5A6E66` | `#8FA69D` | tertiary text |
| `--color-faint` | `#8AA096` | `#5F7A70` | placeholders, disabled |
| `--color-brand` | `#0D9488` | `#2DD4A8` | primary accent = logo teal |
| `--color-brand-deep` | `#0F766E` | `#14B8A6` | pressed/hover accent |
| `--color-brand-bright` | `#14B8A6` | `#5EEAD4` | glows, highlights |
| `--color-ember` | `#B45309` | `#F5A524` | secondary accent: badges, metric values, rare highlights |
| `--color-danger` | `#DC2626` | `#F87171` | errors, destructive |
| `--color-warning` | `#B45309` | `#FBBF24` | warnings (= ember family) |
| `--color-success` | `#0D9488` | `#2DD4A8` | success (= brand) |

### 2.2 Borders — the "teal shimmer" rule

- Light: neutral `--color-hairline: #E2E5E0`, `--color-border-strong: #CBD3CE`.
- Dark: **brand-tinted alpha borders** — `--color-hairline: rgba(45,212,168,.14)`,
  `--color-border-strong: rgba(45,212,168,.28)`. Every container in dark mode carries a
  faint teal shimmer; this is the signature look and must be applied consistently.

### 2.3 Functional / domain colors

- Approval severities keep 4 levels, re-derived: normal = mute, sensitive = ember,
  dangerous = danger, hardline = `#9F1239`/`#FB7185`.
- Settings domain category colors (mcp/pools/skills/models/im/prompts): keep 6 distinct
  hues but rebalance to the same saturation/lightness family as brand/ember so chips feel
  like one set. Exact values fixed in implementation pass P4.
- Selection: `rgba(45,212,168,.30)` dark / `rgba(20,184,166,.25)` light.

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
Every tier pairs with a default line-height so `text-{tier}` alone yields the
right leading.

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
the user must read.

| Token | Light | Dark | Semantic role | Used for |
|---|---|---|---|---|
| `--color-bright` | `#0E1512` | `#FFFFFF` | highest-contrast heading | page-title, boot headline, modal title, prose `strong`/`th` |
| `--color-ink` | `#1A2B26` | `#E6F2ED` | primary text | message body, form input values, nav-item.active, prose body |
| `--color-body` | `#2E443C` | `#B9CFC7` | secondary text / default control | nav-item default, card body, form label, agent label |
| `--color-mute` | `#5A6E66` | `#8FA69D` | auxiliary / meta | page-sub, helper text, statusline, eyebrow-muted, trace-card meta |
| `--color-faint` | `#8AA096` | `#5F7A70` | placeholder / disabled / decoration | placeholder text, disabled, breadcrumb separators |

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
| `--radius-xs` | 6px | checkboxes, tiny chips |
| `--radius-sm` | 8px | small buttons, inputs |
| `--radius-md` | 12px | buttons, cards, dropdown panels |
| `--radius-lg` | 16px | message bubbles, composer, modals |
| `--radius-pill` | 999px | chips, tags, status dots containers, ModelSelector trigger |

Removes the current 6/16/full split (Button 6px vs composer 16px vs selector full).

### Elevation (dark: shadows carry a teal tint)

- `card`: `0 1px 3px rgba(0,0,0,.05)` light / `0 1px 3px rgba(0,0,0,.35)` dark
- `card-hover`: + `0 0 1.5rem color-mix(brand 12%)` glow on hoverable cards
- `popover`: `0 8px 24px rgba(0,0,0,.12)` light / `0 12px 32px rgba(0,0,0,.5)` dark

### Motion — same rhythm everywhere

- `--dur-fast: 150ms`, `--dur: 220ms`, `--dur-slow: 350ms`
- `--ease-out: cubic-bezier(.2,.7,.2,1)` (enter), exits at ~65% duration ease-in
- Hover lift: `translateY(-2px)` cards / `-1px` buttons; press: scale .98
- Stagger: 50ms per sibling (lists, boot elements)
- Only `transform` + `opacity` animate; `prefers-reduced-motion` zeroes all of it
  (already enforced globally — keep and extend).

## 5. Component spec (convergence)

### 5.1 Buttons (one `Button`, restyled)

- Primary: gradient `linear-gradient(120deg, brand, brand-bright)`, ink-colored text
  (dark) / white (light), glow shadow `0 .5rem 1.5rem color-mix(brand 18%)`,
  hover lift -1px + brighter glow. Radius `md`. Min height 36px (44px on touch).
- Ghost: elevated bg + 1px hairline; hover border → `border-strong` (teal shimmer).
- Danger: semantic danger, never gradient. Disabled: opacity .45, no pointer.

### 5.2 Inputs

Label above (13px medium), input 36px, radius `sm`, elevated bg, hairline border;
focus: 2px brand ring + border-brand; helper text below (12px mute); error below field
(12px danger + icon). No placeholder-only labels.

### 5.3 Dropdowns — one visual spec, at most two implementations

All three current implementations (`Select`, `SelectMenu`, `ModelSelector`) converge to
this spec; the native `<select>` wrapper is dropped entirely:

- Trigger: radius `sm` (form) or `pill` (inline/model), chevron rotates 180° on open.
- Panel: popover bg, 1px hairline (teal shimmer in dark), radius `md`, popover shadow,
  opens with 150ms fade + `translateY(-4px→0)` from trigger direction.
- Item: 32px row, hover = brand 8% tint; selected = 2px brand bar left + check icon.
- Group headers: sticky mono eyebrow (as ModelSelector already does).
- Full keyboard nav (arrows/Home/End/Esc/typeahead) — SelectMenu already has it; port to all.

Target end state: one shared `DropdownPanel` primitive + two triggers (form field vs
inline pill). ModelSelector keeps its grouping, sidebar pool picker keeps its density.

### 5.4 Checkbox / dialog / toast

Checkbox: keep custom SVG check, restyle to radius `xs`, brand fill, 180ms check-draw.
Modals: popover bg, radius `lg`, scrim `rgba(0,0,0,.5)`, enter = scale .96→1 + fade 180ms
from trigger. Toasts: popover bg + hairline, brand/ember/danger icon dot, auto-dismiss 4s,
`aria-live="polite"`.

## 6. Chat surface

- **Assistant messages**: subtle surface (not a full bubble, not bare canvas) —
  `brand-soft` tint bg + 2px brand left rail, `radius-lg` with a `tl-xs` tail corner,
  max-width 72ch. Avatar is the `Bot` glyph (lucide) in a brand-tinted circular badge,
  matching the chat header's agent indicator — not the project logo.
- **User bubble**: right-aligned, radius `lg` with 6px tail corner, dark =
  `elevated` bg + brand-alpha border + brand-bright text; light = brand 8% tint bg +
  brand-deep text. Max-width 72ch.
- **Reasoning block**: collapsible, mono eyebrow "REASONING" + chevron, hairline left
  border 2px brand-alpha, dim text.
- **ToolTraceCard**: elevated card, radius `md`, mono eyebrow header, severity color only
  in a 3px left bar + status icon — no full-bleed color washes.
- **ApprovalCard**: elevated + severity left bar; actions = primary (approve) + ghost
  (reject), `deny_reason` shown as helper text.
- **Composer**: floating, radius `lg`, elevated bg, hairline border; focus → brand ring +
  subtle glow. Buttons inside: icon ghost buttons, send = primary gradient circle.
- **Empty state** (no conversation selected): logo-icon watermark (brand, 8% opacity,
  96px) + display-font headline + 2–3 mono-eyebrow hints — replaces the single bare line.
- **Typing indicator**: three brand dots, 1.2s pulse stagger (replaces plain spinner
  where applicable); streaming keeps the existing typewriter.

## 7. Boot experience (the centerpiece fix)

Three phases, all branded, smooth hand-off between them:

1. **Pre-React (index.html inline)**: background already set to the *saved theme's*
   canvas color (fixes the white flash in dark mode), centered logo-icon.svg with a slow
   breathing glow — no spinner, no text. ~10 lines of inline SVG/CSS.
2. **BootScreen**: full-canvas **particle morph** — port of the website's
   `particles.js` (vanilla, zero-dep, DPR-capped, IO/visibility paused,
   reduced-motion → static frame). Sequence: particles drift loosely → gather into the
   star-topology logo mark (hold while connecting) → on `ready`, disperse radially as the
   app fades in (spatial continuity: the logo "explodes" into the UI).
   Below the stage: mono eyebrow status line with the existing staged copy
   ("STARTING BACKEND…" → "CONNECTING…" → "STILL STARTING…"), plus a 24s+ error state:
   hairline card with the raw error in mono + a primary "Retry" button.
   Particle density: ≤600 desktop / ≤300 mobile (boot is transient — keep it cheap).
3. **Entry**: App fades in 250ms with sidebar/session list staggering in at 50ms
   (one-time per cold start).

## 8. Layout & chrome

- **Statusline**: keep 32px; add logo-icon.svg (14px, brand) before the "ModexBot"
  wordmark (Space Grotesk). Connection dot keeps brand/mute states + glow.
- **Sidebar**: `canvas-sidebar` bg, hairline right border (teal shimmer dark). Workspace
  path in mono. Session rows: radius `sm`, hover brand 6% tint, active = brand 10% tint +
  2px brand left bar. "New Conversation" = primary gradient button (the only gradient
  button in chrome).
- **Settings**: keep two-column shell. Group headers = mono eyebrow; category chips
  recolored per §2.3; `ActionBar` sticky bottom with backdrop blur + top hairline;
  unsaved-changes indicator (ember dot).
- **Page transitions**: chat ↔ settings crossfade 200ms (no more instant swap).
- **Mobile** (the website's weakness — do better here): drawer sidebar with 40% scrim;
  composer respects `safe-area-inset-bottom`; all touch targets ≥44px; boot particles
  ignore touch-drag (no repel on scroll); layout uses `100dvh`.

## 9. Accessibility floor

- Contrast: body ≥4.5:1, secondary ≥3:1 in both themes (verify mint-white on `#18201D`
  and brand on dark — brand-bright is for large text/glow only, body links in dark use
  brand-bright).
- Focus-visible: 2px brand outline + 3px offset everywhere (matches website).
- Color never the only signal: severity/status always pair icon + text.
- `prefers-reduced-motion`: particles render one static frame; all UI motion zeroed.

## 10. i18n

Stays en-only for now; all new copy (boot stages, empty state, retry) goes through the
i18n catalog. No hardcoded display strings (existing rule — now also applies to the
pre-React loader, which uses no text at all).

## 11. Implementation phases (subsequent work)

| Phase | Scope |
|---|---|
| P0 tokens | Rewrite `index.css` token set, tailwind mapping, fonts (add Space Grotesk), radius/motion tokens, dark-default |
| P1 boot | Pre-React loader + BootScreen particle morph + entry transition |
| P2 primitives | Button/Input/Card/Checkbox/Toast restyle; DropdownPanel convergence (Select/SelectMenu/ModelSelector) |
| P3 chat | Bubbles, reasoning, tool trace, approval, composer, empty state, typing indicator |
| P4 chrome | Sidebar, statusline, settings pages, category colors, logo-icon integration (copy `assets/logo-icon.svg` → `webui/public/`) |
| P5 polish | Mobile pass, reduced-motion audit, contrast audit, docs (`webui/AGENTS.md` design section rewrite, delete stale DESIGN-notion reference, remove stray `webui/nul`) |

Out of scope: new features, routing, state-management changes, backend changes.

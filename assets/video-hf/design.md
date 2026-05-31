# ModexAgent Design System

## Palette — Dark Premium (Developer Tools)

- **bg**: #0A0E17 — deep navy canvas
- **fg**: #E8ECF1 — warm white text
- **accent**: #2DD4A8 — teal green (primary brand)
- **accent-dim**: #1A6B56 — muted accent for decoratives
- **accent-warm**: #F59E4B — warm orange (secondary, for highlights)
- **surface**: #141B2D — card/surface background
- **surface-raised**: #1A2338 — elevated surface
- **muted**: #64748B — secondary text
- **border**: #1E293B — subtle dividers

## Typography

- **Display**: "Sora" (700 weight) — headlines, hero text. (Exception to ban rule — Sora's geometric precision fits the developer tool aesthetic when paired correctly.)
- **Body**: "DM Mono" (400 weight) — code, labels, technical data
- **Code accent**: "JetBrains Mono" (500 weight) — inline code in UI mockups

Weight contrast: 700 (headlines) vs 400 (body) — extreme enough for video.

## Motion Character

- Easing signature: `power3.out` for entrances, `expo.out` for reveals, `power2.inOut` for transitions
- Duration range: 0.3s (quick labels) to 0.8s (hero reveals)
- Stagger: 80-120ms between related elements
- Ambient: slow radial glow breathing (scale 1.0 ↔ 1.04, 4s cycle)

## Corners
- Cards/surfaces: 8px border-radius

## Do's
- Dark navy canvas with teal-green accent glow
- Radial glows as background texture (15-25% opacity)
- Monospace for all code/terminal elements
- Split-frame layouts: content left, decorative right

## Don'ts
- No purple or blue gradients (AI slop territory)
- No emoji as icons
- No pure white (#fff) — always tint warm
- No left-edge accent stripes on cards

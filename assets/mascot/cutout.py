"""Cut mascot sprites out of GPT-generated line-art sheets.

The artwork is single-color line art on a near-black background. GPT renders
stroke brightness unevenly, so we do NOT trust per-pixel color: alpha is
stroke coverage (intensity normalized against a high percentile of the
stroke's own distribution, so anti-aliased edges get fractional alpha while
the stroke body is fully opaque), and every foreground pixel gets one uniform
brand color. The result composites identically clean on dark and light.
"""

from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

SRC = Path.home() / "Downloads"
OUT = Path("output/logo-drafts/cutout")

NOISE = 10.0  # max-channel residue below this is treated as pure background
PAD = 24
CANVAS = 512
ANCHOR = (0.5, 0.56)


def corner_color(rgb: np.ndarray) -> np.ndarray:
    h, w, _ = rgb.shape
    s = max(6, min(h, w) // 24)
    patches = [rgb[:s, :s], rgb[:s, -s:], rgb[-s:, :s], rgb[-s:, -s:]]
    return np.median(np.concatenate([p.reshape(-1, 3) for p in patches]), axis=0)


def cutout_dark(img: Image.Image, color: tuple[int, int, int] = (45, 212, 168)) -> Image.Image:
    """Line-art keying: strokes are ONE uniform color; alpha carries only the
    stroke coverage (anti-aliasing). GPT renders stroke brightness unevenly —
    trusting per-pixel color keeps that mottle, so we discard color entirely:
    alpha = strength normalized against a high percentile of the stroke's own
    intensity distribution (edge AA is a minority, so most of the stroke
    lands at full coverage), color = the brand teal everywhere."""
    rgb = np.asarray(img.convert("RGB")).astype(np.float64)
    bg = corner_color(rgb)
    rem = np.clip(rgb - bg, 0, None)          # remove uniform background
    strength = rem.max(axis=2)                 # stroke intensity

    fg = strength[strength > NOISE]
    if fg.size == 0:
        return Image.fromarray(np.dstack([rgb, np.zeros_like(strength)]).astype(np.uint8), "RGBA")
    pivot = np.percentile(fg, 65)              # typical full-stroke intensity
    alpha = np.clip(strength / pivot, 0, 1)
    alpha = np.power(alpha, 0.8)               # fatten the AA ramp a touch
    alpha[strength <= NOISE] = 0.0

    # despeckle: drop faint detached fragments; keep substantial components
    labels, _ = ndimage.label(alpha > 0.05)
    idx = range(1, labels.max() + 1)
    masses = ndimage.sum(alpha, labels, idx)
    peaks = ndimage.maximum(alpha, labels, idx)
    keep = {i + 1 for i, (m, p) in enumerate(zip(masses, peaks)) if m >= 800 or p >= 0.5}
    alpha[~np.isin(labels, list(keep))] = 0.0

    uniform = np.zeros_like(rgb)
    uniform[..., 0], uniform[..., 1], uniform[..., 2] = color
    rgba = np.dstack([uniform, alpha * 255]).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


def crop_content(img: Image.Image) -> Image.Image:
    a = np.asarray(img)[:, :, 3]
    ys, xs = np.nonzero(a > 10)
    if len(xs) == 0:
        return img
    x0, x1 = max(0, xs.min() - PAD), min(img.width, xs.max() + PAD)
    y0, y1 = max(0, ys.min() - PAD), min(img.height, ys.max() + PAD)
    return img.crop((x0, y0, x1, y1))


def align(img: Image.Image) -> Image.Image:
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    a = np.asarray(img)[:, :, 3].astype(np.float64)
    total = a.sum()
    if total == 0:
        canvas.paste(img, ((CANVAS - img.width) // 2, (CANVAS - img.height) // 2), img)
        return canvas
    ys, xs = np.nonzero(a > 0)
    cx = float((a[ys, xs] * xs).sum() / total)
    cy = float((a[ys, xs] * ys).sum() / total)
    canvas.paste(img, (round(CANVAS * ANCHOR[0] - cx), round(CANVAS * ANCHOR[1] - cy)), img)
    return canvas


def contact_sheet(frames: list[Image.Image], path: Path) -> None:
    cols = 3
    rows = (len(frames) + cols - 1) // cols
    cell = 300
    for name, color in [("dark", (27, 27, 29, 255)), ("light", (246, 247, 245, 255))]:
        sheet = Image.new("RGBA", (cols * cell, rows * cell), color)
        for i, f in enumerate(frames):
            thumb = f.copy()
            thumb.thumbnail((cell - 16, cell - 16), Image.LANCZOS)
            x = (i % cols) * cell + (cell - thumb.width) // 2
            y = (i // cols) * cell + (cell - thumb.height) // 2
            sheet.paste(thumb, (x, y), thumb)
        sheet.convert("RGB").save(path.parent / f"{path.stem}-{name}.png")


def main() -> None:
    (OUT / "frames").mkdir(parents=True, exist_ok=True)

    # puppy sprite sheet: 2 rows x 4 cols (1774x887 → non-integer cells)
    sheet = Image.open(SRC / "1.png")
    cols, rows = 4, 2
    xs = [round(i * sheet.width / cols) for i in range(cols + 1)]
    ys = [round(i * sheet.height / rows) for i in range(rows + 1)]
    frames = []
    for i in range(cols * rows):
        cell = sheet.crop((xs[i % cols], ys[i // cols], xs[i % cols + 1], ys[i // cols + 1]))
        frame = align(crop_content(cutout_dark(cell)))
        frame.save(OUT / "frames" / f"f{i + 1}.png")
        frames.append(frame)
    contact_sheet(frames, OUT / "sheet-frames.png")

    frames[0].save(OUT / "still-dark.png")

    # light-theme still: same frame cut again directly in the light brand teal
    cell0 = sheet.crop((xs[0], ys[0], xs[1], ys[1]))
    light = align(crop_content(cutout_dark(cell0, color=(13, 148, 136))))
    light.save(OUT / "still-light.png")
    contact_sheet([frames[0], light], OUT / "sheet-stills.png")


if __name__ == "__main__":
    main()

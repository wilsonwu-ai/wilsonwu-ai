#!/usr/bin/env python3
"""Turn a portrait photo into the ASCII portrait on the profile card.

Run once, locally (needs Pillow and numpy):

    python scripts/ascii_portrait.py path/to/photo.jpg

It writes two text files that scripts/build_card.py reads every day:

    assets/portrait-dark.txt    bright pixels become dense letters (dark backgrounds)
    assets/portrait-light.txt   dark pixels become dense letters (light backgrounds)

The photo itself is never committed, only the letters it becomes.

The ramp below runs from empty to dense by measured ink coverage (Menlo,
40 px). The mid-tones, which is where skin lands, are the letters of my
name: l i s u n o w, already in density order. So the face is spelled
with "wilsonwu".
"""
from __future__ import annotations

import argparse
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps

RAMP = " .:;c" + "lisunow" + "XdKDRWMN"

# Layout contract shared with build_card.py: each glyph cell is CELL_W wide
# and CELL_H tall, so the portrait keeps the photo's proportions.
CELL_W, CELL_H = 7.2, 14.0


def background_mask(rgb: Image.Image, sat_max: float, v_lo: float, v_hi: float) -> np.ndarray:
    """Studio-grey backdrop = low-saturation, mid-value pixels connected to the border."""
    hsv = np.asarray(rgb.convert("HSV"), dtype=np.float32) / 255.0
    s, v = hsv[..., 1], hsv[..., 2]
    candidate = (s < sat_max) & (v > v_lo) & (v < v_hi)
    h, w = candidate.shape
    bg = np.zeros((h, w), dtype=bool)
    queue: deque[tuple[int, int]] = deque()
    border = [(y, x) for x in range(w) for y in (0, h - 1)] + [(y, x) for y in range(h) for x in (0, w - 1)]
    for y, x in border:
        if candidate[y, x] and not bg[y, x]:
            bg[y, x] = True
            queue.append((y, x))
    while queue:
        y, x = queue.popleft()
        for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
            if 0 <= ny < h and 0 <= nx < w and candidate[ny, nx] and not bg[ny, nx]:
                bg[ny, nx] = True
                queue.append((ny, nx))
    return bg


def to_grid(values: np.ndarray, weights: np.ndarray, cols: int, rows: int) -> np.ndarray:
    """Weighted box-average of `values` down to a rows x cols grid."""
    def shrink(a: np.ndarray) -> np.ndarray:
        img = Image.fromarray(a.astype(np.float32), mode="F")
        return np.asarray(img.resize((cols, rows), Image.BOX), dtype=np.float32)
    num, den = shrink(values * weights), shrink(weights)
    return np.where(den > 1e-6, num / np.maximum(den, 1e-6), 0.0), den


def render(lum: np.ndarray, subject: np.ndarray, invert: bool, gamma: float) -> list[str]:
    levels = len(RAMP) - 1
    tone = 1.0 - lum if invert else lum
    tone = np.clip(tone, 0.0, 1.0) ** gamma
    lines = []
    for r in range(lum.shape[0]):
        row = []
        for c in range(lum.shape[1]):
            if subject[r, c] < 0.5:
                row.append(" ")
            else:  # subject cells never go blank, so black hair still shows on a dark card
                row.append(RAMP[1 + int(round(tone[r, c] * (levels - 1)))])
        lines.append("".join(row))
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("photo", type=Path)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "assets")
    ap.add_argument("--cols", type=int, default=80)
    ap.add_argument("--crop", type=float, nargs=4, default=(0.08, 0.03, 0.92, 0.64),
                    metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"), help="crop box as fractions of the photo")
    ap.add_argument("--edge", type=float, default=0.45, help="how much outline to mix in")
    ap.add_argument("--clarity", type=float, default=1.8, help="local-contrast boost")
    ap.add_argument("--clarity-radius", type=float, default=5.0)
    ap.add_argument("--equalize", type=float, default=0.35, help="0 = linear tones, 1 = fully equalized")
    ap.add_argument("--sat-max", type=float, default=0.14)
    ap.add_argument("--v-range", type=float, nargs=2, default=(0.22, 0.80))
    ap.add_argument("--gamma-dark", type=float, default=1.2)
    ap.add_argument("--gamma-light", type=float, default=1.1)
    a = ap.parse_args()

    photo = ImageOps.exif_transpose(Image.open(a.photo)).convert("RGB")
    W, H = photo.size
    l, t, r, b = a.crop
    photo = photo.crop((int(l * W), int(t * H), int(r * W), int(b * H)))

    work = photo.resize((300, round(300 * photo.height / photo.width)), Image.LANCZOS)
    bg = background_mask(work, a.sat_max, *a.v_range)
    subject = (~bg).astype(np.float32)

    gray = work.convert("L")
    lum = np.asarray(gray, dtype=np.float32) / 255.0
    # Local contrast ("clarity"): skin sits in a narrow band between black hair and a
    # white shirt, so lift detail relative to a blurred copy before anything else.
    blur = np.asarray(gray.filter(ImageFilter.GaussianBlur(a.clarity_radius)), dtype=np.float32) / 255.0
    lum = lum + a.clarity * (lum - blur)
    edges = np.asarray(gray.filter(ImageFilter.FIND_EDGES).filter(ImageFilter.GaussianBlur(1.0)),
                       dtype=np.float32) / 255.0
    lo, hi = np.percentile(lum[subject > 0], [2, 98])
    lum = np.clip((lum - lo) / max(hi - lo, 1e-6), 0, 1)
    edges = np.clip(edges / max(np.percentile(edges[subject > 0], 98), 1e-6), 0, 1)

    rows = round(a.cols * CELL_W / (CELL_H * work.width / work.height))
    grid_lum, cover = to_grid(lum, subject, a.cols, rows)
    grid_edge, _ = to_grid(edges, subject, a.cols, rows)
    # Blend in rank equalization over subject cells so every glyph in the ramp gets used.
    inside = cover >= 0.5
    ranks = np.zeros_like(grid_lum)
    order = grid_lum[inside].argsort().argsort()
    ranks[inside] = order / max(order.size - 1, 1)
    grid_lum = (1 - a.equalize) * grid_lum + a.equalize * ranks

    dark = render(np.clip(grid_lum + a.edge * grid_edge, 0, 1), cover, invert=False, gamma=a.gamma_dark)
    light = render(np.clip(grid_lum - a.edge * grid_edge, 0, 1), cover, invert=True, gamma=a.gamma_light)

    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "portrait-dark.txt").write_text("\n".join(dark) + "\n")
    (a.out / "portrait-light.txt").write_text("\n".join(light) + "\n")
    print(f"wrote {a.cols}x{rows} portraits to {a.out}")


if __name__ == "__main__":
    main()

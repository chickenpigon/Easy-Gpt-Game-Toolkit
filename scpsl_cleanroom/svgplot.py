"""Dependency-free SVG scatter plot with fitted curves, for the spec documents."""

from __future__ import annotations

import html
import math

import numpy as np

W, H = 560, 340
L, R, T, B = 60, 20, 20, 50
COLORS = ["#2b6cb0", "#c05621", "#2f855a"]


def _ticks(lo, hi, n=5):
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=raw)
    start = math.ceil(lo / step) * step
    return [round(start + i * step, 10) for i in range(int((hi - start) / step) + 1)]


def scatter(points, fits=(), xlabel="x", ylabel="y", title="") -> str:
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    x0, x1 = float(xs.min()), float(xs.max())
    y0, y1 = float(min(ys.min(), 0)), float(ys.max())
    if x1 == x0:
        x1 = x0 + 1
    if y1 == y0:
        y1 = y0 + 1
    y1 += (y1 - y0) * 0.05
    sx = lambda x: L + (x - x0) / (x1 - x0) * (W - L - R)
    sy = lambda y: H - B - (y - y0) / (y1 - y0) * (H - T - B)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="sans-serif" font-size="11">',
           f'<rect width="{W}" height="{H}" fill="white"/>']
    for tx in _ticks(x0, x1):
        out.append(f'<line x1="{sx(tx):.1f}" y1="{T}" x2="{sx(tx):.1f}" y2="{H - B}" stroke="#eee"/>'
                   f'<text x="{sx(tx):.1f}" y="{H - B + 14}" text-anchor="middle">{tx:g}</text>')
    for ty in _ticks(y0, y1):
        out.append(f'<line x1="{L}" y1="{sy(ty):.1f}" x2="{W - R}" y2="{sy(ty):.1f}" stroke="#eee"/>'
                   f'<text x="{L - 6}" y="{sy(ty) + 4:.1f}" text-anchor="end">{ty:g}</text>')
    out.append(f'<line x1="{L}" y1="{H - B}" x2="{W - R}" y2="{H - B}" stroke="#333"/>'
               f'<line x1="{L}" y1="{T}" x2="{L}" y2="{H - B}" stroke="#333"/>')
    for x, y in zip(xs, ys):
        out.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="2.5" fill="#555" fill-opacity="0.6"/>')
    grid = np.linspace(x0, x1, 200)
    for i, f in enumerate(fits[:3]):
        with np.errstate(all="ignore"):
            yy = f.predict(grid)
        pts = " ".join(f"{sx(x):.1f},{sy(min(max(y, y0), y1)):.1f}" for x, y in zip(grid, yy) if np.isfinite(y))
        dash = "" if i == 0 else ' stroke-dasharray="5,4"'
        out.append(f'<polyline points="{pts}" fill="none" stroke="{COLORS[i]}" stroke-width="2"{dash}/>')
        out.append(f'<text x="{L + 8}" y="{T + 14 + i * 14}" fill="{COLORS[i]}">{html.escape(f.equation())}</text>')
    out.append(f'<text x="{(L + W - R) / 2}" y="{H - 12}" text-anchor="middle">{html.escape(xlabel)}</text>')
    out.append(f'<text x="14" y="{(T + H - B) / 2}" text-anchor="middle" '
               f'transform="rotate(-90 14 {(T + H - B) / 2})">{html.escape(ylabel)}</text>')
    if title:
        out.append(f'<text x="{W - R}" y="{T + 10}" text-anchor="end" fill="#666">{html.escape(title)}</text>')
    out.append("</svg>")
    return "\n".join(out)

"""Measurement values with provenance and spread, plus small stats helpers."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

ALLOWED_PROVENANCE = ("observed", "asset", "design")


@dataclass
class Measurement:
    value: float
    unit: str
    provenance: str  # "observed:<source>", "asset:<source>" or "design:<note>"
    n: int = 1
    p10: float | None = None
    p90: float | None = None
    note: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, "", {})}

    @classmethod
    def from_dict(cls, d: dict) -> "Measurement":
        return cls(**d)

    @property
    def kind(self) -> str:
        return self.provenance.split(":", 1)[0]


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile, q in [0, 100]."""
    if not values:
        raise ValueError("percentile of empty list")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def summarize(values: list[float], unit: str, provenance: str, note: str = "") -> Measurement:
    return Measurement(
        value=round(percentile(values, 50), 4),
        unit=unit,
        provenance=provenance,
        n=len(values),
        p10=round(percentile(values, 10), 4),
        p90=round(percentile(values, 90), 4),
        note=note,
    )


def linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Least squares y = a*x + b. Returns (slope, intercept, r^2)."""
    n = len(xs)
    if n < 2:
        raise ValueError("need at least 2 points")
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        raise ValueError("x values are constant")
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 if ss_tot == 0 else 1 - ss_res / ss_tot
    return slope, intercept, r2


def mode_bin(values: list[float], resolution: float) -> float:
    """Most common value after rounding to ``resolution`` (for discrete damage numbers etc.)."""
    counts: dict[float, int] = {}
    for v in values:
        key = round(round(v / resolution) * resolution, 6)
        counts[key] = counts.get(key, 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]

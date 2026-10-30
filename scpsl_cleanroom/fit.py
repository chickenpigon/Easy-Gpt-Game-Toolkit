"""Formula discovery: fit candidate models to observed (x, y) pairs and rank them.

Each model is fitted with Levenberg-Marquardt from several starting points and
ranked by AICc, so a simpler model wins unless a richer one explains the data
clearly better. Fitted parameters are also tried as "nice" round numbers,
because designers usually pick values like 1.5 or 0.25.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np


@dataclass
class Model:
    name: str
    formula: str  # human readable, parameters named a, b, c, d
    params: tuple[str, ...]
    fn: Callable[..., np.ndarray]
    guess: Callable[[np.ndarray, np.ndarray], list[list[float]]]
    domain: str = "any"  # "positive_x" when x must be > 0


def _span(x):
    s = float(np.ptp(x))
    return s if s > 0 else 1.0


def _lin_guess(x, y):
    if len(x) >= 2 and np.ptp(x) > 0:
        b, a = np.polyfit(x, y, 1)
        return float(a), float(b)
    return float(np.mean(y)), 0.0


def _exp_guess(x, y):
    yp = np.clip(y, 1e-9, None)
    if np.all(y > 0) and np.ptp(x) > 0:
        k, ln_a = np.polyfit(x, np.log(yp), 1)
        return float(math.exp(ln_a)), float(k)
    return float(np.mean(y)), 0.0


def _geo_guess(x, y):
    a, k = _exp_guess(x, y)
    return [a, math.exp(k)]


def _guess_saturating(x, y):
    hi = float(np.max(y)) * 1.05 + 1e-9
    return [[hi, float(y[np.argmin(x)]), 1.0 / _span(x)], [hi, float(np.min(y)), 3.0 / _span(x)]]


def _guess_falloff(x, y):
    a = float(np.max(y))
    r = float(np.max(x))
    return [[a, r], [a, r * 1.5], [a, float(np.median(x)) * 2 + 1e-6]]


def _guess_inner_outer(x, y):
    a = float(np.max(y))
    lo, hi = float(np.min(x)), float(np.max(x))
    return [[a, lo + 0.1 * (hi - lo), hi], [a, lo + 0.3 * (hi - lo), hi * 1.2], [a, lo, hi]]


MODELS: dict[str, Model] = {m.name: m for m in [
    Model("constant", "y = a", ("a",),
          lambda x, a: np.full_like(x, a),
          lambda x, y: [[float(np.mean(y))]]),
    Model("linear", "y = a + b*x", ("a", "b"),
          lambda x, a, b: a + b * x,
          lambda x, y: [list(_lin_guess(x, y))]),
    Model("quadratic", "y = a + b*x + c*x^2", ("a", "b", "c"),
          lambda x, a, b, c: a + b * x + c * x * x,
          lambda x, y: [[*_lin_guess(x, y), 0.0]]),
    Model("geometric", "y = a * b^x", ("a", "b"),
          lambda x, a, b: a * np.power(np.abs(b), x),
          lambda x, y: [_geo_guess(x, y)]),
    Model("geometric_capped", "y = min(a * b^x, c)", ("a", "b", "c"),
          lambda x, a, b, c: np.minimum(a * np.power(np.abs(b), x), c),
          lambda x, y: [[*_geo_guess(x, y), float(np.max(y))]]),
    Model("linear_capped", "y = min(a + b*x, c)", ("a", "b", "c"),
          lambda x, a, b, c: np.minimum(a + b * x, c),
          lambda x, y: [[*_lin_guess(x, y), float(np.max(y))]]),
    Model("saturating", "y = a - (a - b) * e^(-c*x)", ("a", "b", "c"),
          lambda x, a, b, c: a - (a - b) * np.exp(-c * x),
          _guess_saturating),
    Model("power", "y = a * x^b", ("a", "b"),
          lambda x, a, b: a * np.power(x, b),
          lambda x, y: [[float(np.mean(y)), 1.0], [float(np.mean(y)), 0.5]], "positive_x"),
    Model("logarithmic", "y = a + b*ln(x)", ("a", "b"),
          lambda x, a, b: a + b * np.log(x),
          lambda x, y: [[float(np.mean(y)), 1.0]], "positive_x"),
    # --- falloff with distance -------------------------------------------------
    Model("linear_falloff", "y = a * max(0, 1 - x/b)", ("a", "b"),
          lambda x, a, b: a * np.clip(1 - x / b, 0, None),
          _guess_falloff),
    Model("inner_outer_falloff", "y = a for x<=b, linear to 0 at x=c", ("a", "b", "c"),
          lambda x, a, b, c: a * np.clip((c - x) / np.where(np.abs(c - b) < 1e-9, 1e-9, c - b), 0, 1),
          _guess_inner_outer),
    Model("inverse_linear", "y = a / (1 + b*x)", ("a", "b"),
          lambda x, a, b: a / (1 + b * x),
          lambda x, y: [[float(np.max(y)), 1.0 / _span(x)]]),
    Model("inverse_square", "y = a / (1 + b*x^2)", ("a", "b"),
          lambda x, a, b: a / (1 + b * x * x),
          lambda x, y: [[float(np.max(y)), 1.0 / _span(x) ** 2]]),
    Model("exp_decay", "y = a * e^(-b*x)", ("a", "b"),
          lambda x, a, b: a * np.exp(-b * x),
          lambda x, y: [[float(np.max(y)), 1.0 / _span(x)]]),
    Model("quadratic_falloff", "y = a * max(0, 1 - (x/b)^2)", ("a", "b"),
          lambda x, a, b: a * np.clip(1 - (x / b) ** 2, 0, None),
          _guess_falloff),
]}

GROWTH = ["constant", "linear", "quadratic", "geometric", "geometric_capped", "linear_capped",
          "saturating", "power", "logarithmic"]
FALLOFF = ["constant", "linear_falloff", "inner_outer_falloff", "inverse_linear", "inverse_square",
           "exp_decay", "quadratic_falloff", "linear"]
DEFAULT = sorted(set(GROWTH + FALLOFF), key=list(MODELS).index)


@dataclass
class Fit:
    model: str
    formula: str
    params: dict[str, float]
    rmse: float
    r2: float
    aicc: float
    n: int
    nice_params: dict[str, float] = field(default_factory=dict)
    nice_rmse: float | None = None

    def predict(self, x):
        p = self.nice_params or self.params
        return MODELS[self.model].fn(np.asarray(x, dtype=float), *p.values())

    def equation(self, nice=True) -> str:
        return _substitute(self.formula, self.nice_params if nice and self.nice_params else self.params)

    def to_dict(self) -> dict:
        d = {"model": self.model, "formula": self.formula, "params": _round(self.params),
             "rmse": round(self.rmse, 6), "r2": round(self.r2, 6), "aicc": round(self.aicc, 3), "n": self.n,
             "equation": self.equation()}
        if self.nice_params:
            d["nice_params"] = self.nice_params
            d["nice_rmse"] = round(self.nice_rmse, 6)
        return d


def _fmt(v: float) -> str:
    return f"{v:.6g}"


def _round(p: dict) -> dict:
    return {k: float(f"{v:.6g}") for k, v in p.items()}


def _substitute(formula: str, params: dict[str, float]) -> str:
    out = []
    i = 0
    while i < len(formula):
        ch = formula[i]
        prev_ok = i == 0 or not formula[i - 1].isalnum()
        next_ok = i + 1 == len(formula) or not formula[i + 1].isalnum()
        if ch in params and prev_ok and next_ok:
            out.append(_fmt(params[ch]))
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _lm(fn, x, y, p0, iters=200):
    """Small Levenberg-Marquardt with numeric Jacobian."""
    with np.errstate(all="ignore"):
        return _lm_inner(fn, x, y, p0, iters)


def _lm_inner(fn, x, y, p0, iters):
    p = np.asarray(p0, dtype=float)
    lam = 1e-3

    def resid(params):
        with np.errstate(all="ignore"):
            r = fn(x, *params) - y
        return np.where(np.isfinite(r), r, 1e6)

    r = resid(p)
    cost = float(r @ r)
    for _ in range(iters):
        J = np.empty((len(x), len(p)))
        for j in range(len(p)):
            h = 1e-6 * max(1.0, abs(p[j]))
            dp = p.copy()
            dp[j] += h
            J[:, j] = (resid(dp) - r) / h
        A = J.T @ J
        g = J.T @ r
        improved = False
        for _ in range(10):
            try:
                step = np.linalg.solve(A + lam * np.diag(np.diag(A) + 1e-12), -g)
            except np.linalg.LinAlgError:
                lam *= 10
                continue
            cand = p + step
            rc = resid(cand)
            cc = float(rc @ rc)
            if cc < cost:
                p, r, cost = cand, rc, cc
                lam = max(lam / 10, 1e-12)
                improved = True
                break
            lam *= 10
        if not improved or float(np.max(np.abs(step))) < 1e-10 * (1 + float(np.max(np.abs(p)))):
            break
    return p, cost


def _aicc(sse, n, k):
    sse = max(sse, 1e-12 * n)
    aic = n * math.log(sse / n) + 2 * k
    return aic + (2 * k * (k + 1)) / (n - k - 1) if n - k - 1 > 0 else float("inf")


NICE_STEPS = (1, 0.5, 0.25, 0.1, 0.05, 0.01)


def _nice(fn, x, y, params: dict[str, float], sse: float) -> tuple[dict, float] | None:
    """Snap parameters to round numbers one at a time, refitting the rest each time.

    A snap is kept when the extra squared error stays within what measurement noise
    explains (about 2 sigma per snapped parameter, sigma estimated from the free fit).
    """
    names = list(params)
    vals = np.array(list(params.values()))
    fixed: dict[int, float] = {}
    dof = max(len(x) - len(vals), 1)
    sigma2 = sse / dof + 1e-12

    def refit(fix):
        free = [j for j in range(len(vals)) if j not in fix]
        if not free:
            full = np.array([fix[j] for j in range(len(vals))])
            r = fn(x, *full) - y
            return full, float(r @ r) if np.all(np.isfinite(r)) else math.inf

        def sub(xx, *fp):
            full = np.empty(len(vals))
            for j, v in fix.items():
                full[j] = v
            full[free] = fp
            return fn(xx, *full)

        p, cost = _lm(sub, x, y, vals[free])
        full = np.empty(len(vals))
        for j, v in fix.items():
            full[j] = v
        full[free] = p
        return full, cost

    for i in np.argsort(-np.abs(vals)):
        v = vals[i]
        mag = 10 ** math.floor(math.log10(abs(v))) if v else 1
        for step in NICE_STEPS:
            cand = round(round(v / (step * mag)) * step * mag, 10)
            trial, cost = refit({**fixed, int(i): cand})
            if cost <= sse + 4.0 * sigma2 * (len(fixed) + 1):
                fixed[int(i)] = cand
                vals = trial
                break
    if not fixed:
        return None
    r = fn(x, *vals) - y
    return dict(zip(names, (float(f"{v:.6g}") for v in vals))), math.sqrt(float(r @ r) / len(x))


def fit_model(name: str, x, y) -> Fit | None:
    m = MODELS[name]
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if m.domain == "positive_x" and np.any(x <= 0):
        return None
    k = len(m.params)
    if len(x) <= k:
        return None
    best = None
    for p0 in m.guess(x, y):
        try:
            p, sse = _lm(m.fn, x, y, p0)
        except (FloatingPointError, ValueError, OverflowError):
            continue
        if np.all(np.isfinite(p)) and (best is None or sse < best[1]):
            best = (p, sse)
    if best is None:
        return None
    p, sse = best
    n = len(x)
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    params = dict(zip(m.params, map(float, p)))
    f = Fit(model=name, formula=m.formula, params=params, rmse=math.sqrt(sse / n),
            r2=1.0 if ss_tot == 0 else 1 - sse / ss_tot, aicc=_aicc(sse, n, k), n=n)
    nice = _nice(m.fn, x, y, params, sse)
    if nice:
        f.nice_params, f.nice_rmse = nice
    return f


def discover(x, y, models: list[str] | None = None, top=3) -> list[Fit]:
    """Fit all candidate models and return the best ``top`` by AICc."""
    fits = [f for name in (models or DEFAULT) if (f := fit_model(name, x, y)) is not None]
    fits.sort(key=lambda f: f.aicc)
    return fits[:top]

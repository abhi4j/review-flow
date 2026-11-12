"""Uncertainty for period-over-period changes, and what drove a change in wait time.

Every change the service reports carries a 95% interval, so whether a metric moved is
decided by arithmetic rather than by the reader or the model. Resampling uses a fixed
seed: the same data always yields the same intervals, which keeps ETags and narrative
cache keys stable.
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

Interval = tuple[float, float]
Array = NDArray[np.float64]
Codes = NDArray[np.int64]
# Maps a (resamples, n) matrix to one value per resample.
Statistic = Callable[[Array], Array]

RESAMPLES = 1000
MIN_SAMPLE = 8  # below this, a bootstrap interval says more about the method than the data
MIN_TAIL_SAMPLE = 50  # a p90 needs about five observations beyond it to resample reliably
_SEED = 0
_Z95 = 1.959964


def median(x: Array) -> Array:
    return np.percentile(x, 50, axis=-1)


def p90(x: Array) -> Array:
    return np.percentile(x, 90, axis=-1)


def percent(x: Array) -> Array:
    return 100 * x.mean(axis=-1)


def top_share(x: Array) -> Array:
    """Percent of each row held by its most frequent code. Codes are 0..k-1."""
    codes = x.astype(np.int64)
    rows, k = codes.shape[0], int(codes.max()) + 1
    offset = codes + k * np.arange(rows)[:, None]
    counts = np.bincount(offset.ravel(), minlength=rows * k).reshape(rows, k)
    shares: Array = 100 * counts.max(axis=1) / codes.shape[1]
    return shares


def excludes_zero(interval: Interval) -> bool:
    return interval[0] > 0 or interval[1] < 0


def bootstrap_delta(
    current: Sequence[float], previous: Sequence[float], statistic: Statistic,
    *, min_sample: int = MIN_SAMPLE,
) -> Interval | None:
    """Percentile-bootstrap interval for statistic(current) - statistic(previous)."""
    if min(len(current), len(previous)) < min_sample:
        return None
    rng = np.random.default_rng(_SEED)
    deltas = statistic(_resample(rng, current)) - statistic(_resample(rng, previous))
    return _interval(deltas)


def poisson_delta(current: int, previous: int) -> Interval:
    """Interval for the difference of two event counts over periods of equal length
    (normal approximation to the difference of Poisson counts)."""
    half = _Z95 * math.sqrt(current + previous)
    return current - previous - half, current - previous + half


@dataclass(frozen=True, slots=True)
class Split:
    """median(current) - median(previous) = mix + within."""

    mix: float
    within: float
    mix_ci: Interval
    within_ci: Interval


def split_change(
    current: Sequence[float], current_cells: Sequence[int],
    previous: Sequence[float], previous_cells: Sequence[int],
    cells: int,
) -> Split | None:
    """Split a change in median into a shift in mix and a change within cells.

    The counterfactual reweights current observations so every cell (a PR size bucket,
    say) has its previous-period share: the median the current period would have had
    with the old mix. `mix` = actual - counterfactual, `within` = counterfactual -
    previous, and they sum to the change.
    """
    if min(len(current), len(previous)) < MIN_SAMPLE:
        return None
    cur, cur_cells = np.asarray(current, float), np.asarray(current_cells, np.int64)
    prev, prev_cells = np.asarray(previous, float), np.asarray(previous_cells, np.int64)
    mix, within = _split(cur, cur_cells, prev, prev_cells, cells)
    if math.isnan(mix):
        return None  # no cell present in both periods
    rng = np.random.default_rng(_SEED)
    samples = np.array([
        _split(cur[i], cur_cells[i], prev[j], prev_cells[j], cells)
        for i, j in zip(_indices(rng, len(cur)), _indices(rng, len(prev)), strict=True)
    ])
    return Split(mix, within, _interval(samples[:, 0]), _interval(samples[:, 1]))


def _split(cur: Array, cur_cells: Codes, prev: Array, prev_cells: Codes,
           cells: int) -> tuple[float, float]:
    prev_share = np.bincount(prev_cells, minlength=cells) / len(prev_cells)
    cur_count = np.bincount(cur_cells, minlength=cells)
    counterfactual = _weighted_median(cur, prev_share[cur_cells] / cur_count[cur_cells])
    return float(np.median(cur)) - counterfactual, counterfactual - float(np.median(prev))


def _weighted_median(values: Array, weights: Array) -> float:
    """Equals np.median when all weights are equal (same interpolation)."""
    keep = weights > 0
    if not keep.any():
        return math.nan
    order = np.argsort(values[keep], kind="stable")
    v, w = values[keep][order], weights[keep][order]
    below = np.concatenate(([0.0], np.cumsum(w)[:-1]))
    if below[-1] == 0:
        return float(v[0])
    return float(np.interp(0.5, below / below[-1], v))


def _indices(rng: np.random.Generator, n: int) -> NDArray[np.int64]:
    return rng.integers(0, n, size=(RESAMPLES, n))


def _resample(rng: np.random.Generator, values: Sequence[float]) -> Array:
    data = np.asarray(values, dtype=float)
    return data[_indices(rng, len(data))]


def _interval(samples: Array) -> Interval:
    low, high = np.nanpercentile(samples, [2.5, 97.5])
    return float(low), float(high)

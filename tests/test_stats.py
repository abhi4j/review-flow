import numpy as np
import pytest

from reviewflow import stats


def test_top_share_per_row():
    rows = np.array([[0, 0, 1, 2], [1, 1, 1, 1]], dtype=float)
    assert stats.top_share(rows).tolist() == [50.0, 100.0]


def test_poisson_interval_is_centred_on_the_difference():
    low, high = stats.poisson_delta(30, 10)
    assert (low + high) / 2 == pytest.approx(20)
    assert stats.excludes_zero((low, high))
    assert not stats.excludes_zero(stats.poisson_delta(5, 2))


def test_bootstrap_is_deterministic_and_needs_a_minimum_sample():
    fast, slow = [1.0, 2, 2, 3, 3, 4, 5, 6, 7, 8], [10.0, 12, 14, 15, 16, 18, 20, 22, 25, 30]
    interval = stats.bootstrap_delta(slow, fast, stats.median)
    assert interval == stats.bootstrap_delta(slow, fast, stats.median)
    assert interval is not None and stats.excludes_zero(interval)
    assert stats.bootstrap_delta(slow[:7], fast, stats.median) is None
    assert stats.bootstrap_delta(slow, fast, stats.p90, min_sample=50) is None


def test_weighted_median_matches_numpy_with_equal_weights():
    values = np.array([5.0, 1, 9, 3, 7, 2])
    assert stats._weighted_median(values, np.ones(6)) == pytest.approx(np.median(values))
    # Doubling the weight of the two largest values moves the median up.
    assert stats._weighted_median(values, np.array([1.0, 1, 2, 1, 2, 1])) > np.median(values)


def _cells(slow_share: float, n: int = 200) -> tuple[list[float], list[int]]:
    """Cell 1 waits ten times longer than cell 0; `slow_share` of PRs are in cell 1."""
    slow = round(n * slow_share)
    values = [1.0 + i % 5 for i in range(n - slow)] + [10.0 + i % 5 for i in range(slow)]
    return values, [0] * (n - slow) + [1] * slow


def test_split_attributes_a_pure_mix_shift_to_mix():
    before, after = _cells(0.2), _cells(0.6)
    split = stats.split_change(*after, *before, cells=2)
    assert split is not None
    change = float(np.median(after[0]) - np.median(before[0]))
    assert split.mix + split.within == pytest.approx(change)
    assert split.mix == pytest.approx(change)
    assert stats.excludes_zero(split.mix_ci) and not stats.excludes_zero(split.within_ci)


def test_split_attributes_slower_cells_to_within():
    values, cells = _cells(0.3)
    slower = [v * 3 for v in values]
    split = stats.split_change(slower, cells, values, cells, cells=2)
    assert split is not None
    assert split.mix == pytest.approx(0)
    assert stats.excludes_zero(split.within_ci) and split.within > 0

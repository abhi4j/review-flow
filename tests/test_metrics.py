"""Every expected number here is derived by hand from tests/scenario.py.

Current-period flow samples (human-authored PRs merged Oct 11-21):
  PR  size  ttfr  review->merge  cycle  author
  1     50     2              3      5  maintainer
  2    600    10             14     24  contributor   (draft: measured from ready)
  3    120     -              -      1  maintainer    (no review)
  5     30     6             24     30  maintainer    (bot comment ignored)
  6    200    12             24     36  maintainer    (first review in previous period)
"""

import pytest

from reviewflow.metrics import compute_review_flow, percentile
from tests.factories import at, pr, review
from tests.scenario import WINDOW, scenario_prs


@pytest.fixture
def insight():
    return compute_review_flow("acme/widgets", scenario_prs(), WINDOW,
                               data_as_of=at(20), top_n=10)


def test_percentile_interpolates_linearly():
    assert percentile([], 0.5) is None
    assert percentile([7.0], 0.9) == 7.0
    assert percentile([1.0, 5.0, 24.0, 30.0, 36.0], 0.5) == 24.0
    assert percentile([2.0, 6.0, 10.0, 12.0], 0.9) == pytest.approx(11.4)


def test_flow_summary_current_period(insight):
    s = insight.summary
    assert s.merged_prs.current == 5
    assert s.bot_prs_excluded.current == 1
    assert s.merged_without_review_pct.current == 20.0
    # ttfr sorted [2, 6, 10, 12]
    assert s.ttfr_p50_hours.current == 8.0
    assert s.ttfr_p90_hours.current == 11.4
    # review->merge sorted [3, 14, 24, 24]
    assert s.review_to_merge_p50_hours.current == 19.0
    # cycle sorted [1, 5, 24, 30, 36]
    assert s.cycle_p50_hours.current == 24.0
    assert s.cycle_p90_hours.current == 33.6
    # size sorted [30, 50, 120, 200, 600]
    assert s.pr_size_p50_lines.current == 120.0


def test_reviewer_load_current_period(insight):
    s = insight.summary
    # bob: PRs 1, 2, 5; dave: PR 4 (bot-authored still counts); alice: PR 5.
    # Excluded: carol's self-reply on PR 2, ci-bot on PR 5, bob's PR 6 review (previous).
    assert s.active_reviewers.current == 3
    assert s.pr_reviews.current == 5
    assert s.top_reviewer_share_pct.current == 60.0
    assert s.review_bus_factor.current == 1


def test_previous_period_and_changes(insight):
    s = insight.summary
    assert s.merged_prs.previous == 2
    assert s.ttfr_p50_hours.previous == 2.0
    assert s.ttfr_p50_hours.delta == 6.0
    assert s.ttfr_p50_hours.delta_pct == 300.0
    assert s.merged_prs.delta_pct == 150.0
    # Previous reviewers: dave PR 10, alice PR 11, bob PR 6 -> one PR each.
    assert s.pr_reviews.previous == 3
    assert s.top_reviewer_share_pct.previous == 33.3
    assert s.review_bus_factor.previous == 2
    assert s.review_bus_factor.delta == -1
    assert s.merged_without_review_pct.previous == 0.0
    assert s.merged_without_review_pct.delta_pct is None  # relative change from zero


def test_reviewer_rows_include_both_periods(insight):
    rows = {r.login: r for r in insight.reviewers}
    assert [r.login for r in insight.reviewers] == ["bob", "alice", "dave"]
    bob = rows["bob"]
    assert (bob.prs_reviewed.current, bob.prs_reviewed.previous, bob.reviews) == (3, 1, 4)
    assert (bob.approvals, bob.changes_requested, bob.inline_comments) == (3, 0, 3)
    assert (bob.share_pct.current, bob.share_pct.previous) == (60.0, 33.3)
    alice = rows["alice"]
    assert (alice.changes_requested, alice.inline_comments, alice.approvals) == (1, 2, 0)
    assert "carol" not in rows and "ci-bot" not in rows


def test_changes_carry_intervals_only_where_the_sample_supports_one(insight):
    s = insight.summary
    # Counts: Poisson interval for 5 vs 2 merged PRs, 3 +/- 1.96 * sqrt(7).
    assert s.merged_prs.delta_ci == (-2.19, 8.19)
    assert s.merged_prs.significant is False
    # Four reviewed PRs is below the bootstrap minimum: no interval, no verdict.
    assert s.ttfr_p50_hours.delta_ci is None and s.ttfr_p50_hours.significant is None
    assert s.review_bus_factor.delta_ci is None
    assert insight.drivers.by_size is None and insight.drivers.by_author is None


def test_segments(insight):
    size = {r.key: r for r in insight.segments.by_size}
    assert (size["small"].merged_prs, size["small"].ttfr_p50_hours) == (2, 4.0)
    assert size["small"].ttfr_p50_vs_overall == 0.5
    assert (size["medium"].merged_prs, size["medium"].ttfr_p50_hours) == (2, 12.0)
    assert size["medium"].cycle_p50_hours == 18.5
    assert (size["large"].merged_prs, size["large"].ttfr_p50_vs_overall) == (1, 1.25)
    author = {r.key: r for r in insight.segments.by_author}
    assert (author["maintainer"].merged_prs, author["maintainer"].ttfr_p50_hours) == (4, 6.0)
    assert author["contributor"].ttfr_p50_vs_overall == 1.25


def test_examples_point_at_real_prs(insight):
    assert [e.number for e in insight.examples.slowest_first_review] == [6, 2, 5, 1]
    assert [e.number for e in insight.examples.merged_without_review] == [3]
    assert insight.examples.slowest_first_review[0].url.endswith("/pull/6")


def test_empty_period_yields_nulls_not_errors():
    result = compute_review_flow("acme/widgets", [], WINDOW, data_as_of=at(20), top_n=5)
    s = result.summary
    assert s.merged_prs.current == 0
    assert s.ttfr_p50_hours.current is None
    assert s.merged_without_review_pct.current is None
    assert s.review_bus_factor.current is None
    assert s.merged_prs.delta_pct is None
    assert result.reviewers == []


def test_self_review_exclusion_is_case_insensitive():
    p = pr(1, author="Alice", ready=at(10), merged=at(11),
           reviews=(review("alice", at(10, 1)),))
    result = compute_review_flow("acme/widgets", [p], WINDOW, data_as_of=at(20), top_n=5)
    assert result.summary.merged_without_review_pct.current == 100.0


def test_review_on_draft_counts_as_immediate():
    p = pr(1, created=at(10), ready=at(12), merged=at(13),
           reviews=(review("bob", at(11)),))
    result = compute_review_flow("acme/widgets", [p], WINDOW, data_as_of=at(20), top_n=5)
    assert result.summary.ttfr_p50_hours.current == 0.0

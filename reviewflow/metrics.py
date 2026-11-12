"""Review-flow metrics. Pure functions over domain records, no I/O.

Two populations, deliberately different:
  * Flow metrics (wait times, unreviewed merges, segments) use human-authored PRs *merged*
    in the period. Merged PRs have a complete lifecycle, so there is no right-censoring:
    a still-open PR that has waited a week would otherwise be silently dropped from the
    median, making slow periods look fast.
  * Reviewer load uses every review *submitted* in the period, on any PR, because review
    work on a long-lived or bot-authored PR is still review work.
"""

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from reviewflow import stats
from reviewflow.models import PullRequest, ReviewState
from reviewflow.period import Window
from reviewflow.schemas import (
    Comparison,
    Decomposition,
    Drivers,
    Effect,
    Examples,
    Interval,
    Number,
    Period,
    PullRequestExample,
    ReviewerRow,
    ReviewFlowInsight,
    SegmentRow,
    Segments,
    Summary,
)

EXAMPLES_PER_LIST = 5
_SIZE_BUCKETS: tuple[tuple[str, str, int, float], ...] = (
    ("small", "Under 100 lines", 0, 100),
    ("medium", "100 to 499 lines", 100, 500),
    ("large", "500 lines or more", 500, math.inf),
)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (NumPy's default method)."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _hours(delta: timedelta) -> float:
    return max(delta.total_seconds(), 0.0) / 3600


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


@dataclass(frozen=True, slots=True)
class FlowSample:
    pr: PullRequest
    ttfr_hours: float | None  # None when merged without any review
    review_to_merge_hours: float | None
    cycle_hours: float


def flow_sample(pr: PullRequest) -> FlowSample:
    assert pr.merged_at is not None
    merged_at = pr.merged_at
    pre_merge = [r.submitted_at for r in pr.human_reviews() if r.submitted_at <= merged_at]
    cycle = _hours(merged_at - pr.ready_at)
    if not pre_merge:
        return FlowSample(pr, None, None, cycle)
    first = min(pre_merge)
    # A review on a draft (before ready_at) counts as an immediate first review.
    return FlowSample(pr, _hours(first - pr.ready_at), _hours(merged_at - first), cycle)


def _size_bucket(sample: FlowSample) -> int:
    return next(i for i, (_, _, low, high) in enumerate(_SIZE_BUCKETS)
                if low <= sample.pr.size_lines < high)


def _author_type(sample: FlowSample) -> int:
    return 0 if sample.pr.is_maintainer_authored else 1


@dataclass(slots=True)
class ReviewerTally:
    prs: set[int] = field(default_factory=set)
    reviews: int = 0
    approvals: int = 0
    changes_requested: int = 0
    inline_comments: int = 0


@dataclass(frozen=True, slots=True)
class PeriodStats:
    samples: list[FlowSample]
    bot_prs: int
    reviewers: dict[str, ReviewerTally]

    @property
    def pr_reviews(self) -> int:
        return sum(len(t.prs) for t in self.reviewers.values())

    def reviewed(self) -> list[FlowSample]:
        return [s for s in self.samples if s.ttfr_hours is not None]

    def ttfr(self) -> list[float]:
        return [s.ttfr_hours for s in self.samples if s.ttfr_hours is not None]

    def review_to_merge(self) -> list[float]:
        return [s.review_to_merge_hours for s in self.samples
                if s.review_to_merge_hours is not None]

    def cycle(self) -> list[float]:
        return [s.cycle_hours for s in self.samples]

    def sizes(self) -> list[float]:
        return [float(s.pr.size_lines) for s in self.samples]

    def unreviewed(self) -> list[float]:
        return [float(s.ttfr_hours is None) for s in self.samples]

    def review_codes(self) -> list[float]:
        """One code per PR review, identifying its reviewer; for resampling load shares."""
        return [float(code) for code, tally in enumerate(self.reviewers.values())
                for _ in tally.prs]

    def share_pct(self, login: str) -> float | None:
        total = self.pr_reviews
        tally = self.reviewers.get(login)
        if not total:
            return None
        return round(100 * len(tally.prs) / total, 1) if tally else 0.0

    def ranked_reviewers(self) -> list[tuple[str, ReviewerTally]]:
        return sorted(self.reviewers.items(),
                      key=lambda kv: (-len(kv[1].prs), -kv[1].approvals, kv[0]))

    def bus_factor(self) -> int | None:
        total = self.pr_reviews
        if not total:
            return None
        covered = 0
        for k, (_, tally) in enumerate(self.ranked_reviewers(), start=1):
            covered += len(tally.prs)
            if 2 * covered >= total:
                return k
        raise AssertionError("unreachable")  # pragma: no cover

    def summary_values(self) -> dict[str, Number | None]:
        merged = len(self.samples)
        ranked = self.ranked_reviewers()
        return {
            "merged_prs": merged,
            "bot_prs_excluded": self.bot_prs,
            "merged_without_review_pct": (
                round(100 * sum(self.unreviewed()) / merged, 1) if merged else None
            ),
            "ttfr_p50_hours": _round(percentile(self.ttfr(), 0.5)),
            "ttfr_p90_hours": _round(percentile(self.ttfr(), 0.9)),
            "review_to_merge_p50_hours": _round(percentile(self.review_to_merge(), 0.5)),
            "cycle_p50_hours": _round(percentile(self.cycle(), 0.5)),
            "cycle_p90_hours": _round(percentile(self.cycle(), 0.9)),
            "pr_size_p50_lines": _round(percentile(self.sizes(), 0.5), 1),
            "active_reviewers": len(self.reviewers),
            "pr_reviews": self.pr_reviews,
            "top_reviewer_share_pct": self.share_pct(ranked[0][0]) if ranked else None,
            "review_bus_factor": self.bus_factor(),
        }


def period_stats(prs: Iterable[PullRequest], window: Window) -> PeriodStats:
    samples: list[FlowSample] = []
    bot_prs = 0
    reviewers: dict[str, ReviewerTally] = {}
    for pr in prs:
        if pr.merged_at is not None and window.contains(pr.merged_at):
            if pr.author_is_bot:
                bot_prs += 1
            else:
                samples.append(flow_sample(pr))
        for review in pr.human_reviews():
            if not window.contains(review.submitted_at):
                continue
            tally = reviewers.setdefault(review.reviewer, ReviewerTally())
            tally.prs.add(pr.number)
            tally.reviews += 1
            tally.approvals += review.state is ReviewState.APPROVED
            tally.changes_requested += review.state is ReviewState.CHANGES_REQUESTED
            tally.inline_comments += review.inline_comments
    return PeriodStats(samples, bot_prs, reviewers)


def _ci(interval: Interval) -> Interval:
    return round(interval[0], 2), round(interval[1], 2)


def compare(current: Number | None, previous: Number | None,
            interval: Interval | None = None) -> Comparison:
    if current is None or previous is None:
        return Comparison(current=current, previous=previous, delta=None, delta_pct=None,
                          delta_ci=None, significant=None)
    delta = current - previous
    delta = round(delta, 2) if isinstance(delta, float) else delta
    delta_pct = round(100 * (current - previous) / previous, 1) if previous else None
    return Comparison(
        current=current, previous=previous, delta=delta, delta_pct=delta_pct,
        delta_ci=_ci(interval) if interval else None,
        significant=stats.excludes_zero(interval) if interval else None,
    )


def _delta_intervals(current: PeriodStats, previous: PeriodStats) -> dict[str, Interval | None]:
    """Intervals for the change in each summary metric. Active reviewers and bus factor
    have none: they are small integers whose resampling distribution is degenerate."""
    def boot(values: Callable[[PeriodStats], list[float]], statistic: stats.Statistic,
             min_sample: int = stats.MIN_SAMPLE) -> Interval | None:
        return stats.bootstrap_delta(values(current), values(previous), statistic,
                                     min_sample=min_sample)

    return {
        "merged_prs": stats.poisson_delta(len(current.samples), len(previous.samples)),
        "bot_prs_excluded": stats.poisson_delta(current.bot_prs, previous.bot_prs),
        "pr_reviews": stats.poisson_delta(current.pr_reviews, previous.pr_reviews),
        "merged_without_review_pct": boot(PeriodStats.unreviewed, stats.percent),
        "ttfr_p50_hours": boot(PeriodStats.ttfr, stats.median),
        "ttfr_p90_hours": boot(PeriodStats.ttfr, stats.p90, stats.MIN_TAIL_SAMPLE),
        "review_to_merge_p50_hours": boot(PeriodStats.review_to_merge, stats.median),
        "cycle_p50_hours": boot(PeriodStats.cycle, stats.median),
        "cycle_p90_hours": boot(PeriodStats.cycle, stats.p90, stats.MIN_TAIL_SAMPLE),
        "pr_size_p50_lines": boot(PeriodStats.sizes, stats.median),
        "top_reviewer_share_pct": boot(PeriodStats.review_codes, stats.top_share),
    }


def _decompose(current: PeriodStats, previous: PeriodStats,
               cell: Callable[[FlowSample], int], cells: int) -> Decomposition | None:
    # ttfr() and reviewed() list the same samples in the same order.
    split = stats.split_change(
        current.ttfr(), [cell(s) for s in current.reviewed()],
        previous.ttfr(), [cell(s) for s in previous.reviewed()],
        cells,
    )
    if split is None:
        return None
    return Decomposition(
        mix=Effect(hours=round(split.mix, 2), ci=_ci(split.mix_ci),
                   significant=stats.excludes_zero(split.mix_ci)),
        within=Effect(hours=round(split.within, 2), ci=_ci(split.within_ci),
                      significant=stats.excludes_zero(split.within_ci)),
    )


def _segment(
    key: str, label: str, samples: list[FlowSample], overall_ttfr: float | None
) -> SegmentRow:
    ttfr = percentile([s.ttfr_hours for s in samples if s.ttfr_hours is not None], 0.5)
    ratio = ttfr / overall_ttfr if ttfr is not None and overall_ttfr else None
    return SegmentRow(
        key=key,
        label=label,
        merged_prs=len(samples),
        ttfr_p50_hours=_round(ttfr),
        cycle_p50_hours=_round(percentile([s.cycle_hours for s in samples], 0.5)),
        ttfr_p50_vs_overall=_round(ratio),
    )


def _segments(period: PeriodStats) -> Segments:
    overall = percentile(period.ttfr(), 0.5)
    by_size = [
        _segment(key, label, [s for s in period.samples if _size_bucket(s) == i], overall)
        for i, (key, label, _, _) in enumerate(_SIZE_BUCKETS)
    ]
    by_author = [
        _segment("maintainer", "Authors with write access",
                 [s for s in period.samples if _author_type(s) == 0], overall),
        _segment("contributor", "Other contributors",
                 [s for s in period.samples if _author_type(s) == 1], overall),
    ]
    return Segments(by_size=by_size, by_author=by_author)


def _example(sample: FlowSample) -> PullRequestExample:
    return PullRequestExample(
        number=sample.pr.number,
        url=sample.pr.url,
        author=sample.pr.author,
        size_lines=sample.pr.size_lines,
        ttfr_hours=_round(sample.ttfr_hours),
        cycle_hours=round(sample.cycle_hours, 2),
    )


def _top(
    samples: Iterable[FlowSample], key: Callable[[FlowSample], tuple[float, int]]
) -> list[PullRequestExample]:
    return [_example(s) for s in sorted(samples, key=key)[:EXAMPLES_PER_LIST]]


def _reviewer_rows(current: PeriodStats, previous: PeriodStats, top_n: int) -> list[ReviewerRow]:
    # Union of both periods' top reviewers, so a heavy reviewer who went quiet stays visible.
    logins = [login for login, _ in current.ranked_reviewers()[:top_n]]
    logins += [login for login, _ in previous.ranked_reviewers()[:top_n] if login not in logins]
    empty = ReviewerTally()
    rows = []
    for login in logins:
        now, before = current.reviewers.get(login, empty), previous.reviewers.get(login, empty)
        rows.append(ReviewerRow(
            login=login,
            prs_reviewed=compare(len(now.prs), len(before.prs),
                                 stats.poisson_delta(len(now.prs), len(before.prs))),
            share_pct=compare(current.share_pct(login), previous.share_pct(login)),
            reviews=now.reviews,
            approvals=now.approvals,
            changes_requested=now.changes_requested,
            inline_comments=now.inline_comments,
        ))
    rows.sort(key=lambda r: (-(r.prs_reviewed.current or 0), -(r.prs_reviewed.previous or 0),
                             r.login))
    return rows


def _period(window: Window) -> Period:
    return Period(since=window.since, until=window.until,
                  days=round(window.length.total_seconds() / 86400, 2))


def compute_review_flow(
    repository: str,
    prs: Sequence[PullRequest],
    window: Window,
    *,
    data_as_of: datetime,
    top_n: int,
) -> ReviewFlowInsight:
    current = period_stats(prs, window)
    previous = period_stats(prs, window.baseline)
    now_values, prev_values = current.summary_values(), previous.summary_values()
    intervals = _delta_intervals(current, previous)
    summary = Summary(**{
        name: compare(now_values[name], prev_values[name], intervals.get(name))
        for name in now_values
    })
    return ReviewFlowInsight(
        repository=repository,
        period=_period(window),
        baseline_period=_period(window.baseline),
        data_as_of=data_as_of,
        summary=summary,
        reviewers=_reviewer_rows(current, previous, top_n),
        segments=_segments(current),
        drivers=Drivers(
            by_size=_decompose(current, previous, _size_bucket, len(_SIZE_BUCKETS)),
            by_author=_decompose(current, previous, _author_type, 2),
        ),
        examples=Examples(
            slowest_first_review=_top(
                current.reviewed(), key=lambda s: (-(s.ttfr_hours or 0.0), s.pr.number)
            ),
            merged_without_review=_top(
                (s for s in current.samples if s.ttfr_hours is None),
                key=lambda s: (-s.pr.size_lines, s.pr.number),
            ),
        ),
    )

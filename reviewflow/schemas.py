"""Response models. Field descriptions are the metric definitions: they render in the
OpenAPI docs and double as fact labels for the narrative, so there is one source of truth."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

Number = int | float
Interval = tuple[float, float]


def _doc(description: str) -> Any:
    return Field(description=description)


class Period(BaseModel):
    since: datetime
    until: datetime
    days: float


class Comparison(BaseModel):
    """A metric in the requested period next to the preceding period of equal length."""

    current: Number | None
    previous: Number | None
    delta: Number | None = _doc("current - previous")
    delta_pct: float | None = _doc("Relative change in percent; null if previous is 0 or null")
    delta_ci: Interval | None = _doc(
        "95% interval for delta: bootstrap for medians and shares, Poisson for counts. "
        "Null when either period has too few samples or the metric has no interval.")
    significant: bool | None = _doc("Whether delta_ci excludes zero; null without an interval")


class Summary(BaseModel):
    merged_prs: Comparison = _doc("Human-authored PRs merged in the period")
    bot_prs_excluded: Comparison = _doc(
        "Bot-authored PRs merged in the period, excluded from flow metrics")
    merged_without_review_pct: Comparison = _doc(
        "Share of merged PRs with no review before merge")
    ttfr_p50_hours: Comparison = _doc("Median hours from ready-for-review to first review")
    ttfr_p90_hours: Comparison = _doc(
        "90th percentile hours from ready-for-review to first review")
    review_to_merge_p50_hours: Comparison = _doc("Median hours from first review to merge")
    cycle_p50_hours: Comparison = _doc("Median hours from ready-for-review to merge")
    cycle_p90_hours: Comparison = _doc("90th percentile hours from ready-for-review to merge")
    pr_size_p50_lines: Comparison = _doc("Median lines changed per merged PR")
    active_reviewers: Comparison = _doc("People who submitted at least one review")
    pr_reviews: Comparison = _doc("Distinct (PR, reviewer) pairs with a review in the period")
    top_reviewer_share_pct: Comparison = _doc(
        "Share of PR reviews done by the busiest reviewer")
    review_bus_factor: Comparison = _doc(
        "Fewest reviewers who together did half of all PR reviews")


class ReviewerRow(BaseModel):
    login: str
    prs_reviewed: Comparison = _doc("Distinct PRs reviewed")
    share_pct: Comparison = _doc("Share of all PR reviews")
    reviews: int = _doc("Review submissions this period")
    approvals: int = _doc("Approving reviews this period")
    changes_requested: int = _doc("Reviews requesting changes this period")
    inline_comments: int = _doc("Inline review comments this period")


class SegmentRow(BaseModel):
    key: str
    label: str
    merged_prs: int = _doc("Merged PRs")
    ttfr_p50_hours: float | None = _doc("Median hours to first review")
    cycle_p50_hours: float | None = _doc("Median hours from ready to merge")
    ttfr_p50_vs_overall: float | None = _doc(
        "Median wait for first review relative to all merged PRs")


class Segments(BaseModel):
    by_size: list[SegmentRow]
    by_author: list[SegmentRow]


class Effect(BaseModel):
    hours: float
    ci: Interval = _doc("95% bootstrap interval")
    significant: bool = _doc("Whether ci excludes zero")


class Decomposition(BaseModel):
    """The change in median wait for first review, split along one PR attribute.
    mix + within equals the change."""

    mix: Effect = _doc(
        "Hours of the change in median wait explained by a shift in which kinds of PR "
        "were merged")
    within: Effect = _doc(
        "Hours of the change in median wait explained by PRs of the same kind waiting "
        "longer or shorter")


class Drivers(BaseModel):
    by_size: Decomposition | None = _doc("Split by PR size bucket")
    by_author: Decomposition | None = _doc("Split by author type (write access or not)")


class PullRequestExample(BaseModel):
    number: int
    url: str
    author: str
    size_lines: int = _doc("Lines changed")
    ttfr_hours: float | None = _doc("Hours to first review")
    cycle_hours: float = _doc("Hours from ready to merge")


class Examples(BaseModel):
    slowest_first_review: list[PullRequestExample]
    merged_without_review: list[PullRequestExample]


class ReviewFlowInsight(BaseModel):
    repository: str
    period: Period
    baseline_period: Period
    data_as_of: datetime = _doc("Upstream data is complete up to this time")
    summary: Summary
    reviewers: list[ReviewerRow]
    segments: Segments
    drivers: Drivers = _doc("What moved the median wait for first review; null parts "
                            "when either period has too few reviewed PRs")
    examples: Examples

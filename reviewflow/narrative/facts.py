"""Flatten a ReviewFlowInsight into addressable facts.

A fact is one number with a stable id, a human label (taken from the schema's field
descriptions), a unit for formatting and a JSON pointer back into the insight response.
Only numbers and constrained identifiers reach the model: no PR titles, bodies or other
free text from GitHub, which removes the prompt-injection surface instead of filtering it.
"""

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from reviewflow.schemas import (
    Comparison,
    Decomposition,
    Drivers,
    Interval,
    Number,
    PullRequestExample,
    ReviewerRow,
    ReviewFlowInsight,
    SegmentRow,
    Summary,
)

Unit = Literal["count", "hours", "percent", "pp", "ratio", "lines"]

# GitHub logins are alphanumerics and hyphens (underscores in enterprise-managed accounts).
_SAFE_LOGIN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_REVIEWER_COMPARISONS = ("prs_reviewed", "share_pct")
_REVIEWER_FIELDS = ("approvals", "changes_requested", "inline_comments")
_SEGMENT_FIELDS = ("merged_prs", "ttfr_p50_hours", "cycle_p50_hours", "ttfr_p50_vs_overall")
_EXAMPLE_FIELDS = ("size_lines", "ttfr_hours", "cycle_hours")


@dataclass(frozen=True, slots=True)
class Fact:
    id: str
    label: str
    value: Number
    unit: Unit
    source: str  # JSON pointer into the insight response
    signed: bool = False
    # Set on changes: the 95% interval, and whether it excludes zero.
    interval: Interval | None = None
    significant: bool | None = None

    @property
    def display(self) -> str:
        return format_value(self.value, self.unit, signed=self.signed)


@dataclass(frozen=True, slots=True)
class Entity:
    """A reviewer or PR the model may mention by reference."""

    display: str
    source: str


@dataclass(frozen=True, slots=True)
class FactSheet:
    facts: dict[str, Fact]
    entities: dict[str, Entity]  # keyed "@login" / "#123"


def _trim(value: float) -> str:
    text = f"{value:,.1f}"
    return text[:-2] if text.endswith(".0") else text


def format_value(value: Number, unit: Unit, *, signed: bool = False) -> str:
    sign = "+" if signed and value > 0 else ""
    match unit:
        case "hours":
            return f"{sign}{value / 24:.1f} days" if abs(value) >= 48 else f"{sign}{value:.1f}h"
        case "percent":
            return f"{sign}{_trim(value)}%"
        case "pp":
            return f"{sign}{_trim(value)} pp"
        case "ratio":
            return f"{value:.1f}×"  # noqa: RUF001  (deliberate multiplication sign)
        case "lines":
            return f"{sign}{value:,.0f} lines"
        case "count":
            return f"{sign}{_trim(value)}"


def _unit(field_name: str) -> Unit:
    if field_name.endswith("_hours"):
        return "hours"
    if field_name.endswith("_pct"):
        return "percent"
    if field_name.endswith("_lines"):
        return "lines"
    if field_name.endswith("_vs_overall"):
        return "ratio"
    return "count"


def _description(model: type[BaseModel], field_name: str) -> str:
    return model.model_fields[field_name].description or field_name.replace("_", " ")


class _Builder:
    def __init__(self) -> None:
        self.facts: dict[str, Fact] = {}
        self.entities: dict[str, Entity] = {}

    def add(self, fact_id: str, label: str, value: Number | None, unit: Unit, source: str,
            *, signed: bool = False, interval: Interval | None = None,
            significant: bool | None = None) -> None:
        if value is not None:
            self.facts[fact_id] = Fact(fact_id, label, value, unit, source, signed,
                                       interval, significant)

    def comparison(self, fact_id: str, label: str, comparison: Comparison, unit: Unit,
                   source: str) -> None:
        self.add(f"{fact_id}.current", f"{label} (this period)", comparison.current,
                 unit, f"{source}/current")
        self.add(f"{fact_id}.previous", f"{label} (previous period)", comparison.previous,
                 unit, f"{source}/previous")
        self.add(f"{fact_id}.delta", f"{label}: change", comparison.delta,
                 "pp" if unit == "percent" else unit, f"{source}/delta", signed=True,
                 interval=comparison.delta_ci, significant=comparison.significant)
        self.add(f"{fact_id}.delta_pct", f"{label}: relative change", comparison.delta_pct,
                 "percent", f"{source}/delta_pct", signed=True,
                 significant=comparison.significant)

    def summary(self, summary: Summary) -> None:
        for name in Summary.model_fields:
            self.comparison(f"summary.{name}", _description(Summary, name),
                            getattr(summary, name), _unit(name), f"/summary/{name}")

    def reviewers(self, rows: list[ReviewerRow]) -> None:
        for index, row in enumerate(rows):
            if not _SAFE_LOGIN.match(row.login):
                continue
            source, prefix = f"/reviewers/{index}", f"reviewers.{row.login}"
            self.entities[f"@{row.login}"] = Entity(f"@{row.login}", source)
            for name in _REVIEWER_COMPARISONS:
                self.comparison(f"{prefix}.{name}",
                                f"@{row.login}: {_description(ReviewerRow, name)}",
                                getattr(row, name), _unit(name), f"{source}/{name}")
            for name in _REVIEWER_FIELDS:
                self.add(f"{prefix}.{name}", f"@{row.login}: {_description(ReviewerRow, name)}",
                         getattr(row, name), _unit(name), f"{source}/{name}")

    def drivers(self, drivers: Drivers) -> None:
        for name in Drivers.model_fields:
            decomposition: Decomposition | None = getattr(drivers, name)
            if decomposition is None:
                continue
            split = _description(Drivers, name)
            for part in Decomposition.model_fields:
                effect = getattr(decomposition, part)
                self.add(f"drivers.{name}.{part}",
                         f"{_description(Decomposition, part)} ({split.lower()})",
                         effect.hours, "hours", f"/drivers/{name}/{part}/hours", signed=True,
                         interval=effect.ci, significant=effect.significant)

    def segments(self, group: str, rows: list[SegmentRow]) -> None:
        for index, row in enumerate(rows):
            for name in _SEGMENT_FIELDS:
                self.add(f"segments.{row.key}.{name}",
                         f"{row.label}: {_description(SegmentRow, name)}",
                         getattr(row, name), _unit(name),
                         f"/segments/{group}/{index}/{name}")

    def examples(self, group: str, rows: list[PullRequestExample]) -> None:
        for index, row in enumerate(rows):
            source = f"/examples/{group}/{index}"
            self.entities.setdefault(f"#{row.number}", Entity(f"#{row.number}", source))
            if _SAFE_LOGIN.match(row.author):
                self.entities.setdefault(f"@{row.author}", Entity(f"@{row.author}", source))
            for name in _EXAMPLE_FIELDS:
                self.add(f"pr.{row.number}.{name}",
                         f"PR #{row.number}: {_description(PullRequestExample, name)}",
                         getattr(row, name), _unit(name), f"{source}/{name}")


def build_fact_sheet(insight: ReviewFlowInsight) -> FactSheet:
    builder = _Builder()
    builder.summary(insight.summary)
    builder.reviewers(insight.reviewers)
    builder.segments("by_size", insight.segments.by_size)
    builder.segments("by_author", insight.segments.by_author)
    builder.drivers(insight.drivers)
    builder.examples("slowest_first_review", insight.examples.slowest_first_review)
    builder.examples("merged_without_review", insight.examples.merged_without_review)
    return FactSheet(builder.facts, builder.entities)

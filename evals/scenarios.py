"""Synthetic repository histories with a planted root cause.

Each scenario simulates two equal periods of PR activity that differ in one known way, or
in none. A narrative is correct when its hypothesis cites the facts that carry the planted
signal, or, when nothing was planted, when it offers no hypothesis at all.
"""

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from reviewflow.models import PullRequest, Review, ReviewState
from reviewflow.narrative.models import ReviewFlowNarrative
from reviewflow.period import Window

PERIOD_DAYS = 30
AUTHORS = ["ana", "ben", "chen", "dara", "eli", "fay", "gus", "hana"]
CONTRIBUTORS = ["ext-ivo", "ext-jun", "ext-kai", "ext-lea"]


@dataclass(frozen=True)
class Period:
    reviewers: dict[str, float]  # login -> relative share of reviews
    wait_hours: float  # mean time to first review
    size_median: int = 120
    contributor_share: float = 0.2
    contributor_wait_factor: float = 1.0
    size_sensitive_wait: bool = False
    prs_per_day: int = 3


@dataclass(frozen=True)
class Scenario:
    name: str
    planted: str
    previous: Period
    current: Period
    # Fact-id prefixes the hypothesis must cite: one from each group. None: no cause.
    expected: tuple[tuple[str, ...], ...] | None

    def judge(self, narrative: ReviewFlowNarrative) -> str | None:
        """Why the narrative is wrong, or None if it is right."""
        hypothesis = narrative.hypothesis
        if self.expected is None:
            return None if hypothesis is None else f"invented a cause: {hypothesis.statement}"
        if hypothesis is None:
            return "offered no hypothesis"
        cited = [e.fact_id for e in hypothesis.evidence]
        missing = [group for group in self.expected
                   if not any(c.startswith(group) for c in cited)]
        return f"evidence {cited} cites none of {missing}" if missing else None

    def build(self, end: datetime, seed: int = 7) -> tuple[list[PullRequest], Window]:
        rng = random.Random(f"{self.name}:{seed}")
        window = Window(end - timedelta(days=PERIOD_DAYS), end)
        prs = _simulate(rng, window.baseline.since, self.previous, first_number=1000)
        prs += _simulate(rng, window.since, self.current, first_number=2000)
        return prs, window


def _simulate(
    rng: random.Random, start: datetime, p: Period, first_number: int
) -> list[PullRequest]:
    prs = []
    number = first_number
    for day in range(PERIOD_DAYS):
        for _ in range(max(0, p.prs_per_day + rng.randint(-1, 1))):
            number += 1
            ready = start + timedelta(days=day, hours=rng.uniform(8, 18))
            size = max(1, int(rng.lognormvariate(math.log(p.size_median), 0.7)))
            external = rng.random() < p.contributor_share
            wait = rng.expovariate(1 / p.wait_hours)
            if external:
                wait *= p.contributor_wait_factor
            if p.size_sensitive_wait:
                wait *= max(0.3, size / 150)
            reviewer = rng.choices(list(p.reviewers), weights=list(p.reviewers.values()))[0]
            author = rng.choice(CONTRIBUTORS if external else
                                [a for a in AUTHORS if a != reviewer])
            first = ready + timedelta(hours=wait)
            approved = first + timedelta(hours=rng.uniform(1, 10))
            merged = approved + timedelta(hours=rng.uniform(0.2, 3))
            prs.append(PullRequest(
                number=number,
                url=f"https://github.com/acme/widgets/pull/{number}",
                author=author,
                author_is_bot=False,
                author_association="CONTRIBUTOR" if external else "MEMBER",
                created_at=ready,
                ready_at=ready,
                merged_at=merged,
                additions=int(size * 0.7),
                deletions=size - int(size * 0.7),
                reviews=(
                    Review(f"R{number}a", reviewer, False, ReviewState.COMMENTED, first,
                           rng.randint(0, 4)),
                    Review(f"R{number}b", reviewer, False, ReviewState.APPROVED, approved, 0),
                ),
            ))
    return prs


_TEAM = {"ana": 5.0, "ben": 2.0, "chen": 2.0, "dara": 1.0}

SCENARIOS = [
    Scenario(
        name="key_reviewer_absent",
        planted="The reviewer who did half the reviews stopped; waits for first review grew.",
        previous=Period(reviewers=_TEAM, wait_hours=4),
        current=Period(reviewers={"ben": 2.0, "chen": 2.0, "dara": 1.0}, wait_hours=14),
        expected=(("reviewers.ana.",), ("summary.ttfr_", "drivers.")),
    ),
    Scenario(
        name="larger_prs",
        planted="PRs got much larger, and large PRs wait longer for a first review.",
        previous=Period(reviewers=_TEAM, wait_hours=5, size_median=80, size_sensitive_wait=True),
        current=Period(reviewers=_TEAM, wait_hours=5, size_median=450, size_sensitive_wait=True),
        expected=(("summary.pr_size_p50_lines.", "drivers.by_size.mix", "segments.large."),),
    ),
    Scenario(
        name="contributors_wait",
        planted="External contributors' PRs wait far longer than maintainers' PRs.",
        previous=Period(reviewers=_TEAM, wait_hours=5, contributor_share=0.4),
        current=Period(reviewers=_TEAM, wait_hours=5, contributor_share=0.4,
                       contributor_wait_factor=6.0),
        expected=(("segments.contributor.", "drivers.by_author.within"),),
    ),
    Scenario(
        name="contributor_mix",
        planted="Contributors always waited longer; now far more of the PRs are theirs.",
        previous=Period(reviewers=_TEAM, wait_hours=4, contributor_share=0.1,
                        contributor_wait_factor=5.0),
        current=Period(reviewers=_TEAM, wait_hours=4, contributor_share=0.6,
                       contributor_wait_factor=5.0),
        expected=(("drivers.by_author.mix",),),
    ),
    Scenario(
        name="load_concentrates",
        planted="One reviewer took over most reviews; waits did not change.",
        previous=Period(reviewers=_TEAM, wait_hours=5),
        current=Period(reviewers={**_TEAM, "ana": 25.0}, wait_hours=5),
        expected=(("summary.top_reviewer_share_pct.", "summary.review_bus_factor.",
                   "reviewers.ana."),),
    ),
    Scenario(
        name="volume_surge",
        planted="Twice as many PRs arrived for the same reviewers, and waits grew.",
        previous=Period(reviewers=_TEAM, wait_hours=4, prs_per_day=3),
        current=Period(reviewers=_TEAM, wait_hours=10, prs_per_day=7),
        expected=(("summary.merged_prs.", "summary.pr_reviews."),
                  ("summary.ttfr_", "drivers.")),
    ),
    Scenario(
        name="steady_state",
        planted="Nothing changed between periods.",
        previous=Period(reviewers=_TEAM, wait_hours=6),
        current=Period(reviewers=_TEAM, wait_hours=6),
        expected=None,
    ),
    Scenario(
        name="steady_state_quiet",
        planted="Nothing changed, in a quiet repository where medians swing by chance.",
        previous=Period(reviewers=_TEAM, wait_hours=6, prs_per_day=1),
        current=Period(reviewers=_TEAM, wait_hours=6, prs_per_day=1),
        expected=None,
    ),
]

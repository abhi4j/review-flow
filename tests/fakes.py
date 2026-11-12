import json
from typing import Any

from reviewflow.narrative.llm import Completion


class FakeLLM:
    """Returns scripted answers in order and records every prompt it was sent."""

    model = "fake-model-1"

    def __init__(self, *answers: dict[str, Any] | str) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    async def complete(self, *, system: str, user: str, schema: dict[str, Any]) -> Completion:
        self.prompts.append(user)
        answer = self._answers.pop(0)
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return Completion(text, input_tokens=100, output_tokens=50)


def good_answer() -> dict[str, Any]:
    """A valid answer for the tests/scenario.py insight. Its sample is too small for any
    change to be significant, so there is no hypothesis."""
    return {
        "analysis": "Merged PRs rose 150%; ttfr p50 went 2h -> 8h; bob did 60% of reviews.",
        "headline": "Reviews slowed as {{@bob}} carried most of the load",
        "narrative": (
            "Merged PRs rose by {{summary.merged_prs.delta_pct}} while median time to first "
            "review grew from {{summary.ttfr_p50_hours.previous}} to "
            "{{summary.ttfr_p50_hours.current}}. {{@bob}} did "
            "{{reviewers.bob.share_pct.current}} of PR reviews."
        ),
        "observations": [
            {"statement": "Median wait for a first review quadrupled.",
             "evidence": ["summary.ttfr_p50_hours.current", "summary.ttfr_p50_hours.previous"]},
            {"statement": "{{#3}} was merged with no review at all.",
             "evidence": ["summary.merged_without_review_pct.current"]},
        ],
        "hypothesis": None,
    }


def hypothesis(*evidence: str, confidence: float = 0.9) -> dict[str, Any]:
    return {
        "statement": "Review capacity left with {{@ana}}.",
        "evidence": list(evidence),
        "confidence": confidence,
        "alternatives": ["Larger PRs this period took longer to pick up."],
        "how_to_verify": "Check whether {{@ana}} was away.",
    }


def cause_answer() -> dict[str, Any]:
    """A valid answer for the key_reviewer_absent eval scenario (seed 7)."""
    return {
        "analysis": "ana went from 44 PR reviews to none; ttfr p50 tripled.",
        "headline": "Waits for review grew after {{@ana}} stopped reviewing",
        "narrative": "Median wait for a first review rose by {{summary.ttfr_p50_hours.delta}}.",
        "observations": [
            {"statement": "{{@ana}} reviewed no PRs this period.",
             "evidence": ["reviewers.ana.prs_reviewed.delta"]},
        ],
        "hypothesis": hypothesis("summary.ttfr_p50_hours.delta",
                                 "reviewers.ana.prs_reviewed.delta"),
    }

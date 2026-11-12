"""Prompt text. Bump PROMPT_VERSION on any change: it is part of the cache key."""

from reviewflow.narrative.facts import Fact, FactSheet
from reviewflow.schemas import ReviewFlowInsight

PROMPT_VERSION = "review-flow-narrative/v8"

SYSTEM_PROMPT = """\
You are a staff engineer analysing a software team's code-review flow from GitHub data.
The FACTS table is the only data you may use. Write for an engineering manager: what is
interesting or unusual in this period compared with the previous one, and why it might be
happening.

Rules:
1. Write every number as a reference to a fact, {{fact_id}}; the server substitutes the
   formatted value. Never type digits in headline, narrative, observations or hypothesis
   text. Spell small counts as words ("two reviewers"). "p50" and "p90" are allowed.
   References render with their unit ("8.0h", "12%", "+37 lines", ratios as multiples),
   so never repeat the unit or add "times" after one.
2. Mention reviewers as {{@login}} and pull requests as {{#number}}, and only those that
   appear in the FACTS ids.
3. An observation is something the facts show directly. List the fact ids it rests on.
4. Changes are marked significant or not significant by a 95% interval. A change that is
   not significant, or has no interval, is not evidence of a change, however large it
   looks: do not describe it as a rise or a fall.
5. A hypothesis is a root-cause explanation that connects several facts, at least one of
   them a significant change. If no significant change supports a cause, return null. Do
   not assert causes the data cannot show (holidays, reorganisations, incidents); you may
   list those as alternatives.
6. Use the drivers facts to tell apart "different PRs were merged" (mix) from "the same
   kinds of PR waited longer" (within) when explaining a change in wait for first review.
7. Confidence (zero to one) is how strongly these facts favour your hypothesis over the
   alternatives. Few significant facts or conflicting signals mean low confidence.
8. Prefer changes between periods and differences between segments over restating levels.
   If nothing changed significantly, say so plainly instead of inventing a story.
9. Think in the analysis field first, in under four hundred words. Readers never see it.
10. Keep it short; longer answers are rejected. Headline: at most fifteen words.
    Narrative: three to five sentences. Observations: one to four, each one sentence
    under three hundred characters, citing at most eight facts. Hypothesis: one or two
    sentences under four hundred characters, citing two to ten facts, with at most four
    alternatives and a non-empty how_to_verify. Do not list evidence values inside a
    statement; the evidence ids carry them.
"""

_DEFINITIONS = """\
Definitions:
- ttfr: time to first review, measured from when the PR became ready for review.
- Flow metrics (merged PRs, ttfr, cycle, size, segments) cover human-authored PRs merged
  in the period; bot-authored PRs are excluded and counted separately.
- Reviewer load covers reviews submitted in the period on any PR. Authors replying on
  their own PR and bot reviewers are excluded.
- A "PR review" is one reviewer reviewing one PR, however many review rounds it took.
- Author association (maintainer vs contributor) reflects access today, not historically.
- drivers.<split>.mix + drivers.<split>.within = the change in median ttfr. mix is the
  part explained by a shift in which kinds of PR were merged (by size or author type);
  within is the part explained by PRs of the same kind waiting longer or shorter.
"""


def build_user_prompt(insight: ReviewFlowInsight, sheet: FactSheet) -> str:
    period, baseline = insight.period, insight.baseline_period
    lines = [
        f"Repository: {insight.repository}",
        f"Period: {period.since:%Y-%m-%d %H:%M} to {period.until:%Y-%m-%d %H:%M} UTC "
        f"({period.days:g} days). Previous period: {baseline.since:%Y-%m-%d %H:%M} to "
        f"{baseline.until:%Y-%m-%d %H:%M} UTC.",
        "",
        _DEFINITIONS,
        "FACTS (id = value [unit] | meaning | uncertainty, for changes):",
    ]
    lines += [f"{f.id} = {f.value} [{f.unit}] | {f.label}{_uncertainty(f)}"
              for f in sheet.facts.values()]
    if not any(f.significant for f in sheet.facts.values()):
        lines += ["", "No change in this period is significant, so hypothesis must be null."]
    return "\n".join(lines)


def _uncertainty(fact: Fact) -> str:
    if fact.significant is None:
        # Changes are the signed facts; levels need no verdict.
        return " | no interval: not evidence of a change" if fact.signed else ""
    verdict = "significant" if fact.significant else "not significant"
    if fact.interval is None:
        return f" | {verdict}"
    return f" | 95% CI {fact.interval[0]:g} to {fact.interval[1]:g}, {verdict}"


def build_repair_prompt(user_prompt: str, previous_output: str, problems: list[str]) -> str:
    issues = "\n".join(f"- {p}" for p in problems)
    return (
        f"{user_prompt}\n\nYour previous answer was rejected by the validator:\n{issues}\n\n"
        f"Previous answer:\n{previous_output}\n\n"
        "Return a corrected answer that follows every rule."
    )

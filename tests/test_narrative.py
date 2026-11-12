import asyncio
import json
from datetime import UTC, datetime

import pytest

from evals.scenarios import SCENARIOS
from reviewflow.errors import NarrativeFailed, NarrativeUnavailable
from reviewflow.metrics import compute_review_flow
from reviewflow.narrative.contract import parse_and_validate
from reviewflow.narrative.facts import build_fact_sheet, format_value
from reviewflow.narrative.service import NarrativeService
from reviewflow.store import Store
from tests.factories import at
from tests.fakes import FakeLLM, cause_answer, good_answer, hypothesis
from tests.scenario import WINDOW, scenario_prs


@pytest.fixture
def insight():
    return compute_review_flow("acme/widgets", scenario_prs(), WINDOW,
                               data_as_of=at(20), top_n=10)


@pytest.fixture(scope="module")
def cause_insight():
    """Large enough for significant changes: the key reviewer stopped reviewing."""
    end = datetime(2026, 11, 1, tzinfo=UTC)
    scenario = next(s for s in SCENARIOS if s.name == "key_reviewer_absent")
    prs, window = scenario.build(end)
    return compute_review_flow("acme/widgets", prs, window, data_as_of=end, top_n=10)


@pytest.fixture
async def store():
    s = await Store.open(":memory:")
    yield s
    await s.close()


def _resolve(document: dict, pointer: str):
    node = document
    for part in pointer.strip("/").split("/"):
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def test_every_fact_points_at_its_value_in_the_insight(insight):
    document = json.loads(insight.model_dump_json())
    sheet = build_fact_sheet(insight)
    assert len(sheet.facts) > 50
    for fact in sheet.facts.values():
        assert _resolve(document, fact.source) == fact.value, fact.id
    for key, entity in sheet.entities.items():
        assert _resolve(document, entity.source)  # the row exists
        assert key in {"@bob", "@alice", "@dave", "@erin", "@carol", "#1", "#2", "#3", "#5", "#6"}


def test_null_metrics_are_not_facts(insight):
    sheet = build_fact_sheet(insight)
    assert "summary.merged_without_review_pct.delta_pct" not in sheet.facts  # from zero


@pytest.mark.parametrize(("value", "unit", "signed", "expected"), [
    (8.0, "hours", False, "8.0h"),
    (72.0, "hours", False, "3.0 days"),
    (300.0, "percent", True, "+300%"),
    (-1, "count", True, "-1"),
    (26.7, "pp", True, "+26.7 pp"),
    (1.25, "ratio", False, "1.2×"),  # noqa: RUF001
    (1200, "lines", False, "1,200 lines"),
    (-0.03, "hours", True, "0.0h"),  # below display precision: no sign
])
def test_format_value(value, unit, signed, expected):
    assert format_value(value, unit, signed=signed) == expected


class TestContract:
    def test_valid_answer(self, insight):
        draft, problems = parse_and_validate(json.dumps(good_answer()), build_fact_sheet(insight))
        assert problems == []
        assert draft is not None

    def test_rejects_numbers_typed_by_the_model(self, insight):
        answer = good_answer()
        answer["narrative"] = "Reviews took 8 hours, up from {{summary.ttfr_p50_hours.previous}}."
        _, problems = parse_and_validate(json.dumps(answer), build_fact_sheet(insight))
        assert any("number written directly" in p for p in problems)

    def test_allows_percentile_names_and_numbers_in_analysis(self, insight):
        answer = good_answer()
        answer["headline"] = "The p90 wait is where it hurts"
        answer["analysis"] = "p90 went from 2.8 to 11.4"
        _, problems = parse_and_validate(json.dumps(answer), build_fact_sheet(insight))
        assert problems == []

    def test_rejects_unknown_references_and_evidence(self, insight):
        answer = good_answer()
        answer["headline"] = "{{@mallory}} and {{summary.made_up.current}}"
        answer["observations"][0]["evidence"] = ["summary.not_a_fact.current"]
        _, problems = parse_and_validate(json.dumps(answer), build_fact_sheet(insight))
        assert len(problems) == 3

    def test_hypothesis_needs_two_facts(self, cause_insight):
        answer = cause_answer()
        answer["hypothesis"] = hypothesis("summary.ttfr_p50_hours.delta")
        draft, problems = parse_and_validate(json.dumps(answer), build_fact_sheet(cause_insight))
        assert draft is None and any("hypothesis.evidence" in p for p in problems)

    def test_hypothesis_needs_a_significant_change(self, cause_insight):
        sheet = build_fact_sheet(cause_insight)
        answer = cause_answer()
        _, problems = parse_and_validate(json.dumps(answer), sheet)
        assert problems == []
        answer["hypothesis"] = hypothesis("summary.ttfr_p50_hours.current",
                                          "reviewers.ana.prs_reviewed.previous")
        _, problems = parse_and_validate(json.dumps(answer), sheet)
        assert any("cites no significant change" in p for p in problems)

    def test_small_samples_cannot_support_a_hypothesis(self, insight):
        answer = good_answer()
        answer["hypothesis"] = hypothesis("summary.merged_prs.delta", "summary.pr_reviews.delta")
        _, problems = parse_and_validate(json.dumps(answer), build_fact_sheet(insight))
        assert any("cites no significant change" in p for p in problems)

    def test_not_json(self, insight):
        _, problems = parse_and_validate("Sure! Here's my analysis", build_fact_sheet(insight))
        assert problems and "not valid JSON" in problems[0]


class TestService:
    async def test_renders_numbers_from_facts(self, insight, store):
        llm = FakeLLM(good_answer())
        result = await NarrativeService(llm, store).narrate(insight)
        assert result.narrative.startswith("Merged PRs rose by +150%")
        assert "from 2.0h to 8.0h" in result.narrative
        assert "@bob did 60%" in result.narrative
        assert result.observations[1].statement == "#3 was merged with no review at all."
        assert result.hypothesis is None
        assert result.template.headline == "Reviews slowed as {{@bob}} carried most of the load"
        assert result.references["@bob"].source == "/reviewers/0"
        assert result.meta.attempts == 1 and result.meta.cached is False

    async def test_hypothesis_evidence_carries_uncertainty_and_confidence_is_capped(
        self, cause_insight, store
    ):
        result = await NarrativeService(FakeLLM(cause_answer()), store).narrate(cause_insight)
        evidence = result.hypothesis.evidence
        assert evidence[0].source == "/summary/ttfr_p50_hours/delta"
        assert evidence[0].significant is True
        assert evidence[0].interval == cause_insight.summary.ttfr_p50_hours.delta_ci
        assert result.hypothesis.confidence.model_reported == 0.9
        assert result.hypothesis.confidence.score == 0.85  # ceiling for a large sample

    async def test_repairs_once_with_the_validator_feedback(self, insight, store):
        bad = good_answer()
        bad["narrative"] = "Median review wait was 8 hours."
        llm = FakeLLM(bad, good_answer())
        result = await NarrativeService(llm, store).narrate(insight)
        assert result.meta.attempts == 2
        assert "rejected by the validator" in llm.prompts[1]
        assert "number written directly" in llm.prompts[1]

    async def test_fails_closed_when_repair_also_fails(self, insight, store):
        bad = good_answer()
        bad["narrative"] = "Median review wait was 8 hours."
        with pytest.raises(NarrativeFailed):
            await NarrativeService(FakeLLM(bad, bad), store).narrate(insight)

    async def test_identical_inputs_hit_the_cache(self, insight, store):
        llm = FakeLLM(good_answer())
        service = NarrativeService(llm, store)
        first = await service.narrate(insight)
        second = await service.narrate(insight)
        assert len(llm.prompts) == 1
        assert second.meta.cached is True
        assert second.narrative == first.narrative

    async def test_concurrent_identical_requests_share_one_generation(self, insight, store):
        class SlowLLM(FakeLLM):
            async def complete(self, **kwargs):
                await asyncio.sleep(0.01)
                return await super().complete(**kwargs)

        llm = SlowLLM(good_answer())
        service = NarrativeService(llm, store)
        first, second = await asyncio.gather(service.narrate(insight), service.narrate(insight))
        assert len(llm.prompts) == 1
        assert first.narrative == second.narrative

    async def test_too_little_activity_skips_the_model(self, store):
        empty = compute_review_flow("acme/widgets", [], WINDOW, data_as_of=at(20), top_n=10)
        llm = FakeLLM()
        result = await NarrativeService(llm, store).narrate(empty)
        assert result.meta.llm_called is False and llm.prompts == []
        assert "has 0 merged PRs and 0 PR reviews" in result.narrative

    async def test_missing_api_key_is_a_clear_503(self, insight, store):
        with pytest.raises(NarrativeUnavailable):
            await NarrativeService(None, store).narrate(insight)

    async def test_prompt_contains_only_numbers_and_identifiers(self, insight, store):
        llm = FakeLLM(good_answer())
        await NarrativeService(llm, store).narrate(insight)
        prompt = llm.prompts[0]
        assert "summary.ttfr_p50_hours.current = 8.0 [hours]" in prompt
        assert "summary.merged_prs.delta = 3 [count]" in prompt
        assert "95% CI -2.19 to 8.19, not significant" in prompt
        assert "summary.review_bus_factor.delta = -1 [count]" in prompt
        assert "no interval: not evidence of a change" in prompt
        assert "hypothesis must be null" in prompt  # nothing in this small sample is
        assert "https://" not in prompt  # no URLs or free text from GitHub

    async def test_prompt_does_not_forbid_a_hypothesis_when_a_change_is_significant(
        self, cause_insight, store
    ):
        llm = FakeLLM(cause_answer())
        await NarrativeService(llm, store).narrate(cause_insight)
        assert "hypothesis must be null" not in llm.prompts[0]

"""The evals are only meaningful if each planted cause is statistically visible in the
metrics, for every seed the eval runs, and if the no-change scenarios stay quiet."""

from datetime import UTC, datetime

import pytest

from evals.scenarios import SCENARIOS
from reviewflow.metrics import compute_review_flow
from reviewflow.narrative.facts import build_fact_sheet

END = datetime(2026, 11, 1, tzinfo=UTC)
SEEDS = (1, 2, 3)


def _insight(name: str, seed: int):
    scenario = next(s for s in SCENARIOS if s.name == name)
    prs, window = scenario.build(END, seed)
    return compute_review_flow("acme/widgets", prs, window, data_as_of=END, top_n=10)


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("scenario", [s for s in SCENARIOS if s.expected],
                         ids=lambda s: s.name)
def test_each_expected_group_has_a_significant_fact(scenario, seed):
    facts = build_fact_sheet(_insight(scenario.name, seed)).facts.values()
    for group in scenario.expected:
        assert any(f.significant and f.id.startswith(group) for f in facts), group


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("name", ["steady_state", "steady_state_quiet"])
def test_no_change_scenarios_show_no_change_in_wait(name, seed):
    insight = _insight(name, seed)
    assert not insight.summary.ttfr_p50_hours.significant
    for split in (insight.drivers.by_size, insight.drivers.by_author):
        assert split is None or not (split.mix.significant or split.within.significant)


@pytest.mark.parametrize("seed", SEEDS)
def test_drivers_tell_a_mix_shift_from_slower_reviews(seed):
    mix = _insight("contributor_mix", seed).drivers.by_author
    assert mix.mix.significant and mix.mix.hours > 0 and not mix.within.significant
    slower = _insight("contributors_wait", seed).drivers.by_author
    assert slower.within.significant and slower.within.hours > 0 and not slower.mix.significant

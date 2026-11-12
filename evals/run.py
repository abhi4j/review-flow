"""Measure the narrative against planted-cause scenarios with the configured model.

    uv run python -m evals.run --seeds 3 --out evals/results.md

Every scenario runs once per seed (a different simulated history each time). Reported:

  accuracy      cause scenarios whose hypothesis cites the planted signal
  abstention    no-change scenarios answered without a hypothesis
  grounded      answers that passed validation, possibly after one repair
  repair rate   answers that needed the repair attempt
  Brier score   mean (confidence - correct)^2 over answers with a hypothesis, for the
                model's own confidence and for the service's capped score; lower is better

Exit status is non-zero when accuracy or abstention falls below --min-score, so this can
gate prompt and model changes.
"""

import argparse
import asyncio
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from evals.scenarios import SCENARIOS, Scenario
from reviewflow.config import Settings
from reviewflow.errors import ServiceError
from reviewflow.logs import configure_logging
from reviewflow.metrics import compute_review_flow
from reviewflow.narrative.llm import LLM, create_llm
from reviewflow.narrative.prompt import PROMPT_VERSION
from reviewflow.narrative.service import NarrativeService
from reviewflow.store import Store

END = datetime(2026, 11, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Run:
    scenario: Scenario
    seed: int
    error: str | None  # why the answer is wrong, or why there is none
    grounded: bool
    attempts: int
    confidence: tuple[float, float] | None  # (model-reported, capped) when hypothesised
    seconds: float
    tokens: int

    @property
    def correct(self) -> bool:
        return self.grounded and self.error is None


async def _run(llm: LLM, scenario: Scenario, seed: int, limit: asyncio.Semaphore) -> Run:
    prs, window = scenario.build(END, seed)
    insight = compute_review_flow("acme/widgets", prs, window, data_as_of=END, top_n=10)
    store = await Store.open(Path(":memory:"))  # fresh cache: always call the model
    started = time.perf_counter()
    try:
        async with limit:
            narrative = await NarrativeService(llm, store).narrate(insight)
    except ServiceError as exc:
        return Run(scenario, seed, f"{type(exc).__name__}: {exc.detail}", False, 0, None,
                   time.perf_counter() - started, 0)
    finally:
        await store.close()
    h, meta = narrative.hypothesis, narrative.meta
    return Run(
        scenario, seed, scenario.judge(narrative), True, meta.attempts,
        (h.confidence.model_reported, h.confidence.score) if h else None,
        time.perf_counter() - started, (meta.input_tokens or 0) + (meta.output_tokens or 0),
    )


def _share(runs: list[Run]) -> str:
    return f"{sum(r.correct for r in runs)}/{len(runs)}" if runs else "n/a"


def _brier(runs: list[Run], index: int) -> str:
    scored = [(r.confidence[index] - r.correct) ** 2 for r in runs if r.confidence]
    return f"{statistics.mean(scored):.3f} (n={len(scored)})" if scored else "n/a"


def report(runs: list[Run], model: str, seeds: int) -> str:
    causes = [r for r in runs if r.scenario.expected is not None]
    quiet = [r for r in runs if r.scenario.expected is None]
    lines = [
        f"# Narrative evals: {model}, {PROMPT_VERSION}, {seeds} seed(s)",
        "",
        "| Measure | Result |",
        "|---|---|",
        f"| Accuracy on planted causes | {_share(causes)} |",
        f"| Abstention when nothing changed | {_share(quiet)} |",
        f"| Grounded answers | {sum(r.grounded for r in runs)}/{len(runs)} |",
        f"| Needed the repair attempt | {sum(r.attempts > 1 for r in runs)}/{len(runs)} |",
        f"| Brier score, model's confidence | {_brier(runs, 0)} |",
        f"| Brier score, capped confidence | {_brier(runs, 1)} |",
        f"| Median latency | {statistics.median(r.seconds for r in runs):.1f}s |",
        f"| Tokens, total | {sum(r.tokens for r in runs):,} |",
        "",
        "| Scenario | Correct | Planted |",
        "|---|---|---|",
    ]
    for scenario in SCENARIOS:
        mine = [r for r in runs if r.scenario is scenario]
        lines.append(f"| {scenario.name} | {_share(mine)} | {scenario.planted} |")
    failures = [r for r in runs if not r.correct]
    if failures:
        lines += ["", "Failures:", ""]
        lines += [f"- {r.scenario.name} (seed {r.seed}): {r.error}" for r in failures]
    return "\n".join(lines) + "\n"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--min-score", type=float, default=0.8)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    configure_logging("WARNING")  # validator rejections, with their reasons, on stderr

    llm = create_llm(Settings(github_token="unused-by-evals"))
    if llm is None:
        print("Set LLM_API_KEY to run evals.", file=sys.stderr)
        return 2
    limit = asyncio.Semaphore(args.concurrency)
    jobs = [_run(llm, s, seed, limit) for s in SCENARIOS for seed in range(1, args.seeds + 1)]
    runs = []
    for job in asyncio.as_completed(jobs):
        run = await job
        runs.append(run)
        status = "ok  " if run.correct else "FAIL"
        print(f"{status} {run.scenario.name} seed={run.seed} {run.seconds:.1f}s",
              file=sys.stderr)

    text = report(runs, llm.model, args.seeds)
    print(text)
    if args.out:
        args.out.write_text(text)
    rates = [sum(r.correct for r in group) / len(group) for group in (
        [r for r in runs if r.scenario.expected is not None],
        [r for r in runs if r.scenario.expected is None],
    )]
    return 0 if min(rates) >= args.min_score else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

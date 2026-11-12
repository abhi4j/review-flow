"""Orchestrates: facts -> prompt -> model -> validate (repair once) -> render -> cache."""

import asyncio
import hashlib
import json
import logging
import time
from datetime import UTC, datetime

from reviewflow.errors import NarrativeFailed, NarrativeUnavailable
from reviewflow.narrative.contract import (
    OUTPUT_SCHEMA,
    Draft,
    parse_and_validate,
    references_in,
    render,
)
from reviewflow.narrative.facts import FactSheet, build_fact_sheet
from reviewflow.narrative.llm import LLM
from reviewflow.narrative.models import (
    Confidence,
    Evidence,
    Hypothesis,
    NarrativeMeta,
    Observation,
    Reference,
    ReviewFlowNarrative,
    Template,
)
from reviewflow.narrative.prompt import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_repair_prompt,
    build_user_prompt,
)
from reviewflow.schemas import ReviewFlowInsight
from reviewflow.store import Store

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # one answer plus one repair; a second failure is reported, not retried
MIN_ACTIVITY = 3  # below this many merged PRs *and* PR reviews there is nothing to explain


def confidence_ceiling(insight: ReviewFlowInsight) -> tuple[float, str]:
    """Self-reported model confidence is not calibrated, so it is capped by what the
    sample can support. Heuristic thresholds; calibrating them needs labelled outcomes."""
    current = int(insight.summary.merged_prs.current or 0)
    previous = int(insight.summary.merged_prs.previous or 0)
    if current < 10:
        return 0.35, "Fewer than 10 merged PRs in the period."
    if current < 30 or previous < 10:
        return 0.6, "Fewer than 30 merged PRs in the period or 10 in the previous one."
    return 0.85, "Observational data only; capped until the hypothesis is verified."


def cache_key(insight: ReviewFlowInsight, sheet: FactSheet, llm: LLM) -> str:
    material = {
        "prompt": PROMPT_VERSION,
        "model": llm.model,
        "repository": insight.repository.lower(),
        "period": [insight.period.since.isoformat(), insight.period.until.isoformat()],
        "facts": sorted((f.id, f.value) for f in sheet.facts.values()),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


class NarrativeService:
    def __init__(self, llm: LLM | None, store: Store) -> None:
        self._llm = llm
        self._store = store
        # Identical concurrent requests share one generation instead of each paying for it.
        self._in_flight: dict[str, asyncio.Task[ReviewFlowNarrative]] = {}

    def require_llm(self) -> LLM:
        """Fail before any upstream work when the narrative cannot be produced."""
        if self._llm is None:
            raise NarrativeUnavailable("The narrative needs an LLM API key "
                                       "(set LLM_API_KEY).")
        return self._llm

    async def narrate(self, insight: ReviewFlowInsight) -> ReviewFlowNarrative:
        llm = self.require_llm()
        sheet = build_fact_sheet(insight)
        summary = insight.summary
        if (summary.merged_prs.current or 0) < MIN_ACTIVITY and \
                (summary.pr_reviews.current or 0) < MIN_ACTIVITY:
            return self._insufficient(insight, sheet)

        key = cache_key(insight, sheet, llm)
        if cached := await self._store.get_narrative(key):
            narrative = ReviewFlowNarrative.model_validate_json(cached)
            narrative.meta.cached = True
            return narrative
        task = self._in_flight.get(key)
        if task is None:
            task = asyncio.create_task(self._generate(insight, sheet, llm, key))
            self._in_flight[key] = task
            task.add_done_callback(lambda _: self._in_flight.pop(key, None))
        # Shielded: one client disconnecting must not cancel the others' generation.
        return await asyncio.shield(task)

    async def _generate(self, insight: ReviewFlowInsight, sheet: FactSheet, llm: LLM,
                        key: str) -> ReviewFlowNarrative:
        user_prompt = build_user_prompt(insight, sheet)
        prompt, input_tokens, output_tokens = user_prompt, 0, 0
        draft: Draft | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = time.perf_counter()
            completion = await llm.complete(
                system=SYSTEM_PROMPT, user=prompt, schema=OUTPUT_SCHEMA
            )
            input_tokens += completion.input_tokens or 0
            output_tokens += completion.output_tokens or 0
            draft, problems = parse_and_validate(completion.text, sheet)
            log.info("narrative attempt", extra={"fields": {
                "attempt": attempt, "model": llm.model,
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "input_tokens": completion.input_tokens,
                "output_tokens": completion.output_tokens,
                "problems": len(problems),
            }})
            if not problems:
                break
            log.warning("narrative failed validation",
                        extra={"fields": {"attempt": attempt, "problems": problems}})
            prompt = build_repair_prompt(user_prompt, completion.text, problems)
        else:
            raise NarrativeFailed(
                "The model's answer could not be grounded in the computed metrics, so no "
                "narrative was returned."
            )
        assert draft is not None

        narrative = self._assemble(insight, sheet, draft, NarrativeMeta(
            model=llm.model, prompt_version=PROMPT_VERSION,
            llm_called=True, cached=False, attempts=attempt,
            input_tokens=input_tokens, output_tokens=output_tokens,
            generated_at=datetime.now(UTC),
        ))
        await self._store.put_narrative(key, narrative.model_dump_json(), datetime.now(UTC))
        return narrative

    def _assemble(self, insight: ReviewFlowInsight, sheet: FactSheet, draft: Draft,
                  meta: NarrativeMeta) -> ReviewFlowNarrative:
        def evidence(ids: list[str]) -> list[Evidence]:
            unique = list(dict.fromkeys(ids))
            return [
                Evidence(fact_id=f.id, label=f.label, value=f.value, display=f.display,
                         source=f.source, interval=f.interval, significant=f.significant)
                for f in (sheet.facts[i] for i in unique)
            ]

        hypothesis = None
        if draft.hypothesis is not None:
            ceiling, basis = confidence_ceiling(insight)
            reported = draft.hypothesis.confidence
            hypothesis = Hypothesis(
                statement=render(draft.hypothesis.statement, sheet),
                confidence=Confidence(score=round(min(reported, ceiling), 2),
                                      model_reported=reported, ceiling=ceiling, basis=basis),
                evidence=evidence(draft.hypothesis.evidence),
                alternatives=[render(a, sheet) for a in draft.hypothesis.alternatives],
                how_to_verify=render(draft.hypothesis.how_to_verify, sheet),
            )
        return ReviewFlowNarrative(
            repository=insight.repository,
            period=insight.period,
            headline=render(draft.headline, sheet),
            narrative=render(draft.narrative, sheet),
            observations=[
                Observation(statement=render(o.statement, sheet), evidence=evidence(o.evidence))
                for o in draft.observations
            ],
            hypothesis=hypothesis,
            template=Template(headline=draft.headline, narrative=draft.narrative),
            references=_references(sheet, [text for _, text in draft.reader_texts()]),
            meta=meta,
        )

    def _insufficient(self, insight: ReviewFlowInsight, sheet: FactSheet) -> ReviewFlowNarrative:
        headline = "Too little review activity in this period to analyse."
        narrative = (
            "This period has {{summary.merged_prs.current}} merged PRs and "
            "{{summary.pr_reviews.current}} PR reviews, which is too little to say anything "
            "reliable. Widen the period or choose a busier repository."
        )
        return ReviewFlowNarrative(
            repository=insight.repository,
            period=insight.period,
            headline=headline,
            narrative=render(narrative, sheet),
            observations=[],
            hypothesis=None,
            template=Template(headline=headline, narrative=narrative),
            references=_references(sheet, [narrative]),
            meta=NarrativeMeta(
                model=None, prompt_version=PROMPT_VERSION, llm_called=False,
                cached=False, attempts=0, input_tokens=None, output_tokens=None,
                generated_at=datetime.now(UTC),
            ),
        )


def _references(sheet: FactSheet, texts: list[str]) -> dict[str, Reference]:
    refs: dict[str, Reference] = {}
    for text in texts:
        for ref in references_in(text):
            if ref in sheet.facts:
                fact = sheet.facts[ref]
                refs[ref] = Reference(display=fact.display, source=fact.source,
                                      label=fact.label, value=fact.value)
            else:
                entity = sheet.entities[ref]
                refs[ref] = Reference(display=entity.display, source=entity.source)
    return refs

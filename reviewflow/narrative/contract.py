"""The contract between the service and the model: output schema, validation, rendering.

The JSON schema is written by hand rather than generated so that it stays inside the
subset structured-output modes reliably support (closed objects, no numeric or length
constraints). Those constraints live in the Pydantic draft model and are checked after
decoding.
"""

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from reviewflow.narrative.facts import FactSheet

_STRINGS = {"type": "array", "items": {"type": "string"}}
_EVIDENCE = {**_STRINGS, "description": "Fact ids from the FACTS table that support this."}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["analysis", "headline", "narrative", "observations", "hypothesis"],
    "properties": {
        "analysis": {
            "type": "string",
            "description": "Your private reasoning: compare periods, segments and reviewers, "
                           "weigh candidate causes. Not shown to readers; may use numbers.",
        },
        "headline": {"type": "string", "description": "One sentence, at most fifteen words."},
        "narrative": {"type": "string", "description": "Three to five sentences."},
        "observations": {
            "type": "array",
            "description": "One to four things the data shows directly.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["statement", "evidence"],
                "properties": {"statement": {"type": "string"}, "evidence": _EVIDENCE},
            },
        },
        "hypothesis": {
            "anyOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["statement", "evidence", "confidence", "alternatives",
                                 "how_to_verify"],
                    "properties": {
                        "statement": {"type": "string",
                                      "description": "The most likely root cause."},
                        "evidence": _EVIDENCE,
                        "confidence": {"type": "number",
                                       "description": "Between zero and one."},
                        "alternatives": {**_STRINGS,
                                         "description": "Other plausible explanations."},
                        "how_to_verify": {"type": "string",
                                          "description": "What to check to confirm or refute it."},
                    },
                },
                {"type": "null"},
            ]
        },
    },
}


# The limits below are stated to the model in rule 10 of prompt.SYSTEM_PROMPT.
class DraftObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=1, max_length=400)
    evidence: list[str] = Field(min_length=1, max_length=8)


class DraftHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=1, max_length=500)
    evidence: list[str] = Field(min_length=2, max_length=10)
    confidence: float = Field(ge=0, le=1)
    alternatives: list[str] = Field(max_length=4)
    how_to_verify: str = Field(min_length=1, max_length=400)


class Draft(BaseModel):
    """What the model returns, before references are resolved."""

    model_config = ConfigDict(extra="forbid")
    analysis: str
    headline: str = Field(min_length=1, max_length=200)
    narrative: str = Field(min_length=1, max_length=1500)
    observations: list[DraftObservation] = Field(min_length=1, max_length=4)
    hypothesis: DraftHypothesis | None

    def reader_texts(self) -> list[tuple[str, str]]:
        """(location, text) for every field a reader sees; `analysis` is excluded."""
        texts = [("headline", self.headline), ("narrative", self.narrative)]
        texts += [(f"observations[{i}].statement", o.statement)
                  for i, o in enumerate(self.observations)]
        if self.hypothesis:
            texts.append(("hypothesis.statement", self.hypothesis.statement))
            texts.append(("hypothesis.how_to_verify", self.hypothesis.how_to_verify))
            texts += [(f"hypothesis.alternatives[{i}]", a)
                      for i, a in enumerate(self.hypothesis.alternatives)]
        return texts

    def evidence_lists(self) -> list[tuple[str, list[str]]]:
        lists = [(f"observations[{i}].evidence", o.evidence)
                 for i, o in enumerate(self.observations)]
        if self.hypothesis:
            lists.append(("hypothesis.evidence", self.hypothesis.evidence))
        return lists


REFERENCE = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")
_PERCENTILE_TOKEN = re.compile(r"\bp(?:50|90)\b", re.IGNORECASE)
_DIGIT = re.compile(r"\d")


def resolves(ref: str, sheet: FactSheet) -> bool:
    return ref in sheet.facts or ref in sheet.entities


def display(ref: str, sheet: FactSheet) -> str:
    if ref in sheet.facts:
        return sheet.facts[ref].display
    return sheet.entities[ref].display


def references_in(text: str) -> list[str]:
    return REFERENCE.findall(text)


def render(text: str, sheet: FactSheet) -> str:
    return REFERENCE.sub(lambda m: display(m.group(1), sheet), text)


def parse_and_validate(raw: str, sheet: FactSheet) -> tuple[Draft | None, list[str]]:
    """Decode the model's answer and check it against the fact sheet.

    Returns the draft and a list of problems phrased so they can be sent back to the
    model in a repair attempt. An empty list means every reference resolves and no
    number appears outside a reference.
    """
    try:
        draft = Draft.model_validate(json.loads(raw))
    except json.JSONDecodeError as exc:
        return None, [f"Output was not valid JSON ({exc.msg})."]
    except ValidationError as exc:
        return None, [
            f"{'.'.join(str(p) for p in err['loc']) or 'output'}: {err['msg']}"
            for err in exc.errors()
        ]
    errors: list[str] = []
    for where, text in draft.reader_texts():
        for ref in references_in(text):
            if not resolves(ref, sheet):
                errors.append(f"{where}: {{{{{ref}}}}} is not a known fact id, @login or #PR.")
        bare = _PERCENTILE_TOKEN.sub("", REFERENCE.sub("", text))
        if match := _DIGIT.search(bare):
            start = max(0, match.start() - 25)
            errors.append(
                f"{where}: contains a number written directly near "
                f"'{bare[start:match.start() + 25]}'. Use a {{{{fact_id}}}} reference or "
                f"spell small counts as words."
            )
    for where, ids in draft.evidence_lists():
        unknown = [i for i in ids if i not in sheet.facts]
        if unknown:
            errors.append(f"{where}: unknown fact ids {unknown}.")
    if draft.hypothesis and not any(
        sheet.facts[i].significant for i in draft.hypothesis.evidence if i in sheet.facts
    ):
        errors.append(
            "hypothesis.evidence: cites no significant change. A hypothesis must rest on at "
            "least one fact marked significant; if none supports a cause, return null."
        )
    return draft, errors

from datetime import datetime

from pydantic import BaseModel, Field

from reviewflow.schemas import Interval, Number, Period


class Evidence(BaseModel):
    fact_id: str
    label: str
    value: Number
    display: str
    source: str = Field(description="JSON pointer into the review-flow insight response")
    interval: Interval | None = Field(description="95% interval, for changes")
    significant: bool | None = Field(description="Whether the interval excludes zero")


class Observation(BaseModel):
    statement: str
    evidence: list[Evidence]


class Confidence(BaseModel):
    score: float = Field(description="min(model_reported, ceiling)")
    model_reported: float
    ceiling: float = Field(description="Upper bound set by sample size, computed by the service")
    basis: str


class Hypothesis(BaseModel):
    statement: str
    confidence: Confidence
    evidence: list[Evidence]
    alternatives: list[str]
    how_to_verify: str


class Reference(BaseModel):
    display: str
    source: str
    label: str | None = None
    value: Number | None = None


class Template(BaseModel):
    """Headline and narrative with unresolved references, for clients that link numbers
    back to their facts (the bundled UI highlights them)."""

    headline: str
    narrative: str


class NarrativeMeta(BaseModel):
    model: str | None
    prompt_version: str
    llm_called: bool
    cached: bool
    attempts: int
    input_tokens: int | None
    output_tokens: int | None
    generated_at: datetime


class ReviewFlowNarrative(BaseModel):
    repository: str
    period: Period
    headline: str
    narrative: str
    observations: list[Observation]
    hypothesis: Hypothesis | None
    template: Template
    references: dict[str, Reference]
    meta: NarrativeMeta

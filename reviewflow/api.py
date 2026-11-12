import asyncio
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from reviewflow.config import Settings
from reviewflow.errors import InvalidRequest, RepositoryNotAllowed
from reviewflow.metrics import compute_review_flow
from reviewflow.models import RepoRef
from reviewflow.narrative.models import ReviewFlowNarrative
from reviewflow.narrative.service import NarrativeService
from reviewflow.period import Window, resolve_window
from reviewflow.schemas import ReviewFlowInsight
from reviewflow.store import Store
from reviewflow.sync import SyncCoordinator


@dataclass(frozen=True, slots=True)
class Services:
    settings: Settings
    store: Store
    sync: SyncCoordinator
    narrative: NarrativeService


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


class Problem(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    instance: str
    request_id: str | None = None


# GitHub's own naming rules. Validated before anything leaves the service.
Owner = Annotated[str, Path(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$",
                            description="Repository owner (user or organisation)")]
RepoName = Annotated[str, Path(pattern=r"^[A-Za-z0-9._-]{1,100}$",
                               description="Repository name")]
Since = Annotated[datetime | None, Query(
    description="Period start, ISO 8601 date or datetime (UTC if no offset). "
                "Defaults to DEFAULT_WINDOW_DAYS before `until`.")]
Until = Annotated[datetime | None, Query(
    description="Period end (exclusive), ISO 8601. Defaults to now; future times are clamped.")]

_ERRORS: dict[int | str, dict[str, object]] = {
    403: {"model": Problem, "description": "Repository not in ALLOWED_REPOS."},
    404: {"model": Problem, "description": "Repository not found."},
    422: {"model": Problem, "description": "Invalid parameters."},
    502: {"model": Problem, "description": "Upstream (GitHub or LLM provider) error."},
    503: {"model": Problem, "description": "Upstream rate limited, or no LLM configured."},
}

router = APIRouter(prefix="/v1", tags=["insights"])


def _repo(owner: str, name: str, settings: Settings) -> RepoRef:
    if name in {".", ".."}:
        raise InvalidRequest("Invalid repository name.")
    ref = RepoRef(owner, name)
    if settings.allowed_repos and ref.key not in settings.allowed_repos:
        raise RepositoryNotAllowed(f"'{ref.display}' is not in this service's allow-list.")
    return ref


def _window(since: datetime | None, until: datetime | None, settings: Settings) -> Window:
    return resolve_window(since, until, now=datetime.now(UTC),
                          default_days=settings.default_window_days,
                          max_days=settings.max_window_days,
                          max_lookback_days=settings.max_lookback_days)


async def _insight(services: Services, ref: RepoRef, window: Window,
                   top: int) -> ReviewFlowInsight:
    # The baseline period is compared against, so it must be covered too. The first query
    # for a repository waits here while its history is fetched.
    data_as_of = await services.sync.ensure_covered(ref, window.baseline.since, window.until)
    prs = await services.store.load_scope(ref.key, window.baseline.since, window.until)
    # CPU-bound over potentially thousands of PRs: keep it off the event loop.
    return await asyncio.to_thread(
        compute_review_flow, ref.display, prs, window, data_as_of=data_as_of, top_n=top,
    )


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    candidates = {tag.strip().removeprefix("W/") for tag in header.split(",")}
    return "*" in candidates or etag in candidates


@router.get(
    "/repos/{owner}/{repo}/insights/review-flow",
    response_model=ReviewFlowInsight,
    responses=_ERRORS,
    summary="Review flow: who reviews, how long PRs wait, and what changed",
)
async def review_flow(
    request: Request,
    services: ServicesDep,
    owner: Owner,
    repo: RepoName,
    since: Since = None,
    until: Until = None,
    top: Annotated[int, Query(ge=1, le=50, description="Reviewers to list per period")] = 10,
) -> Response:
    ref = _repo(owner, repo, services.settings)
    insight = await _insight(services, ref, _window(since, until, services.settings), top)
    body = insight.model_dump_json().encode()
    etag = f'"{hashlib.sha256(body).hexdigest()[:32]}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


@router.get(
    "/repos/{owner}/{repo}/insights/review-flow/narrative",
    response_model=ReviewFlowNarrative,
    responses=_ERRORS,
    summary="LLM narrative over the review-flow metrics, with hypothesis and evidence",
)
async def review_flow_narrative(
    services: ServicesDep,
    owner: Owner,
    repo: RepoName,
    since: Since = None,
    until: Until = None,
) -> Response:
    ref = _repo(owner, repo, services.settings)
    window = _window(since, until, services.settings)
    services.narrative.require_llm()  # before the sync spends GitHub budget
    insight = await _insight(services, ref, window, top=10)
    narrative = await services.narrative.narrate(insight)
    return Response(narrative.model_dump_json(), media_type="application/json")


health = APIRouter(tags=["ops"])


@health.get("/healthz", summary="Liveness")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@health.get("/readyz", summary="Readiness: storage reachable")
async def readyz(services: ServicesDep) -> dict[str, str]:
    await services.store.ping()
    return {"status": "ready"}

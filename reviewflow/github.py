"""GitHub GraphQL client: pull requests with their reviews, newest-updated first.

GraphQL instead of REST because REST needs one call per PR to list its reviews (N+1);
here a page of 50 PRs, their reviews and their ready-for-review events is one request.
Owner and repository names travel only as GraphQL variables, never interpolated into
query text, and the endpoint is a constant, so callers cannot redirect requests.
"""

import asyncio
import logging
import random
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import SecretStr

from reviewflow.errors import RepositoryNotFound, UpstreamError, UpstreamRateLimited
from reviewflow.models import PullRequest, RepoRef, Review, ReviewState

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.github.com/graphql"
_REVIEWS_PER_PR = 25  # covers almost every PR; the rest are fetched by _remaining_reviews

_REVIEW_FIELDS = """
  pageInfo { hasNextPage endCursor }
  nodes {
    id state submittedAt
    author { __typename login }
    comments { totalCount }
  }
"""

PULL_REQUESTS_QUERY = f"""
query PullRequests($owner: String!, $name: String!, $first: Int!, $after: String) {{
  rateLimit {{ cost remaining resetAt }}
  repository(owner: $owner, name: $name) {{
    pullRequests(first: $first, after: $after,
                 orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        id number url createdAt updatedAt mergedAt additions deletions
        authorAssociation
        author {{ __typename login }}
        draftEvents: timelineItems(itemTypes: [READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT],
                                   first: 1) {{
          nodes {{ __typename ... on ReadyForReviewEvent {{ createdAt }} }}
        }}
        reviews(first: {_REVIEWS_PER_PR}) {{ {_REVIEW_FIELDS} }}
      }}
    }}
  }}
}}
"""

REVIEWS_QUERY = f"""
query PullRequestReviews($id: ID!, $after: String) {{
  rateLimit {{ cost remaining resetAt }}
  node(id: $id) {{
    ... on PullRequest {{ reviews(first: 100, after: $after) {{ {_REVIEW_FIELDS} }} }}
  }}
}}
"""

_RETRYABLE_STATUS = frozenset({502, 503, 504})


@dataclass(frozen=True, slots=True)
class PullRequestPage:
    pull_requests: list[PullRequest]
    oldest_updated_at: datetime | None


def create_http_client(token: SecretStr, timeout_seconds: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "Authorization": f"Bearer {token.get_secret_value()}",
            "User-Agent": "reviewflow/0.1",
            "Accept": "application/vnd.github+json",
        },
        timeout=httpx.Timeout(timeout_seconds, connect=5.0),
    )


class GitHubClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        page_size: int = 50,
        min_rate_budget: int = 100,
        max_attempts: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = http
        self._page_size = page_size
        self._min_rate_budget = min_rate_budget
        self._max_attempts = max_attempts
        self._sleep = sleep
        # Set when the token's remaining budget drops below the floor; the budget is shared
        # by every sync using this token, so it is tracked on the client, not per call.
        self._budget_resets_at: datetime | None = None

    async def pull_request_pages(
        self, repo: RepoRef
    ) -> AsyncGenerator[PullRequestPage, None]:
        """Yield pages of PRs ordered by `updatedAt` descending. The caller decides when to
        stop; pagination is lazy so an incremental sync costs only the pages it reads."""
        cursor: str | None = None
        while True:
            data = await self._graphql(
                PULL_REQUESTS_QUERY,
                {"owner": repo.owner, "name": repo.name, "first": self._page_size,
                 "after": cursor},
            )
            repository = data.get("repository")
            if repository is None:
                raise RepositoryNotFound(f"Repository '{repo.display}' was not found.")
            connection = repository["pullRequests"]
            prs: list[PullRequest] = []
            updated: list[datetime] = []
            for node in connection["nodes"]:
                if node is None:
                    continue
                prs.append(await self._parse_pull_request(node))
                updated.append(_parse_time(node["updatedAt"]))
            page_info = connection["pageInfo"]
            yield PullRequestPage(prs, min(updated, default=None))
            if not page_info["hasNextPage"]:
                return
            cursor = page_info["endCursor"]

    async def _parse_pull_request(self, node: dict[str, Any]) -> PullRequest:
        author, author_is_bot = _parse_actor(node["author"])
        reviews_conn = node["reviews"]
        reviews = _parse_reviews(reviews_conn["nodes"])
        if reviews_conn["pageInfo"]["hasNextPage"]:
            reviews += await self._remaining_reviews(
                node["id"], reviews_conn["pageInfo"]["endCursor"]
            )
        created_at = _parse_time(node["createdAt"])
        # The first draft transition tells how the PR was opened: a PR opened as a draft
        # first becomes ready; one opened ready (and later converted) was reviewable from
        # creation, so a later ready-for-review event must not restart its clock.
        first = next((n for n in node["draftEvents"]["nodes"] if n), None)
        ready_at = created_at
        if first is not None and first["__typename"] == "ReadyForReviewEvent":
            ready_at = _parse_time(first["createdAt"])
        return PullRequest(
            number=node["number"],
            url=node["url"],
            author=author,
            author_is_bot=author_is_bot,
            author_association=node["authorAssociation"],
            created_at=created_at,
            ready_at=ready_at,
            merged_at=_parse_time(node["mergedAt"]) if node["mergedAt"] else None,
            additions=node["additions"],
            deletions=node["deletions"],
            reviews=tuple(reviews),
        )

    async def _remaining_reviews(self, pr_id: str, cursor: str | None) -> list[Review]:
        reviews: list[Review] = []
        while True:
            data = await self._graphql(REVIEWS_QUERY, {"id": pr_id, "after": cursor})
            connection = (data.get("node") or {}).get("reviews")
            if connection is None:
                return reviews
            reviews += _parse_reviews(connection["nodes"])
            if not connection["pageInfo"]["hasNextPage"]:
                return reviews
            cursor = connection["pageInfo"]["endCursor"]

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self._check_budget()
        for attempt in range(1, self._max_attempts + 1):
            started = time.perf_counter()
            try:
                response = await self._http.post(
                    GRAPHQL_URL, json={"query": query, "variables": variables}
                )
            except httpx.TransportError as exc:
                if attempt == self._max_attempts:
                    raise UpstreamError("GitHub could not be reached.") from exc
                log.warning("github transport error, retrying",
                            extra={"fields": {"attempt": attempt, "error": type(exc).__name__}})
                await self._backoff(attempt)
                continue
            if response.status_code in _RETRYABLE_STATUS and attempt < self._max_attempts:
                log.warning("github returned retryable status",
                            extra={"fields": {"attempt": attempt, "status": response.status_code}})
                await self._backoff(attempt)
                continue
            data = self._unwrap(response)
            rate = data.get("rateLimit") or {}
            log.info("github graphql call", extra={"fields": {
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "cost": rate.get("cost"), "remaining": rate.get("remaining"),
            }})
            self._record_budget(rate)
            return data
        raise AssertionError("unreachable")  # pragma: no cover

    def _unwrap(self, response: httpx.Response) -> dict[str, Any]:
        status = response.status_code
        if status == 401:
            log.error("github rejected the configured token")
            raise UpstreamError("GitHub rejected the service's credentials.")
        if status in (403, 429):
            retry_after = _retry_after_seconds(response)
            if retry_after is not None or status == 429:
                raise UpstreamRateLimited(
                    "GitHub rate limit reached; retry later.", retry_after=retry_after or 60
                )
            raise UpstreamError("GitHub denied the request.")
        if status != 200:
            log.error("github unexpected status", extra={"fields": {"status": status}})
            raise UpstreamError(f"GitHub returned HTTP {status}.")
        payload: dict[str, Any] = response.json()
        errors = payload.get("errors") or []
        if errors:
            types = {str(e.get("type")) for e in errors}
            log.warning("github graphql errors", extra={"fields": {"types": sorted(types)}})
            if "NOT_FOUND" in types:
                raise RepositoryNotFound("Repository was not found.")
            if types & {"RATE_LIMITED", "RATE_LIMIT"}:
                raise UpstreamRateLimited("GitHub rate limit reached; retry later.",
                                          retry_after=60)
            raise UpstreamError("GitHub returned an error for the query.")
        data: dict[str, Any] = payload["data"]
        return data

    def _check_budget(self) -> None:
        if self._budget_resets_at is None:
            return
        remaining = (self._budget_resets_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            self._budget_resets_at = None
            return
        raise UpstreamRateLimited(
            "GitHub API budget reserved; retry after the rate limit window resets.",
            retry_after=max(1, int(remaining)),
        )

    def _record_budget(self, rate: dict[str, Any]) -> None:
        remaining, resets_at = rate.get("remaining"), rate.get("resetAt")
        if isinstance(remaining, int) and resets_at and remaining < self._min_rate_budget:
            self._budget_resets_at = _parse_time(resets_at)
            log.warning("github rate budget below floor",
                        extra={"fields": {"remaining": remaining, "resets_at": resets_at}})

    async def _backoff(self, attempt: int) -> None:
        await self._sleep(0.5 * 2 ** (attempt - 1) + random.uniform(0, 0.25))  # noqa: S311


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _parse_actor(actor: dict[str, Any] | None) -> tuple[str, bool]:
    if actor is None:  # deleted account; GitHub shows these as "ghost"
        return "ghost", False
    return str(actor["login"]), actor.get("__typename") == "Bot"


def _parse_reviews(nodes: list[dict[str, Any] | None]) -> list[Review]:
    reviews: list[Review] = []
    for node in nodes:
        # Pending reviews are drafts visible only to their author; they have no submittedAt.
        if node is None or node["submittedAt"] is None or node["state"] == "PENDING":
            continue
        reviewer, is_bot = _parse_actor(node["author"])
        reviews.append(Review(
            id=node["id"],
            reviewer=reviewer,
            reviewer_is_bot=is_bot,
            state=ReviewState(node["state"]),
            submitted_at=_parse_time(node["submittedAt"]),
            inline_comments=int(node["comments"]["totalCount"]),
        ))
    return reviews


def _retry_after_seconds(response: httpx.Response) -> int | None:
    if (value := response.headers.get("retry-after")) and value.isdigit():
        return int(value)
    if response.headers.get("x-ratelimit-remaining") == "0":
        reset = response.headers.get("x-ratelimit-reset", "")
        if reset.isdigit():
            return max(1, int(reset) - int(time.time()))
        return 60
    return None

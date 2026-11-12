"""Builders for domain records and a fake GitHub GraphQL endpoint."""

import itertools
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from reviewflow.models import PullRequest, Review, ReviewState

T0 = datetime(2026, 10, 1, tzinfo=UTC)
_ids = itertools.count(1)


def at(days: float = 0, hours: float = 0) -> datetime:
    return T0 + timedelta(days=days, hours=hours)


def review(
    reviewer: str,
    when: datetime,
    state: ReviewState = ReviewState.APPROVED,
    *,
    bot: bool = False,
    comments: int = 0,
) -> Review:
    return Review(f"R{next(_ids)}", reviewer, bot, state, when, comments)


def pr(
    number: int,
    *,
    ready: datetime,
    merged: datetime | None = None,
    author: str = "alice",
    created: datetime | None = None,
    reviews: tuple[Review, ...] = (),
    additions: int = 10,
    deletions: int = 0,
    association: str = "MEMBER",
    bot: bool = False,
) -> PullRequest:
    return PullRequest(
        number=number,
        url=f"https://github.com/acme/widgets/pull/{number}",
        author=author,
        author_is_bot=bot,
        author_association=association,
        created_at=created or ready,
        ready_at=ready,
        merged_at=merged,
        additions=additions,
        deletions=deletions,
        reviews=reviews,
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _actor(login: str, is_bot: bool) -> dict[str, str] | None:
    if login == "ghost":
        return None
    return {"__typename": "Bot" if is_bot else "User", "login": login}


def _review_node(r: Review) -> dict[str, Any]:
    return {
        "id": r.id,
        "state": r.state.value,
        "submittedAt": _iso(r.submitted_at),
        "author": _actor(r.reviewer, r.reviewer_is_bot),
        "comments": {"totalCount": r.inline_comments},
    }


def updated_at(p: PullRequest) -> datetime:
    """A PR's updatedAt is never earlier than its last merge or review."""
    times = [p.created_at, p.ready_at, *(r.submitted_at for r in p.reviews)]
    if p.merged_at:
        times.append(p.merged_at)
    return max(times)


class FakeGitHub:
    """Serves the two GraphQL queries the client sends, from an in-memory list of PRs.

    Pages honour `first`/`after`, PRs are ordered by updatedAt descending, and only the
    first 25 reviews are inlined, like the real query, so overflow paging is exercised.
    """

    def __init__(self, prs: list[PullRequest], *, remaining: int = 4999) -> None:
        self.prs = sorted(prs, key=lambda p: (updated_at(p), p.number), reverse=True)
        self.remaining = remaining
        self.pr_page_calls = 0
        self.review_calls = 0
        self.repo_exists = True

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        variables = body["variables"]
        rate = {"cost": 1, "remaining": self.remaining, "resetAt": _iso(T0 + timedelta(days=60))}
        if "query PullRequests(" in body["query"]:
            self.pr_page_calls += 1
            if not self.repo_exists:
                return httpx.Response(200, json={
                    "data": {"rateLimit": rate, "repository": None},
                    "errors": [{"type": "NOT_FOUND", "message": "Could not resolve"}],
                })
            start = int(variables["after"] or 0)
            page = self.prs[start:start + variables["first"]]
            end = start + len(page)
            return httpx.Response(200, json={"data": {"rateLimit": rate, "repository": {
                "pullRequests": {
                    "pageInfo": {"hasNextPage": end < len(self.prs), "endCursor": str(end)},
                    "nodes": [self._pr_node(p) for p in page],
                }}}})
        self.review_calls += 1
        target = next(p for p in self.prs if f"PR_{p.number}" == variables["id"])
        start = int(variables["after"])
        chunk = target.reviews[start:start + 100]
        end = start + len(chunk)
        return httpx.Response(200, json={"data": {"rateLimit": rate, "node": {"reviews": {
            "pageInfo": {"hasNextPage": end < len(target.reviews), "endCursor": str(end)},
            "nodes": [_review_node(r) for r in chunk],
        }}}})

    @staticmethod
    def _pr_node(p: PullRequest) -> dict[str, Any]:
        inline = p.reviews[:25]
        return {
            "id": f"PR_{p.number}",
            "number": p.number,
            "url": p.url,
            "createdAt": _iso(p.created_at),
            "updatedAt": _iso(updated_at(p)),
            "mergedAt": _iso(p.merged_at) if p.merged_at else None,
            "additions": p.additions,
            "deletions": p.deletions,
            "authorAssociation": p.author_association,
            "author": _actor(p.author, p.author_is_bot),
            "draftEvents": {"nodes": [{"__typename": "ReadyForReviewEvent",
                                       "createdAt": _iso(p.ready_at)}]
                            if p.ready_at != p.created_at else []},
            "reviews": {
                "pageInfo": {"hasNextPage": len(p.reviews) > 25, "endCursor": str(len(inline))},
                "nodes": [_review_node(r) for r in inline],
            },
        }

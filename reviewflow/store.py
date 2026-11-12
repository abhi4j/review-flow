"""SQLite persistence for fetched GitHub data, sync coverage and narrative cache.

Timestamps are stored as integer epoch seconds (UTC) so range predicates use indexes and
compare numerically. The schema is plain SQL and portable to Postgres; SQLite is used
because this is a single-node service and it keeps the quickstart dependency-free.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

import aiosqlite

from reviewflow.models import PullRequest, Review, ReviewState

_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pull_requests (
    repo               TEXT    NOT NULL,
    number             INTEGER NOT NULL,
    url                TEXT    NOT NULL,
    author             TEXT    NOT NULL,
    author_is_bot      INTEGER NOT NULL,
    author_association TEXT    NOT NULL,
    created_at         INTEGER NOT NULL,
    ready_at           INTEGER NOT NULL,
    merged_at          INTEGER,
    additions          INTEGER NOT NULL,
    deletions          INTEGER NOT NULL,
    PRIMARY KEY (repo, number)
);
CREATE INDEX IF NOT EXISTS ix_pr_merged ON pull_requests (repo, merged_at);

CREATE TABLE IF NOT EXISTS reviews (
    id               TEXT    PRIMARY KEY,
    repo             TEXT    NOT NULL,
    pr_number        INTEGER NOT NULL,
    reviewer         TEXT    NOT NULL,
    reviewer_is_bot  INTEGER NOT NULL,
    state            TEXT    NOT NULL,
    submitted_at     INTEGER NOT NULL,
    inline_comments  INTEGER NOT NULL,
    FOREIGN KEY (repo, pr_number) REFERENCES pull_requests (repo, number) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_review_pr ON reviews (repo, pr_number);
CREATE INDEX IF NOT EXISTS ix_review_submitted ON reviews (repo, submitted_at);

-- The contiguous range of PR `updatedAt` values fully ingested for a repository.
CREATE TABLE IF NOT EXISTS sync_coverage (
    repo          TEXT    PRIMARY KEY,
    covered_from  INTEGER NOT NULL,
    covered_to    INTEGER NOT NULL
);

-- Content-addressed: the key hashes every input, so entries never need invalidation.
CREATE TABLE IF NOT EXISTS narrative_cache (
    cache_key   TEXT    PRIMARY KEY,
    payload     TEXT    NOT NULL,
    created_at  INTEGER NOT NULL
);
"""

# "scope" = PRs relevant to [start, end): merged in it, or reviewed in it.
_SCOPE_REVIEWS_SQL = """
WITH scope AS (
    SELECT p.number FROM pull_requests p
    WHERE p.repo = :repo AND p.merged_at >= :start AND p.merged_at < :end
    UNION
    SELECT r.pr_number FROM reviews r
    WHERE r.repo = :repo AND r.submitted_at >= :start AND r.submitted_at < :end
)
SELECT r.* FROM reviews r JOIN scope s ON r.pr_number = s.number
WHERE r.repo = :repo ORDER BY r.submitted_at
"""

_SCOPE_PULL_REQUESTS_SQL = """
WITH scope AS (
    SELECT p.number FROM pull_requests p
    WHERE p.repo = :repo AND p.merged_at >= :start AND p.merged_at < :end
    UNION
    SELECT r.pr_number FROM reviews r
    WHERE r.repo = :repo AND r.submitted_at >= :start AND r.submitted_at < :end
)
SELECT p.* FROM pull_requests p JOIN scope s ON p.number = s.number
WHERE p.repo = :repo ORDER BY p.number
"""


def _ts(value: datetime) -> int:
    return int(value.timestamp())


def _dt(value: int) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)


@dataclass(frozen=True, slots=True)
class Coverage:
    covered_from: datetime
    covered_to: datetime


class Store:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    @classmethod
    async def open(cls, path: Path) -> Self:
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(_SCHEMA)
        await db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        await db.commit()
        return cls(db)

    async def close(self) -> None:
        await self._db.close()

    async def ping(self) -> None:
        await self._db.execute("SELECT 1")

    # --- sync coverage -------------------------------------------------------------------

    async def get_coverage(self, repo: str) -> Coverage | None:
        async with self._db.execute(
            "SELECT covered_from, covered_to FROM sync_coverage WHERE repo = ?", (repo,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return Coverage(_dt(row["covered_from"]), _dt(row["covered_to"]))

    async def set_coverage(self, repo: str, coverage: Coverage) -> None:
        await self._db.execute(
            """INSERT INTO sync_coverage (repo, covered_from, covered_to) VALUES (?, ?, ?)
               ON CONFLICT (repo) DO UPDATE SET
                 covered_from = excluded.covered_from, covered_to = excluded.covered_to""",
            (repo, _ts(coverage.covered_from), _ts(coverage.covered_to)),
        )
        await self._db.commit()

    # --- pull requests -------------------------------------------------------------------

    async def upsert_pull_requests(self, repo: str, prs: Sequence[PullRequest]) -> None:
        """Insert or refresh PRs. A PR's reviews are replaced wholesale, so dismissed or
        deleted reviews disappear instead of lingering from an earlier fetch."""
        if not prs:
            return
        await self._db.executemany(
            """INSERT INTO pull_requests (repo, number, url, author, author_is_bot,
                   author_association, created_at, ready_at, merged_at, additions, deletions)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (repo, number) DO UPDATE SET
                 url = excluded.url, author = excluded.author,
                 author_is_bot = excluded.author_is_bot,
                 author_association = excluded.author_association,
                 created_at = excluded.created_at, ready_at = excluded.ready_at,
                 merged_at = excluded.merged_at, additions = excluded.additions,
                 deletions = excluded.deletions""",
            [
                (
                    repo, pr.number, pr.url, pr.author, pr.author_is_bot,
                    pr.author_association, _ts(pr.created_at), _ts(pr.ready_at),
                    _ts(pr.merged_at) if pr.merged_at else None, pr.additions, pr.deletions,
                )
                for pr in prs
            ],
        )
        await self._db.executemany(
            "DELETE FROM reviews WHERE repo = ? AND pr_number = ?",
            [(repo, pr.number) for pr in prs],
        )
        await self._db.executemany(
            """INSERT INTO reviews (id, repo, pr_number, reviewer, reviewer_is_bot, state,
                   submitted_at, inline_comments)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
                 repo = excluded.repo, pr_number = excluded.pr_number,
                 reviewer = excluded.reviewer, reviewer_is_bot = excluded.reviewer_is_bot,
                 state = excluded.state, submitted_at = excluded.submitted_at,
                 inline_comments = excluded.inline_comments""",
            [
                (
                    r.id, repo, pr.number, r.reviewer, r.reviewer_is_bot, r.state.value,
                    _ts(r.submitted_at), r.inline_comments,
                )
                for pr in prs
                for r in pr.reviews
            ],
        )
        await self._db.commit()

    async def load_scope(self, repo: str, start: datetime, end: datetime) -> list[PullRequest]:
        """Every PR merged or reviewed in [start, end), with *all* of its reviews.

        All reviews are needed (not only those inside the range) because time-to-first-review
        for a PR merged in the range can depend on a review submitted before it.
        """
        params = {"repo": repo, "start": _ts(start), "end": _ts(end)}
        async with self._db.execute(_SCOPE_REVIEWS_SQL, params) as cur:
            review_rows = await cur.fetchall()
        reviews_by_pr: dict[int, list[Review]] = {}
        for row in review_rows:
            reviews_by_pr.setdefault(row["pr_number"], []).append(
                Review(
                    id=row["id"],
                    reviewer=row["reviewer"],
                    reviewer_is_bot=bool(row["reviewer_is_bot"]),
                    state=ReviewState(row["state"]),
                    submitted_at=_dt(row["submitted_at"]),
                    inline_comments=row["inline_comments"],
                )
            )
        async with self._db.execute(_SCOPE_PULL_REQUESTS_SQL, params) as cur:
            pr_rows = await cur.fetchall()
        return [_row_to_pr(row, reviews_by_pr.get(row["number"], ())) for row in pr_rows]

    # --- narrative cache -----------------------------------------------------------------

    async def get_narrative(self, cache_key: str) -> str | None:
        async with self._db.execute(
            "SELECT payload FROM narrative_cache WHERE cache_key = ?", (cache_key,)
        ) as cur:
            row = await cur.fetchone()
        return None if row is None else str(row["payload"])

    async def put_narrative(self, cache_key: str, payload: str, now: datetime) -> None:
        await self._db.execute(
            """INSERT INTO narrative_cache (cache_key, payload, created_at) VALUES (?, ?, ?)
               ON CONFLICT (cache_key) DO NOTHING""",
            (cache_key, payload, _ts(now)),
        )
        await self._db.commit()


def _row_to_pr(row: aiosqlite.Row, reviews: Iterable[Review]) -> PullRequest:
    return PullRequest(
        number=row["number"],
        url=row["url"],
        author=row["author"],
        author_is_bot=bool(row["author_is_bot"]),
        author_association=row["author_association"],
        created_at=_dt(row["created_at"]),
        ready_at=_dt(row["ready_at"]),
        merged_at=_dt(row["merged_at"]) if row["merged_at"] is not None else None,
        additions=row["additions"],
        deletions=row["deletions"],
        reviews=tuple(reviews),
    )

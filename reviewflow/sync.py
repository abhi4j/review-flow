"""Incremental ingestion with a coverage watermark, one sync at a time per repository.

Invariant: `sync_coverage` = [covered_from, covered_to] means every PR whose `updatedAt`
falls in that range is stored with its current reviews. Because a PR's `updatedAt` is at
least as late as its merge and every review on it, PRs updated since `need_from` are
exactly the ones that can contribute to metrics over a window starting at `need_from`.

PRs are paged newest-updated first, so:
  * extending coverage to now only reads pages until we reach the old `covered_to`;
  * extending it further into the past re-reads recent pages (GraphQL cannot seek by date),
    which is idempotent and costs pages, not correctness.
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from reviewflow.github import GitHubClient
from reviewflow.models import RepoRef
from reviewflow.store import Coverage, Store

log = logging.getLogger(__name__)

# Overlap re-read on incremental syncs, so clock skew between this host and GitHub cannot
# open a gap between the old watermark and the new pages.
CLOCK_SKEW_MARGIN = timedelta(minutes=5)


def needs_sync(
    coverage: Coverage | None,
    need_from: datetime,
    need_to: datetime,
    now: datetime,
    ttl: timedelta,
) -> bool:
    if coverage is None or coverage.covered_from > need_from:
        return True
    return coverage.covered_to < need_to and now - coverage.covered_to > ttl


class SyncCoordinator:
    """Brings stored data up to date before a query reads it.

    The request waits for the sync. A lock per repository means concurrent requests run one
    sync between them: whoever gets the lock next re-reads coverage and finds it fresh.
    """

    def __init__(self, store: Store, github: GitHubClient, *, ttl: timedelta) -> None:
        self._store = store
        self._github = github
        self._ttl = ttl
        self._locks: dict[str, asyncio.Lock] = {}

    async def ensure_covered(
        self, repo: RepoRef, need_from: datetime, need_to: datetime
    ) -> datetime:
        """Sync if needed; return the time up to which stored data is complete."""
        async with self._locks.setdefault(repo.key, asyncio.Lock()):
            coverage = await self._store.get_coverage(repo.key)
            if needs_sync(coverage, need_from, need_to, datetime.now(UTC), self._ttl):
                try:
                    coverage = await self._sync(repo, coverage, need_from)
                except Exception as exc:
                    log.warning("sync failed",
                                extra={"fields": {"repo": repo.key, "error": repr(exc)}})
                    raise
            assert coverage is not None
            return coverage.covered_to

    async def _sync(
        self, repo: RepoRef, coverage: Coverage | None, need_from: datetime
    ) -> Coverage:
        # Whole seconds, matching storage, so a fresh sync and a later read of it report the
        # same data_as_of. Rounding down only makes the covered range more conservative.
        started_at = datetime.now(UTC).replace(microsecond=0)
        if coverage is not None and coverage.covered_from <= need_from:
            stop_at, covered_from = coverage.covered_to - CLOCK_SKEW_MARGIN, coverage.covered_from
        else:
            stop_at, covered_from = need_from, need_from
        log.info("sync started", extra={"fields": {
            "repo": repo.key, "stop_at": stop_at.isoformat(), "incremental": coverage is not None,
        }})
        pages = pull_requests = 0
        async with contextlib.aclosing(self._github.pull_request_pages(repo)) as page_iter:
            async for page in page_iter:
                await self._store.upsert_pull_requests(repo.key, page.pull_requests)
                pages += 1
                pull_requests += len(page.pull_requests)
                if page.oldest_updated_at is None or page.oldest_updated_at < stop_at:
                    break
        # The new upper bound is when this sync *started*: a PR updated mid-sync can move
        # ahead of the cursor, but its updatedAt is then later than this bound and the next
        # incremental sync re-reads it.
        updated = Coverage(covered_from, started_at)
        await self._store.set_coverage(repo.key, updated)
        log.info("sync finished", extra={"fields": {
            "repo": repo.key, "pages": pages, "pull_requests": pull_requests,
        }})
        return updated

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from reviewflow.github import PullRequestPage
from reviewflow.models import RepoRef
from reviewflow.store import Coverage, Store
from reviewflow.sync import SyncCoordinator, needs_sync
from tests.factories import at, pr

REPO = RepoRef("acme", "widgets")
NOW = datetime.now(UTC).replace(microsecond=0)  # storage has one-second resolution


class FakeClient:
    """Pages of PRs whose updatedAt walks back one day per page from now."""

    def __init__(self, pages: int = 30, gate: asyncio.Event | None = None) -> None:
        self.total_pages = pages
        self.pages_served = 0
        self.syncs = 0
        self.gate = gate

    async def pull_request_pages(self, repo: RepoRef):
        self.syncs += 1
        for i in range(self.total_pages):
            if self.gate is not None:
                await self.gate.wait()
            self.pages_served += 1
            updated = NOW - timedelta(days=i)
            yield PullRequestPage([pr(i + 1, ready=updated - timedelta(hours=1), merged=updated)],
                                  updated)


@pytest.fixture
async def store():
    s = await Store.open(":memory:")
    yield s
    await s.close()


def _coordinator(store, client, ttl=timedelta(minutes=10)) -> SyncCoordinator:
    return SyncCoordinator(store, client, ttl=ttl)


def test_needs_sync_rules():
    ttl = timedelta(minutes=10)
    cov = Coverage(at(0), at(10))
    assert needs_sync(None, at(1), at(2), at(10), ttl)
    assert needs_sync(cov, at(-1), at(2), at(10), ttl)  # wants older data
    assert not needs_sync(cov, at(1), at(9), at(20), ttl)  # window fully in the past
    assert not needs_sync(cov, at(1), at(11), at(10, 0.1), ttl)  # stale but within TTL
    assert needs_sync(cov, at(1), at(11), at(11), ttl)  # stale beyond TTL


async def test_first_sync_reads_only_back_to_the_needed_start(store):
    client = FakeClient()
    data_as_of = await _coordinator(store, client).ensure_covered(
        REPO, NOW - timedelta(days=5, hours=12), NOW)
    assert data_as_of >= NOW
    assert client.pages_served == 7  # days 0..6; day 6 is older than the start, so stop
    coverage = await store.get_coverage(REPO.key)
    assert coverage.covered_from == NOW - timedelta(days=5, hours=12)


async def test_covered_request_within_ttl_does_not_call_github(store):
    client = FakeClient()
    coordinator = _coordinator(store, client)
    await coordinator.ensure_covered(REPO, NOW - timedelta(days=3), NOW)
    await coordinator.ensure_covered(REPO, NOW - timedelta(days=2), NOW)
    assert client.syncs == 1


async def test_incremental_sync_stops_at_previous_watermark(store):
    await store.set_coverage(REPO.key, Coverage(NOW - timedelta(days=30), NOW - timedelta(days=2)))
    client = FakeClient()
    await _coordinator(store, client).ensure_covered(REPO, NOW - timedelta(days=10), NOW)
    # Walks back past (watermark - 5 min skew margin): days 0-2 are newer, day 3 is older.
    assert client.pages_served == 4
    coverage = await store.get_coverage(REPO.key)
    assert coverage.covered_from == NOW - timedelta(days=30)


async def test_extending_into_the_past_rewalks_to_the_older_start(store):
    await store.set_coverage(REPO.key, Coverage(NOW - timedelta(days=3), NOW))
    client = FakeClient()
    await _coordinator(store, client).ensure_covered(REPO, NOW - timedelta(days=8, hours=1), NOW)
    assert client.pages_served == 10
    assert (await store.get_coverage(REPO.key)).covered_from == NOW - timedelta(days=8, hours=1)


async def test_concurrent_requests_share_one_sync(store):
    client = FakeClient()
    coordinator = _coordinator(store, client)
    await asyncio.gather(*[
        coordinator.ensure_covered(REPO, NOW - timedelta(days=3), NOW) for _ in range(5)])
    assert client.syncs == 1


async def test_request_for_older_data_waits_then_extends(store):
    gate = asyncio.Event()
    client = FakeClient(gate=gate)
    coordinator = _coordinator(store, client)
    narrow = asyncio.create_task(coordinator.ensure_covered(REPO, NOW - timedelta(days=2), NOW))
    await asyncio.sleep(0.01)  # the narrow sync holds the lock
    wide = asyncio.create_task(coordinator.ensure_covered(REPO, NOW - timedelta(days=8), NOW))
    await asyncio.sleep(0.01)
    gate.set()
    await asyncio.gather(narrow, wide)
    assert client.syncs == 2  # the narrow sync did not cover 8 days back, so one more ran
    assert (await store.get_coverage(REPO.key)).covered_from == NOW - timedelta(days=8)


async def test_failed_sync_surfaces_error_and_does_not_mark_coverage(store):
    class Broken:
        async def pull_request_pages(self, repo):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    with pytest.raises(RuntimeError):
        await _coordinator(store, Broken()).ensure_covered(REPO, NOW - timedelta(days=1), NOW)
    assert await store.get_coverage(REPO.key) is None

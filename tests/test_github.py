import httpx
import pytest

from reviewflow.errors import RepositoryNotFound, UpstreamError, UpstreamRateLimited
from reviewflow.github import GitHubClient
from reviewflow.models import RepoRef, ReviewState
from tests.factories import FakeGitHub, at, pr, review

REPO = RepoRef("acme", "widgets")


async def _no_sleep(_: float) -> None:
    return None


def _client(transport: httpx.BaseTransport, **kwargs) -> GitHubClient:
    return GitHubClient(httpx.AsyncClient(transport=transport), sleep=_no_sleep, **kwargs)


async def _collect(client: GitHubClient):
    return [page async for page in client.pull_request_pages(REPO)]


async def test_pages_newest_updated_first_and_parses_fields():
    fake = FakeGitHub([
        pr(1, ready=at(1), merged=at(2), reviews=(review("bob", at(1, 5)),)),
        pr(2, created=at(3), ready=at(4), merged=at(5), author="ghost"),
        pr(3, ready=at(6)),
    ])
    pages = await _collect(_client(fake.transport(), page_size=2))
    assert [len(p.pull_requests) for p in pages] == [2, 1]
    numbers = [x.number for page in pages for x in page.pull_requests]
    assert numbers == [3, 2, 1]
    by_number = {x.number: x for page in pages for x in page.pull_requests}
    assert by_number[2].ready_at == at(4) and by_number[2].created_at == at(3)
    assert by_number[2].author == "ghost"
    assert by_number[1].reviews[0].reviewer == "bob"
    assert by_number[1].reviews[0].state is ReviewState.APPROVED
    assert pages[0].oldest_updated_at == at(5)


async def test_fetches_reviews_beyond_the_inline_page():
    many = tuple(review(f"r{i}", at(1, i / 10)) for i in range(30))
    fake = FakeGitHub([pr(1, ready=at(1), merged=at(3), reviews=many)])
    pages = await _collect(_client(fake.transport()))
    assert len(pages[0].pull_requests[0].reviews) == 30
    assert fake.review_calls == 1


async def test_missing_repository():
    fake = FakeGitHub([])
    fake.repo_exists = False
    with pytest.raises(RepositoryNotFound):
        await _collect(_client(fake.transport()))


async def test_rate_limit_response_maps_to_retry_after():
    transport = httpx.MockTransport(lambda _: httpx.Response(
        403, headers={"retry-after": "42"}, json={"message": "secondary rate limit"}))
    with pytest.raises(UpstreamRateLimited) as exc:
        await _collect(_client(transport))
    assert exc.value.retry_after == 42


async def test_retries_transient_failures_then_succeeds():
    fake = FakeGitHub([pr(1, ready=at(1), merged=at(2))])
    responses = iter([httpx.Response(502), httpx.Response(503)])

    def flaky(request: httpx.Request) -> httpx.Response:
        return next(responses, None) or fake.handle(request)

    pages = await _collect(_client(httpx.MockTransport(flaky)))
    assert pages[0].pull_requests[0].number == 1


async def test_gives_up_after_max_attempts():
    transport = httpx.MockTransport(lambda _: httpx.Response(502))
    with pytest.raises(UpstreamError):
        await _collect(_client(transport, max_attempts=2))


async def test_bad_credentials_are_not_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, json={"message": "Bad credentials"})

    with pytest.raises(UpstreamError):
        await _collect(_client(httpx.MockTransport(handler)))
    assert len(calls) == 1


async def test_stops_before_exhausting_the_token_budget():
    fake = FakeGitHub([pr(n, ready=at(n), merged=at(n, 1)) for n in range(1, 5)], remaining=40)
    client = _client(fake.transport(), page_size=2, min_rate_budget=100)
    with pytest.raises(UpstreamRateLimited):
        await _collect(client)
    assert fake.pr_page_calls == 1  # the page that reported the low budget, then stop


async def test_owner_and_name_travel_as_variables_only():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return FakeGitHub([]).handle(request)

    await _collect(_client(httpx.MockTransport(handler)))
    body = seen[0].content.decode()
    assert '"owner": "acme"' in body or '"owner":"acme"' in body
    assert str(seen[0].url) == "https://api.github.com/graphql"


async def test_pr_opened_ready_then_converted_to_draft_keeps_creation_as_ready_time():
    fake = FakeGitHub([pr(1, ready=at(1), merged=at(3))])

    def handler(request: httpx.Request) -> httpx.Response:
        response = fake.handle(request)
        body = response.json()
        node = body["data"]["repository"]["pullRequests"]["nodes"][0]
        node["draftEvents"] = {"nodes": [{"__typename": "ConvertToDraftEvent"}]}
        return httpx.Response(200, json=body)

    pages = await _collect(_client(httpx.MockTransport(handler)))
    assert pages[0].pull_requests[0].ready_at == at(1)

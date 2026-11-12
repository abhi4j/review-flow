from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from reviewflow.config import Settings
from reviewflow.main import create_app
from tests.factories import FakeGitHub
from tests.fakes import FakeLLM, good_answer
from tests.scenario import scenario_prs

BASE = "/v1/repos/acme/widgets/insights/review-flow"
PERIOD = {"since": "2026-10-11", "until": "2026-10-21"}


def _settings(tmp_path, **overrides) -> Settings:
    # PERIOD is fixed while the service clock is real: keep the lookback limit out of the way.
    values = {"github_token": "test-token", "database_path": tmp_path / "test.db",
              "max_lookback_days": 36500, **overrides}
    return Settings(_env_file=None, **values)


@pytest.fixture
def fake_github():
    return FakeGitHub(scenario_prs())


@pytest.fixture
def client(tmp_path, fake_github):
    app = create_app(_settings(tmp_path),
                     github_http=httpx.AsyncClient(transport=fake_github.transport()),
                     llm=FakeLLM(good_answer()))
    with TestClient(app) as c:
        yield c


def test_review_flow_insight(client, fake_github):
    response = client.get(BASE, params=PERIOD)
    assert response.status_code == 200
    body = response.json()
    assert body["repository"] == "acme/widgets"
    assert body["summary"]["ttfr_p50_hours"]["current"] == 8.0
    assert body["summary"]["merged_prs"] == {"current": 5, "previous": 2, "delta": 3,
                                             "delta_pct": 150.0, "delta_ci": [-2.19, 8.19],
                                             "significant": False}
    assert body["drivers"] == {"by_size": None, "by_author": None}
    assert body["period"]["days"] == 10.0
    assert response.headers["etag"]
    assert response.headers["x-request-id"]


def test_repeat_query_is_served_from_storage_and_supports_etags(client, fake_github):
    first = client.get(BASE, params=PERIOD)
    calls = fake_github.pr_page_calls
    second = client.get(BASE, params=PERIOD, headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 304
    assert fake_github.pr_page_calls == calls


def test_narrative_endpoint(client):
    response = client.get(f"{BASE}/narrative", params=PERIOD)
    assert response.status_code == 200
    body = response.json()
    assert body["hypothesis"] is None  # too few PRs for any change to be significant
    assert body["observations"][0]["evidence"][0]["source"] == "/summary/ttfr_p50_hours/current"
    assert body["meta"]["model"] == "fake-model-1"


@pytest.mark.parametrize(("path", "params", "status"), [
    ("/v1/repos/-bad-/widgets/insights/review-flow", {}, 422),
    ("/v1/repos/acme/wid%20gets/insights/review-flow", {}, 422),
    ("/v1/repos/acme/../insights/review-flow", {}, 404),  # never reaches GitHub
    (BASE, {"since": "2026-10-21", "until": "2026-10-11"}, 422),
    (BASE, {"since": "2025-01-01", "until": "2026-01-01"}, 422),  # longer than 180 days
    (BASE, {"since": "not-a-date"}, 422),
    (BASE, {"top": 0}, 422),
])
def test_invalid_input_is_rejected_as_problem_json(client, path, params, status):
    response = client.get(path, params=params)
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")


def test_since_older_than_the_lookback_limit_is_rejected(tmp_path, fake_github):
    app = create_app(_settings(tmp_path, max_lookback_days=365),
                     github_http=httpx.AsyncClient(transport=fake_github.transport()))
    since = (datetime.now(UTC) - timedelta(days=400)).date().isoformat()
    with TestClient(app) as c:
        response = c.get(BASE, params={"since": since})
    assert response.status_code == 422
    assert fake_github.pr_page_calls == 0


def test_unknown_repository_is_404(client, fake_github):
    fake_github.repo_exists = False
    response = client.get(BASE, params=PERIOD)
    assert response.status_code == 404
    assert response.json()["title"] == "Repository not found"


def test_allow_list(tmp_path, fake_github):
    app = create_app(_settings(tmp_path, allowed_repos="other/repo"),
                     github_http=httpx.AsyncClient(transport=fake_github.transport()))
    with TestClient(app) as c:
        assert c.get(BASE, params=PERIOD).status_code == 403
    assert fake_github.pr_page_calls == 0


def test_narrative_without_api_key_is_503(tmp_path, fake_github):
    app = create_app(_settings(tmp_path),
                     github_http=httpx.AsyncClient(transport=fake_github.transport()))
    with TestClient(app) as c:
        response = c.get(f"{BASE}/narrative", params=PERIOD)
    assert response.status_code == 503
    assert "LLM_API_KEY" in response.json()["detail"]
    assert fake_github.pr_page_calls == 0


def test_rate_limited_upstream_is_503_with_retry_after(tmp_path):
    transport = httpx.MockTransport(lambda _: httpx.Response(429, headers={"retry-after": "30"}))
    app = create_app(_settings(tmp_path), github_http=httpx.AsyncClient(transport=transport))
    with TestClient(app) as c:
        response = c.get(BASE, params=PERIOD)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"


def test_untrusted_request_id_is_replaced(client):
    response = client.get("/healthz", headers={"X-Request-ID": "x\nforged-log-line"})
    assert response.headers["x-request-id"] != "x\nforged-log-line"
    kept = client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert kept.headers["x-request-id"] == "abc-123"


def test_ui_is_served_with_a_strict_csp(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "script-src 'self'" in response.headers["content-security-policy"]


def test_readiness(client):
    assert client.get("/readyz").json() == {"status": "ready"}


def test_secrets_never_appear_in_responses_or_logs(client, caplog):
    client.get(BASE, params=PERIOD)
    client.get(f"{BASE}/narrative", params=PERIOD)
    assert "test-token" not in caplog.text

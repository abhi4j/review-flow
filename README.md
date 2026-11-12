# Review Flow

Review Flow looks at a GitHub repository's pull requests and reviews and tells you how
code review is going: how long PRs wait before someone reviews them, who is doing the
reviewing, and what changed compared with the previous period. A second endpoint uses an
LLM to explain the change in a few sentences, with a likely cause and the numbers that
support it.

## Why

Most engineering dashboards count output: commits, merged PRs, lines changed. Those
numbers reward volume and say little about where work gets stuck. In most teams it gets
stuck in review. A PR that waits two days for a first look costs its author the context
they had when they opened it, and the cause is usually visible in the data: one person
doing most of the reviews, that person going on leave, PRs getting larger, or outside
contributors waiting behind everyone else.

Two things make this harder than it looks. First, week-to-week numbers are noisy. With
thirty PRs, a median wait can move by hours for no reason at all, and a dashboard that
colours every change red or green trains people to react to noise. Second, an LLM asked
to explain the numbers will happily explain the noise too, and may quote figures that
were never in the data.

So the service does three things:

1. It measures review flow for a period and compares it with the period before.
2. It attaches a 95% interval to every change, so you can tell a real shift from noise,
   and it splits a change in wait time into "different kinds of PR were merged" and "the
   same kinds of PR waited longer".
3. It asks an LLM to explain only the changes that are real. The model refers to facts
   by id instead of writing numbers, the server fills the numbers in, and an answer that
   cannot be checked against the data is rejected.

Here is what it said about `astral-sh/uv` for October 2026:

> More PRs merged, fewer reviewed, as most now skip review
>
> With merge volume up and review pairs down, PRs increasingly bypassed review instead of
> queueing for it, which mechanically removed the review wait and halved the median cycle
> while leaving first-review time flat. The shorter cycle is therefore not faster
> reviewing; it is less reviewing.

## Setup

You need:

- Docker, or Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- A GitHub [fine-grained token](https://github.com/settings/personal-access-tokens/new)
  with read-only access to public repositories and nothing else
- Optionally, an API key for an OpenAI-compatible LLM endpoint, for the narrative

Copy the example config and fill it in:

```bash
cp .env.example .env
```

`GITHUB_TOKEN` is the only required value. For the narrative, set `LLM_API_KEY`, and if
your provider is not DeepSeek's own API, also `LLM_BASE_URL` and `LLM_MODEL`. The setup
used for development and the evals was DeepSeek V4.1 Flash hosted on Deploy:

```bash
LLM_API_KEY=...
LLM_BASE_URL=https://api-sf1-cdn.deploy.com/api/v1
LLM_MODEL=deepseek-ai/deepseek-v4.1-flash-1017
LLM_JSON_MODE=json_schema
```

`LLM_JSON_MODE` controls how JSON output is enforced: `json_schema` for providers that
can constrain output to a schema, `json_object` (the default) for those that only offer
JSON mode. Without an LLM key everything except the narrative endpoint works.

Then run it, either with Docker:

```bash
docker compose up --build
```

or directly:

```bash
uv sync
uv run python -m reviewflow
```

Open http://localhost:8000, enter a repository such as `pydantic/pydantic`, and pick a
period. The first request for a repository fetches its PR history from GitHub, which
takes a few seconds for a small project and a couple of minutes for a busy one. After
that the data comes from SQLite and only new activity is fetched.

To check your changes:

```bash
make check   # ruff, mypy --strict, and the test suite (no network access needed)
make eval    # runs the narrative against simulated repositories; needs an LLM key
```

The evals simulate repositories where something specific changed (a key reviewer left,
PRs got bigger, outside contributors started waiting longer) and two where nothing did,
then check whether the narrative names the right cause, or names none. The latest results
are in [evals/results.md](evals/results.md).

## API

Both endpoints take `since` and `until` as ISO 8601 dates or times (UTC if no offset).
The default period is the last 30 days. Interactive docs are at `/docs`.

```bash
curl "localhost:8000/v1/repos/pydantic/pydantic/insights/review-flow?since=2026-10-01&until=2026-11-01"
curl "localhost:8000/v1/repos/pydantic/pydantic/insights/review-flow/narrative?since=2026-10-01&until=2026-11-01"
```

The first returns the metrics; the second returns the narrative, with a hypothesis,
a confidence score and evidence that points back into the metrics response. Errors use
`application/problem+json`. You will see `422` for a bad repository name or period, `403`
for a repository outside `ALLOWED_REPOS`, `404` for one the token cannot see, `502` when GitHub or the LLM fails or the model's
answer cannot be verified, and `503` with `Retry-After` when an upstream is rate limited
or no LLM key is configured. The metrics response has an ETag, so repeat requests can get
a `304`.

## What the numbers mean

Flow metrics cover human-authored PRs merged in the period. Using merged PRs means every
PR counted has a complete history; counting PRs opened in the period would leave out the
ones still waiting, which are exactly the slow ones. Reviewer load counts reviews
submitted in the period on any PR.

| Metric | Meaning |
|---|---|
| Wait for first review | Hours from the PR becoming ready for review to the first review by someone other than the author. Drafts count from when they were marked ready. Reported as median and p90. |
| First review to merge | Hours from that first review to the merge. |
| Ready to merge | Hours from ready for review to merge. |
| Merged without review | Share of merged PRs with no review at all before the merge. |
| PR reviews | One reviewer reviewing one PR, however many rounds it took, so ten comments on one PR do not count as ten reviews. |
| Review concentration | The busiest reviewer's share of PR reviews, and how many reviewers it takes to cover half of them. |
| Segments | Wait for first review by PR size and by whether the author has write access. |

Bot accounts are left out, and so are authors' replies on their own PRs, which GitHub
records as reviews. Medians and shares get bootstrap intervals and counts get Poisson
intervals; a change is marked significant when its interval excludes zero. Each response
also lists the slowest PRs and the unreviewed merges with links, so any number can be
checked against GitHub.

## Configuration

| Variable | Default | |
|---|---|---|
| `GITHUB_TOKEN` | required | Read-only GitHub token. Never logged or returned. |
| `LLM_API_KEY` | unset | Enables the narrative endpoint. |
| `LLM_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible Chat Completions endpoint. |
| `LLM_MODEL` | `deepseek-flash` | Model name at that endpoint. |
| `LLM_JSON_MODE` | `json_object` | `json_object` or `json_schema`, see above. |
| `ALLOWED_REPOS` | any | Comma-separated `owner/repo` list this instance will serve. |
| `SYNC_TTL_SECONDS` | 600 | How old stored data can be before a request refreshes it. |
| `DEFAULT_WINDOW_DAYS` / `MAX_WINDOW_DAYS` | 30 / 180 | Default and maximum period length. |
| `MAX_LOOKBACK_DAYS` | 365 | How far back a period may start. |
| `DATABASE_PATH` | `data/reviewflow.db` | SQLite file; `/data/reviewflow.db` on a volume in Docker. |

## Code layout

```
reviewflow/
  api.py         routes and request validation
  github.py      GitHub GraphQL client
  sync.py        incremental sync into SQLite
  store.py       database schema and queries
  metrics.py     the review-flow metrics
  stats.py       intervals and the split of a change in wait time
  narrative/     fact table, prompt, LLM client, answer validation
  static/        the web UI
evals/           simulated repositories and the eval runner
tests/           tests against a fake GitHub and a scripted LLM
```

[NOTES.md](NOTES.md) covers the design decisions, the trade-offs, what the evals showed,
and what I would do next.

# Submission notes

## 1. Running it locally

Prerequisites: a GitHub fine-grained token with **Public repositories (read-only)** access;
optionally a key for an OpenAI-compatible LLM API (DeepSeek by default) for the narrative.

```bash
cp .env.example .env
# edit .env:  GITHUB_TOKEN=github_pat_...
#             LLM_API_KEY=...                (optional: enables the narrative)
#             LLM_BASE_URL=...  LLM_MODEL=...  (for providers other than DeepSeek)

docker compose up --build                  # http://localhost:8000
# or, with Python 3.12+ and uv:
uv sync && uv run python -m reviewflow     # http://127.0.0.1:8000

make check                                 # lint, mypy --strict, offline tests
make eval                                  # narrative evals; needs LLM_API_KEY
```

Try `pydantic/pydantic` or `astral-sh/uv` over the last 30 or 90 days. The README has the
API reference, metric definitions and configuration.

## 2. Architecture tour

**Metrics path.** A request is validated (GitHub's owner/repo naming rules, a bounded
period) and then asks the sync coordinator whether SQLite already covers the period *and*
the baseline period before it. Coverage is a watermark over PR `updatedAt`: a PR's
`updatedAt` is never earlier than its merge or any of its reviews, so "every PR updated
since T" is exactly the data that can affect metrics from T onward. The GitHub client
pages PRs newest-updated first over GraphQL (one request returns 50 PRs with their reviews
and ready-for-review events; REST would need 51), so an incremental refresh reads only
until it passes the old watermark. The request waits for the sync, and a lock per
repository means concurrent requests trigger one sync between them: the next request to
get the lock re-reads coverage and finds it fresh. Metrics are pure functions over domain
records, run off the event loop, and the response is ETag-versioned.

**Statistics.** `stats.py` gives every change a 95% interval: a percentile bootstrap for
medians and shares, a Poisson interval for counts, with a fixed seed so the same data
always gives the same response (and ETag, and narrative cache key). The change in median
wait for first review is also split, by PR size and by author type, into *mix* (this
period's PRs reweighted to last period's mix give a counterfactual median; actual minus
counterfactual) and *within* (counterfactual minus last period). The two add up to the
change, so "PRs got bigger" and "reviewers got slower" become numbers with intervals
instead of a story.

**Narrative path.** The metrics response is flattened into a fact table: every number
gets a stable id, a label taken from the schema's field descriptions, a unit, and a JSON
pointer back into the response. The prompt contains only these facts and constrained
identifiers. The model answers in JSON against a schema (constrained decoding where the
provider supports it, JSON mode with the schema in the prompt otherwise), writing
`{{fact_id}}`, `{{@login}}` or `{{#pr}}` wherever it refers to data, and gets a private
`analysis` field to reason in first. A validator rejects unknown references, digits typed
outside a reference, unknown evidence ids, and hypotheses that cite fewer than two facts
or no significant change; the model gets
one repair attempt with the validator's messages, and a second failure is a `502` rather
than an unverified answer. The server then renders every number itself, hydrates the
evidence chain, and caps the model's confidence by what the sample size can support.
Results are cached under a hash of all inputs, so there is nothing to invalidate.

### Decisions worth discussing

- **Statistics decide what changed; the model explains it.** Asking an LLM whether a
  move from 4.1h to 5.3h on 30 PRs is meaningful invites a confident story about noise.
  The intervals and the mix/within split are deterministic, the prompt shows each
  change's verdict, and the validator refuses a hypothesis with no significant change
  behind it. The evals measure what is left: whether the model picks the right
  significant facts, and whether it abstains when nothing changed.
- **Counterfactual reweighting for the split, on medians.** Splitting a change in means is
  textbook, but wait times are heavy-tailed and the service reports medians. Reweighting
  to the old mix keeps the split on the statistic people read, and the weighted median
  reduces to NumPy's median when the mix is unchanged, so "no mix shift" gives exactly
  zero.
- **Review flow over contributor leaderboards.** Commit and PR counts reward volume; the
  wait for review is where teams queue, and its causes are usually visible in the data.
  Comparing against the previous period makes "what changed" the headline.
- **Merged-PR cohort for flow metrics.** Measuring PRs *opened* in the period would drop
  the slowest, still-open ones from the median (right-censoring) and make bad periods look
  good. Merged PRs have complete lifecycles. Reviewer load uses reviews *submitted* in the
  period instead, since that is when the work happened.
- **Correctness details that change the numbers:** PRs opened as drafts measured from ready-for-review
  (a PR opened ready and converted to draft later is not reset);
  authors' replies on their own PR (GitHub records them as `COMMENTED` reviews) excluded;
  bots excluded; reviews before the window still count for a PR merged inside it (there is
  a test for each).
- **The model never writes numbers.** Checking the model's numbers after the fact means
  parsing prose and tolerating rounding; making it reference facts that the server renders
  makes grounding true by construction.
- **No free text from GitHub in the prompt.** PR titles would help some hypotheses, but
  they are attacker-controlled on public repositories. Leaving them out removes the
  injection path instead of filtering it.
- **Confidence is bounded, not trusted.** LLM self-reported confidence is uncalibrated, so
  the score is `min(model, ceiling)`, with the ceiling set by sample size and both values
  returned. The evals report the Brier score of both, which is how to tell whether the cap
  helps; the thresholds stay heuristic until there is labelled data from real repositories.
- **Requests wait for the sync.** The first query for a busy repository can take a
  minute, which is fine for a demo and keeps the request path simple. At larger scale the
  sync would run in the background and the API would return `202 Accepted` with progress
  until the data is ready, as GitHub does for its own repository statistics.
- **SQLite, single process.** Zero-dependency quickstart and enough for one node. The
  schema is plain SQL; moving to Postgres is mechanical, and the in-process
  per-repository lock would become a Postgres advisory lock.
- **GET for the narrative.** From the client's view it is a safe, repeatable read of a
  derived representation, and the content-addressed cache makes repeats free.

### Things I chose not to do

- **No authentication or per-client rate limiting on the service.** Anyone who can reach it
  spends the GitHub and LLM budget. `ALLOWED_REPOS`, the GitHub budget floor and the
  narrative cache limit the damage; in production this sits behind a gateway with API keys
  and quotas.
- **Wall-clock wait times.** A PR opened Friday evening "waits" all weekend. Working-hours
  adjustment needs per-team calendars and time zones, so it is out of scope, and it is the
  largest distortion in the numbers.
- **Bot detection is GitHub's actor type only.** Bots running as user accounts (for example
  `k8s-ci-robot`) count as people. `authorAssociation` is GitHub's current value, not the
  value when the PR was opened.
- **No correction for multiple comparisons.** Around twenty intervals per response at 95%
  means some "significant" changes are chance. The p90 intervals need 50 observations
  (tail percentiles resample poorly), and the hypothesis also needs the model to connect
  the facts, but nothing controls the family-wise error rate. The no-change eval scenarios
  measure the resulting false-alarm rate rather than hiding it.
- **Sync is not resumable mid-walk.** If a long first backfill stops at the rate-limit floor
  or the server restarts, the next attempt re-reads from the newest page. Writes are idempotent, so this costs API
  points, not correctness.

### What the evals found

`make eval` against `deepseek-ai/deepseek-v4.1-flash-1017` (8 scenarios × 3 seeds;
the latest run is in `evals/results.md`). Each prompt change below was driven by a
measured failure, and the prompt version is in every cache key and eval report:

| Prompt | Causes found | Abstained when nothing changed | Grounded | Repairs | Median latency |
|---|---|---|---|---|---|
| v3 | 18/18 | 1/6 | 20/24 | 9/24 | 139s |
| v4 | 17/18 | 5/6 | 23/24 | 5/24 | 92s |
| v7 | 18/18 | 5/6 | 24/24 | 3/24 | 73s |
| v8 (committed) | 17/18 | 5/6 | 23/24 | 6/24 | 86s |

- **v3 → v4: untested changes read as real.** Changes with no interval (a p90 on a small
  sample, bus factor, reviewer shares) were shown without a verdict, and the model called
  large ones "significant", then built hypotheses the validator rejected. Labelling them
  "no interval: not evidence of a change", and telling the model outright when nothing
  in the period is significant, took abstention from 1/6 to 5/6.
- **v4 → v7: length and truncation.** Most repairs were hypothesis statements over the
  limit, because the model restated evidence values inside them; the one grounding
  failure was an answer cut off mid-JSON by the output-token cap. Character budgets, "the
  evidence ids carry the values", a bounded `analysis` field and a larger token cap
  fixed both, and cut latency.
- **What is left is the statistics, not the prompt.** The remaining failure is a
  no-change history where two changes come out significant by chance (multiple
  comparisons, see above), and the model reasonably explains them. The fix belongs in
  the intervals (false-discovery control), not in more prompt rules; tuning further on
  24 runs would fit the prompt to the eval.
- **The noise floor is a few runs.** v8 only reworded one phrase of v7, yet one fewer
  cause was found and repairs doubled. Differences of one to three runs in 24 are
  run-to-run variance; a prompt change needs more seeds before it counts as better.
- **Most repairs are definitional constants.** The model writes "95% intervals" or
  "500+ lines", which the no-typed-digits rule rejects although they are not data. An
  allow-list of the constants the prompt itself defines would remove most repairs.
- **Confidence is under-, not over-stated.** The sample-size cap never bound: the model
  reported moderate confidence (Brier around 0.2) while being right in almost every run. The cap
  stays as a guard, but on this model a calibration step would raise scores, not cut them.

## 3. With another day

1. Make `make eval` a CI gate on prompt and model changes, tracking results per prompt
   version; label narratives from real repositories to fit the confidence ceiling instead
   of hand-setting it, and recalibrate the model's under-stated confidence.
2. Control false discoveries across the intervals in a response (Benjamini-Hochberg on
   bootstrap p-values) and report the reviewer-departure effect as its own driver.
3. Business-hours wait times, with weekends and a configurable team time zone excluded.
4. Webhook ingestion (`pull_request`, `pull_request_review`) for freshness instead of
   TTL polling; background syncs with `202` responses for first loads; Postgres with a
   per-repository advisory lock; a resumable sync cursor.
5. Review-request data (who was asked versus who reviewed) to find routing bottlenecks such
   as a CODEOWNERS entry pointing at one person.
6. An overall deadline and streaming for the narrative endpoint; API keys and per-client
   quotas.

## 4. AI usage

Claude Code was the main implementer; I directed scope and reviewed the output. I chose
the stack (Python and FastAPI) and the narrative model. Claude wrote most of the
code, tests, eval scenarios and docs, and checked the UI in a headless browser against a
fake GitHub and a scripted model.

Review rounds changed the result:

- **Scope cuts.** A background sync returning `202` while still fetching, and a second
  LLM provider behind the same interface, were removed as more than a take-home needs.
- **A concurrency bug.** The first background sync had a check-then-act race (five
  concurrent requests started five syncs), caught by a concurrency test. The
  per-repository lock that replaced it has no such window, and the same test guards it.
- **Correctness and abuse fixes from a review pass.** PRs opened ready and later
  converted to draft had their wait clock reset; `since` had no lower bound, so one
  request could trigger a walk of a repository's whole history; and the narrative endpoint
  spent GitHub budget before discovering it had no LLM key.
- **ML depth.** The first version let the model judge which changes mattered. Reviewing it
  as an ML system led to the intervals, the mix/within split, the significance rule in
  the validator, and evals that measure accuracy, abstention and calibration instead of
  pass/fail.
- **Provider-neutral LLM client.** The narrative started on Claude, briefly moved to
  Gemini (which was overloaded during testing), and now uses any OpenAI-compatible Chat
  Completions API, DeepSeek by default. Each swap touched only the client module: the
  prompt, schema, validator and evals sit behind a one-method `LLM` protocol. Providers
  differ in JSON enforcement, so `LLM_JSON_MODE` chooses constrained decoding or JSON mode
  with the schema in the prompt; the validator makes both safe. The client tests run the
  real openai SDK against a mocked HTTP transport.

# Narrative evals: deepseek-ai/deepseek-v4.1-flash-1017, review-flow-narrative/v8, 3 seed(s)

| Measure | Result |
|---|---|
| Accuracy on planted causes | 17/18 |
| Abstention when nothing changed | 5/6 |
| Grounded answers | 23/24 |
| Needed the repair attempt | 6/24 |
| Brier score, model's confidence | 0.219 (n=18) |
| Brier score, capped confidence | 0.219 (n=18) |
| Median latency | 85.8s |
| Tokens, total | 240,742 |

| Scenario | Correct | Planted |
|---|---|---|
| key_reviewer_absent | 3/3 | The reviewer who did half the reviews stopped; waits for first review grew. |
| larger_prs | 3/3 | PRs got much larger, and large PRs wait longer for a first review. |
| contributors_wait | 3/3 | External contributors' PRs wait far longer than maintainers' PRs. |
| contributor_mix | 2/3 | Contributors always waited longer; now far more of the PRs are theirs. |
| load_concentrates | 3/3 | One reviewer took over most reviews; waits did not change. |
| volume_surge | 3/3 | Twice as many PRs arrived for the same reviewers, and waits grew. |
| steady_state | 2/3 | Nothing changed between periods. |
| steady_state_quiet | 3/3 | Nothing changed, in a quiet repository where medians swing by chance. |

Failures:

- contributor_mix (seed 3): NarrativeFailed: The model's answer could not be grounded in the computed metrics, so no narrative was returned.
- steady_state (seed 2): invented a cause: The significant growth in median PR size (+37 lines) plausibly concentrated review attention, coinciding with @ben's significant drop (-12) and matching non-significant pickups by @dara and @chen; flat ttfr and cycle suggest the redistribution kept wait stable.

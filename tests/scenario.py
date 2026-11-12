"""A small repository history whose metrics are worked out by hand in test_metrics.py.

Current period:  2026-10-11 to 2026-10-21 (10 days)
Previous period: 2026-10-01 to 2026-10-11
"""

from reviewflow.models import PullRequest, ReviewState
from reviewflow.period import Window
from tests.factories import at, pr, review

A, C, CR = ReviewState.APPROVED, ReviewState.COMMENTED, ReviewState.CHANGES_REQUESTED

WINDOW = Window(at(days=10), at(days=20))


def scenario_prs() -> list[PullRequest]:
    return [
        # --- merged in the previous period ---------------------------------------------
        pr(10, author="alice", ready=at(1), merged=at(1, 2), additions=10,
           reviews=(review("dave", at(1, 1), A),)),
        pr(11, author="bob", ready=at(2), merged=at(2, 4), additions=20,
           reviews=(review("alice", at(2, 3), A),)),
        # --- merged in the current period ------------------------------------------------
        # Straightforward: ttfr 2h, review->merge 3h, cycle 5h.
        pr(1, author="alice", ready=at(10), merged=at(10, 5), additions=50,
           reviews=(review("bob", at(10, 2), A),)),
        # Opened as a draft a day early; wait counts from ready-for-review, not creation.
        # The author's own reply (a COMMENTED review by carol) must not count.
        pr(2, author="carol", association="CONTRIBUTOR", created=at(10), ready=at(11),
           merged=at(12), additions=400, deletions=200,
           reviews=(review("carol", at(11, 5), C),
                    review("bob", at(11, 10), C, comments=3),
                    review("bob", at(11, 20), A))),
        # Merged without review.
        pr(3, author="alice", ready=at(13), merged=at(13, 1), additions=120),
        # Bot-authored: excluded from flow metrics, but dave's review is real review work.
        pr(4, author="dependabot", bot=True, ready=at(14), merged=at(14, 2), additions=4,
           reviews=(review("dave", at(14, 1), A),)),
        # A bot reviewer's early comment must not count as the first review.
        pr(5, author="erin", ready=at(15), merged=at(16, 6), additions=30,
           reviews=(review("ci-bot", at(15, 1), C, bot=True),
                    review("alice", at(15, 6), CR, comments=2),
                    review("bob", at(16), A))),
        # Reviewed in the previous period, merged in this one: its first review predates
        # the window, so loading only in-window reviews would get ttfr wrong.
        pr(6, author="alice", ready=at(9), merged=at(10, 12), additions=200,
           reviews=(review("bob", at(9, 12), C),)),
        # Opened but never merged: contributes nothing.
        pr(7, author="frank", ready=at(18), additions=5),
    ]

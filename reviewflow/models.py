"""Domain records: the normalised subset of GitHub data the metrics need."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ReviewState(StrEnum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    COMMENTED = "COMMENTED"
    DISMISSED = "DISMISSED"


# GitHub's CommentAuthorAssociation values that mean "has write access / is on the team".
MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


@dataclass(frozen=True, slots=True)
class RepoRef:
    owner: str
    name: str

    @property
    def key(self) -> str:
        """Canonical storage key. GitHub owner/repo names are case-insensitive."""
        return f"{self.owner}/{self.name}".lower()

    @property
    def display(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class Review:
    id: str
    reviewer: str
    reviewer_is_bot: bool
    state: ReviewState
    submitted_at: datetime
    inline_comments: int


@dataclass(frozen=True, slots=True)
class PullRequest:
    number: int
    url: str
    author: str
    author_is_bot: bool
    author_association: str
    created_at: datetime
    # When the PR first became reviewable: the first ready-for-review event for PRs opened
    # as drafts, otherwise creation time. Review wait is measured from here, not creation.
    ready_at: datetime
    merged_at: datetime | None
    additions: int
    deletions: int
    reviews: tuple[Review, ...]

    @property
    def size_lines(self) -> int:
        return self.additions + self.deletions

    @property
    def is_maintainer_authored(self) -> bool:
        return self.author_association in MAINTAINER_ASSOCIATIONS

    def human_reviews(self) -> list[Review]:
        """Reviews by someone other than the author and not by a bot.

        GitHub records an author's replies to review threads as COMMENTED reviews by the
        author, so excluding self-reviews is required for the counts to mean anything.
        """
        author = self.author.lower()
        return [
            r for r in self.reviews if not r.reviewer_is_bot and r.reviewer.lower() != author
        ]

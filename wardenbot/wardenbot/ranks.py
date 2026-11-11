"""Rank progression and sign-up penalty rules. Pure functions, no Discord."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

RANK_ORDER = ("recruit", "private", "sergeant")


def rank_index(rank: str | None) -> int:
    try:
        return RANK_ORDER.index(rank or "")
    except ValueError:
        return -1


def at_least(rank: str | None, minimum: str) -> bool:
    return rank_index(rank) >= rank_index(minimum)


@dataclass(frozen=True)
class RankRules:
    member_after_ops: int = 7
    veteran_after_ops: int = 30


def auto_rank(
    current: str | None,
    admitted_at: datetime | None,
    now: datetime,
    has_valid_vouch: bool,
    attended_ops: int,
    rules: RankRules,
) -> str:
    """Rank follows explicitly confirmed completed operations; legacy inputs are ignored."""
    if current is None or admitted_at is None:
        return "recruit"
    if attended_ops >= rules.veteran_after_ops:
        return "sergeant"  # stable database key, displayed as Veteran
    if attended_ops >= rules.member_after_ops:
        return "private"   # displayed as Member
    return "recruit"       # displayed as Verified


def noshow_block_until(
    history: list[tuple[datetime, bool]],
    limit: int,
    block_days: int,
) -> datetime | None:
    """Block sign-ups after `limit` consecutive no-shows.

    `history` holds (operation_end, attended) for finished operations the member
    signed up for, newest first. Returns the moment the block ends (or None). The
    caller compares it with the current time.
    """
    if limit <= 0 or len(history) < limit:
        return None
    recent = history[:limit]
    if any(attended for _, attended in recent):
        return None
    return recent[0][0] + timedelta(days=block_days)

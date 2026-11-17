"""Decides what the 60-second tick has to do for each operation (pure, no Discord)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .opsmeta import OPEN, RUNNING


@dataclass(frozen=True)
class OpTimes:
    id: int
    status: str
    start_at: datetime
    duration_min: int
    reminder60_sent: bool
    reminder15_sent: bool


@dataclass(frozen=True)
class Action:
    op_id: int
    kind: str  # remind60 | remind15 | start | autoclose


def plan_actions(ops: list[OpTimes], now: datetime, auto_close_hours: int) -> list[Action]:
    """Reminders at T-60/T-15 and automatic start. Completion is always manual.

    auto_close_hours is accepted for compatibility and deliberately ignored.
    """
    actions: list[Action] = []
    for op in ops:
        if op.status not in (OPEN, RUNNING):
            continue
        if op.status == OPEN:
            if now >= op.start_at:
                actions.append(Action(op.id, "start"))
                continue
            t15 = op.start_at - timedelta(minutes=15)
            t60 = op.start_at - timedelta(minutes=60)
            if now >= t15:
                if not op.reminder15_sent:
                    actions.append(Action(op.id, "remind15"))
            elif now >= t60 and not op.reminder60_sent:
                actions.append(Action(op.id, "remind60"))
    return actions

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
    reminder10_sent: bool


@dataclass(frozen=True)
class Action:
    op_id: int
    kind: str  # remind60 | remind10 | start | autoclose


def plan_actions(ops: list[OpTimes], now: datetime, auto_close_hours: int) -> list[Action]:
    """Reminders at T-60 and T-10, automatic start at T0, automatic close after end + grace.

    A bot that was offline sends only the reminder that still makes sense: if the T-10 moment
    has passed, the T-60 reminder is skipped. Nothing is announced for an operation that should
    already be closed."""
    actions: list[Action] = []
    for op in ops:
        if op.status not in (OPEN, RUNNING):
            continue
        end = op.start_at + timedelta(minutes=op.duration_min)
        if now >= end + timedelta(hours=auto_close_hours):
            actions.append(Action(op.id, "autoclose"))
            continue
        if op.status == OPEN:
            if now >= op.start_at:
                actions.append(Action(op.id, "start"))
                continue
            t10 = op.start_at - timedelta(minutes=10)
            t60 = op.start_at - timedelta(minutes=60)
            if now >= t10:
                if not op.reminder10_sent:
                    actions.append(Action(op.id, "remind10"))
            elif now >= t60 and not op.reminder60_sent:
                actions.append(Action(op.id, "remind60"))
    return actions

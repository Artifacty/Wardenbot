from datetime import UTC, datetime, timedelta

from wardenbot.opsplan import OpTimes, plan_actions

START = datetime(2026, 11, 8, 17, 0, tzinfo=UTC)


def op(status="open", r60=False, r10=False, minutes=120, op_id=1):
    return OpTimes(op_id, status, START, minutes, r60, r10)


def kinds(ops, now, grace=6):
    return [a.kind for a in plan_actions(ops, now, grace)]


def test_nothing_early():
    assert kinds([op()], START - timedelta(hours=2)) == []


def test_remind_60_once():
    now = START - timedelta(minutes=59)
    assert kinds([op()], now) == ["remind60"]
    assert kinds([op(r60=True)], now) == []


def test_remind_10_once_and_skips_60_when_late():
    now = START - timedelta(minutes=9)
    assert kinds([op()], now) == ["remind10"]  # bot was offline: only the useful one
    assert kinds([op(r60=True, r10=True)], now) == []


def test_start_at_t0():
    assert kinds([op()], START) == ["start"]
    assert kinds([op(status="running")], START + timedelta(minutes=5)) == []


def test_autoclose_after_end_plus_grace():
    end = START + timedelta(minutes=120)
    assert kinds([op(status="running")], end + timedelta(hours=5, minutes=59)) == []
    assert kinds([op(status="running")], end + timedelta(hours=6)) == ["autoclose"]
    # an operation that never started closes without a start announcement
    assert kinds([op()], end + timedelta(hours=7)) == ["autoclose"]


def test_finished_ops_ignored():
    assert kinds([op(status="done"), op(status="cancelled", op_id=2)], START + timedelta(days=3)) == []

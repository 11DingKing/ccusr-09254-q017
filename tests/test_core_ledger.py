"""导师工作量账本内核测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.ledger import (
    BASIS_POINTS,
    InvalidReason,
    RuleSet,
    build_ledger,
    summarize,
)
from app.core.replay import Event, EventType


def _event(
    eid: str,
    etype: EventType,
    student: str,
    payload: dict,
    *,
    plan_version: str = "P1",
) -> Event:
    return Event(
        event_id=eid,
        plan_version=plan_version,
        event_type=etype,
        student_id=student,
        payload=payload,
        created_at=datetime.now(timezone.utc),
    )


def _checkin(eid: str, student: str, start: str, end: str) -> Event:
    return _event(
        eid,
        EventType.CHECKIN,
        student,
        {
            "activity_id": "A1",
            "activity_type": "internship",
            "check_in_at": start,
            "check_out_at": end,
        },
    )


def _confirm(eid: str, student: str, checkin_id: str, mentor: str, **extra) -> Event:
    payload = {"checkin_event_id": checkin_id, "mentor_id": mentor, **extra}
    return _event(eid, EventType.MENTOR_CONFIRM, student, payload)


def _delegate(eid: str, student: str, primary: str, delegate: str, **extra) -> Event:
    return _event(
        eid,
        EventType.MENTOR_DELEGATE,
        student,
        {
            "primary_mentor_id": primary,
            "delegate_mentor_id": delegate,
            **extra,
        },
    )


def _revoke_confirm(eid: str, student: str, checkin_id: str, mentor: str) -> Event:
    return _event(
        eid,
        EventType.MENTOR_CONFIRM_REVOKE,
        student,
        {"checkin_event_id": checkin_id, "mentor_id": mentor},
    )


def test_single_confirm_credits_full_duration_to_mentor():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _confirm("E-02", "S1", "E-01", "M1"),
    ]
    state = build_ledger(events, plan_version="P1", timezone_name="Asia/Shanghai")
    assert len(state.lines) == 1
    line = state.lines[0]
    assert line.mentor_id == "M1"
    assert line.seconds == 7200
    assert line.weight_bp == BASIS_POINTS
    assert line.counts_raw is True
    assert state.invalid_confirmations == []


def test_duplicate_confirm_only_first_counts():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _confirm("E-02", "S1", "E-01", "M1"),
        _confirm("E-03", "S1", "E-01", "M2"),
    ]
    state = build_ledger(events, plan_version="P1", timezone_name="Asia/Shanghai")
    assert len(state.lines) == 1
    assert state.lines[0].mentor_id == "M1"
    invalid = state.invalid_confirmations
    assert len(invalid) == 1
    assert invalid[0].reason == InvalidReason.DUPLICATE
    assert invalid[0].mentor_id == "M2"


def test_unknown_checkin_and_student_mismatch_are_invalid():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _confirm("E-02", "S1", "E-404", "M1"),
        _confirm("E-03", "S2", "E-01", "M1"),
    ]
    state = build_ledger(events, plan_version="P1", timezone_name="Asia/Shanghai")
    reasons = {item.confirm_event_id: item.reason for item in state.invalid_confirmations}
    assert reasons == {
        "E-02": InvalidReason.UNKNOWN_CHECKIN,
        "E-03": InvalidReason.STUDENT_MISMATCH,
    }
    assert state.lines == []


def test_delegated_confirm_splits_60_40_by_default():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _delegate("E-02", "S1", "M-PRIMARY", "M-DELEGATE"),
        _confirm("E-03", "S1", "E-01", "M-DELEGATE", delegated=True,
                 primary_mentor_id="M-PRIMARY"),
    ]
    state = build_ledger(events, plan_version="P1", timezone_name="Asia/Shanghai")
    by_mentor = {line.mentor_id: line for line in state.lines}
    assert set(by_mentor) == {"M-DELEGATE", "M-PRIMARY"}
    assert by_mentor["M-PRIMARY"].seconds == 4320  # 60% of 7200
    assert by_mentor["M-DELEGATE"].seconds == 2880  # 40% of 7200
    assert sum(line.seconds for line in state.lines) == 7200
    # 原始确认数只计在确认人（受托导师）身上一次。
    assert by_mentor["M-DELEGATE"].counts_raw is True
    assert by_mentor["M-PRIMARY"].counts_raw is False
    totals = summarize(state.lines)
    assert totals["M-PRIMARY"]["weighted_count"] == 0.6
    assert totals["M-DELEGATE"]["raw_confirmations"] == 1


def test_delegated_confirm_without_active_grant_is_invalid():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _confirm("E-02", "S1", "E-01", "M-DELEGATE", delegated=True,
                 primary_mentor_id="M-PRIMARY"),
    ]
    state = build_ledger(events, plan_version="P1", timezone_name="Asia/Shanghai")
    assert state.lines == []
    assert state.invalid_confirmations[0].reason == InvalidReason.DELEGATION_NOT_ACTIVE


def test_malformed_explicit_shares_are_rejected():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _delegate("E-02", "S1", "M-PRIMARY", "M-DELEGATE"),
        _confirm("E-03", "S1", "E-01", "M-DELEGATE", delegated=True,
                 primary_mentor_id="M-PRIMARY", shares={"M-PRIMARY": 7000}),
    ]
    state = build_ledger(events, plan_version="P1", timezone_name="Asia/Shanghai")
    assert state.lines == []
    assert state.invalid_confirmations[0].reason == InvalidReason.MALFORMED_SHARES


def test_explicit_shares_split_without_rounding_loss():
    events = [
        # 100 秒按 1/3、2/3 拆分，尾差补给最后一位，合计不丢秒。
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T08:01:40+08:00"),
        _delegate("E-02", "S1", "MA", "MB"),
        _confirm("E-03", "S1", "E-01", "MB", delegated=True,
                 primary_mentor_id="MA", shares={"MA": 3333, "MB": 6667}),
    ]
    state = build_ledger(events, plan_version="P1", timezone_name="Asia/Shanghai")
    seconds = {line.mentor_id: line.seconds for line in state.lines}
    assert sum(seconds.values()) == 100
    assert seconds["MA"] == 33
    assert seconds["MB"] == 67


def test_revoke_confirmation_voids_and_releases_slot():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _confirm("E-02", "S1", "E-01", "M1"),
        # 撤销发生时新确认仍是重复，不生效。
        _confirm("E-03", "S1", "E-01", "M2"),
        _revoke_confirm("E-04", "S1", "E-01", "M1"),
        # 撤销释放槽位后，新确认生效。
        _confirm("E-05", "S1", "E-01", "M2"),
    ]
    state = build_ledger(events, plan_version="P1", timezone_name="Asia/Shanghai")
    assert len(state.lines) == 1
    assert state.lines[0].mentor_id == "M2"
    assert state.lines[0].confirm_event_id == "E-05"
    reasons = sorted(item.reason for item in state.invalid_confirmations)
    assert reasons == sorted([InvalidReason.DUPLICATE, InvalidReason.REVOKED])


def test_delegation_revoke_voids_delegated_confirmation():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _delegate("E-02", "S1", "M-PRIMARY", "M-DELEGATE"),
        _confirm("E-03", "S1", "E-01", "M-DELEGATE", delegated=True,
                 primary_mentor_id="M-PRIMARY"),
        # 主带导师撤销委托授权，受托确认作废并释放槽位。
        _delegate("E-04", "S1", "M-PRIMARY", "M-DELEGATE", revoked=True),
        _confirm("E-05", "S1", "E-01", "M-PRIMARY"),
    ]
    state = build_ledger(events, plan_version="P1", timezone_name="Asia/Shanghai")
    assert len(state.lines) == 1
    assert state.lines[0].mentor_id == "M-PRIMARY"
    assert state.lines[0].confirm_event_id == "E-05"
    assert any(
        item.reason == InvalidReason.DELEGATION_REVOKED
        and item.confirm_event_id == "E-03"
        for item in state.invalid_confirmations
    )


def test_replay_is_deterministic_regardless_of_order():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _delegate("E-02", "S1", "M-PRIMARY", "M-DELEGATE"),
        _confirm("E-03", "S1", "E-01", "M-DELEGATE", delegated=True,
                 primary_mentor_id="M-PRIMARY"),
    ]
    kwargs = dict(plan_version="P1", timezone_name="Asia/Shanghai")
    a = build_ledger(list(reversed(events)), **kwargs)
    b = build_ledger(events, **kwargs)
    assert [line.to_dict() for line in a.lines] == [line.to_dict() for line in b.lines]


def test_custom_rules_change_split():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _delegate("E-02", "S1", "M-PRIMARY", "M-DELEGATE"),
        _confirm("E-03", "S1", "E-01", "M-DELEGATE", delegated=True,
                 primary_mentor_id="M-PRIMARY"),
    ]
    rules = RuleSet(delegation_primary_share_bp=7000, delegation_secondary_share_bp=3000)
    state = build_ledger(
        events, plan_version="P1", timezone_name="Asia/Shanghai", rules=rules
    )
    seconds = {line.mentor_id: line.seconds for line in state.lines}
    assert seconds == {"M-DELEGATE": 2160, "M-PRIMARY": 5040}


def test_period_uses_plan_timezone_for_month_boundaries():
    # 上海时间 2024-04-01 00:30 = UTC 2024-03-31 16:30，应归属 4 月而非 3 月。
    events = [
        _checkin("E-01", "S1", "2024-04-01T00:30:00+08:00", "2024-04-01T01:30:00+08:00"),
        _confirm("E-02", "S1", "E-01", "M1"),
    ]
    state = build_ledger(events, plan_version="P1", timezone_name="Asia/Shanghai")
    assert len(state.lines) == 1
    assert state.lines[0].period == "2024-04"


def test_multi_month_checkin_splits_seconds_by_month():
    # 上海时间 3/31 23:00 → 4/1 02:00（UTC 15:00→18:00），3 小时拆为 1h + 2h。
    events = [
        _checkin("E-01", "S1", "2024-03-31T23:00:00+08:00", "2024-04-01T02:00:00+08:00"),
        _delegate("E-02", "S1", "M-PRIMARY", "M-DELEGATE"),
        _confirm("E-03", "S1", "E-01", "M-DELEGATE", delegated=True,
                 primary_mentor_id="M-PRIMARY"),
    ]
    state = build_ledger(events, plan_version="P1", timezone_name="Asia/Shanghai")
    by_period_mentor = {(line.period, line.mentor_id): line.seconds for line in state.lines}
    # 3 月 1 小时（3600s）按 60/40：主带 2160、受托 1440
    assert by_period_mentor[("2024-03", "M-PRIMARY")] == 2160
    assert by_period_mentor[("2024-03", "M-DELEGATE")] == 1440
    # 4 月 2 小时（7200s）：主带 4320、受托 2880
    assert by_period_mentor[("2024-04", "M-PRIMARY")] == 4320
    assert by_period_mentor[("2024-04", "M-DELEGATE")] == 2880
    assert sum(line.seconds for line in state.lines) == 10800


def test_period_uses_plan_timezone_utc_month_preceding():
    # 同一 UTC 时刻在纽约（UTC-4/UTC-5）仍属于前一个月。
    events = [
        _checkin("E-01", "S1", "2024-04-01T02:00:00+08:00", "2024-04-01T03:00:00+08:00"),
        _confirm("E-02", "S1", "E-01", "M1"),
    ]
    # 2024-04-01 02:00 +08:00 = 2024-03-31 18:00 EDT
    ny = build_ledger(events, plan_version="P1", timezone_name="America/New_York")
    assert ny.lines[0].period == "2024-03"
    sh = build_ledger(events, plan_version="P1", timezone_name="Asia/Shanghai")
    assert sh.lines[0].period == "2024-04"


def test_invalid_ruleset_is_rejected():
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
    ]
    rules = RuleSet(delegation_primary_share_bp=6000, delegation_secondary_share_bp=3000)
    with pytest.raises(ValueError):
        build_ledger(
            events, plan_version="P1", timezone_name="Asia/Shanghai", rules=rules
        )

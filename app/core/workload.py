"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from .clock import elapsed_seconds, split_by_academic_day, to_utc
from .replay import INTERNSHIP_TYPE, Event, EventType

DEFAULT_SECONDS_PER_UNIT = 45 * 60
DEFAULT_DELEGATOR_SHARE_BPS = 2000

ROLE_PRIMARY = "primary"
ROLE_DELEGATE = "delegate"
ROLE_DELEGATOR = "delegator"

_ACTING_ROLES = (ROLE_PRIMARY, ROLE_DELEGATE)


@dataclass(frozen=True)
class SettlementRule:
    """封装领域状态与业务约束。"""

    seconds_per_unit: int = DEFAULT_SECONDS_PER_UNIT
    delegator_share_bps: int = DEFAULT_DELEGATOR_SHARE_BPS

    def __post_init__(self) -> None:
        if self.seconds_per_unit <= 0:
            raise ValueError("seconds_per_unit must be positive")
        if not 0 <= self.delegator_share_bps <= 10000:
            raise ValueError("delegator_share_bps must be within 0..10000")

    def to_dict(self) -> dict[str, int]:
        return {
            "seconds_per_unit": self.seconds_per_unit,
            "delegator_share_bps": self.delegator_share_bps,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SettlementRule":
        return cls(
            seconds_per_unit=int(data["seconds_per_unit"]),
            delegator_share_bps=int(data["delegator_share_bps"]),
        )


@dataclass(frozen=True)
class WorkloadEntry:
    """封装领域状态与业务约束。"""

    entry_id: str
    role: str
    mentor_id: str
    student_id: str
    checkin_event_id: str
    confirm_event_id: str
    activity_id: str
    academic_day: str
    period: str
    seconds: int
    share_bps: int
    delegation_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "role": self.role,
            "mentor_id": self.mentor_id,
            "student_id": self.student_id,
            "checkin_event_id": self.checkin_event_id,
            "confirm_event_id": self.confirm_event_id,
            "activity_id": self.activity_id,
            "academic_day": self.academic_day,
            "period": self.period,
            "seconds": self.seconds,
            "share_bps": self.share_bps,
            "delegation_id": self.delegation_id,
        }


@dataclass
class _CheckinState:
    event_id: str
    student_id: str
    activity_id: str
    activity_type: str
    start_utc: datetime
    end_utc: datetime
    active_confirm_event_id: str | None = None
    active_mentor_id: str | None = None


@dataclass
class _Delegation:
    delegation_id: str
    student_id: str
    from_mentor_id: str
    to_mentor_id: str
    share_bps: int
    activity_id: str | None
    registered_event_id: str
    active: bool = True


def _match_delegation(
    delegations: dict[str, _Delegation],
    student_id: str,
    mentor_id: str,
    activity_id: str,
) -> _Delegation | None:
    """执行确定性的业务处理。"""
    candidates = [
        d
        for d in delegations.values()
        if d.active
        and d.student_id == student_id
        and d.to_mentor_id == mentor_id
        and (d.activity_id is None or d.activity_id == activity_id)
    ]
    if not candidates:
        return None
    # Activity-specific delegations win over general ones; ties break by
    # registration order so the result is deterministic.
    candidates.sort(key=lambda d: (d.activity_id is None, d.registered_event_id))
    return candidates[0]


def build_accruals(
    events: Iterable[Event],
    *,
    plan_version: str,
    timezone_name: str,
    rule: SettlementRule,
    up_to_event_id: str | None = None,
) -> tuple[list[WorkloadEntry], list[dict[str, Any]]]:
    """执行确定性的业务处理。"""
    sorted_events = sorted(
        (e for e in events if e.plan_version == plan_version),
        key=lambda e: e.event_id,
    )
    if up_to_event_id is not None:
        sorted_events = [e for e in sorted_events if e.event_id <= up_to_event_id]

    checkins: dict[str, _CheckinState] = {}
    delegations: dict[str, _Delegation] = {}
    entries: list[WorkloadEntry] = []
    skipped: list[dict[str, Any]] = []

    def _skip(event: Event, reason: str) -> None:
        skipped.append(
            {
                "event_id": event.event_id,
                "event_type": str(event.event_type),
                "reason": reason,
            }
        )

    def _handle_checkin(event: Event) -> None:
        try:
            start = to_utc(datetime.fromisoformat(event.payload["check_in_at"]))
            end = to_utc(datetime.fromisoformat(event.payload["check_out_at"]))
        except (KeyError, TypeError, ValueError):
            _skip(event, "invalid_checkin")
            return
        checkins[event.event_id] = _CheckinState(
            event_id=event.event_id,
            student_id=event.student_id,
            activity_id=str(event.payload.get("activity_id", "")),
            activity_type=str(event.payload.get("activity_type", "regular")),
            start_utc=start,
            end_utc=end,
        )

    def _handle_confirm(event: Event) -> None:
        target = checkins.get(event.payload.get("checkin_event_id") or "")
        if target is None:
            _skip(event, "unknown_checkin")
            return
        if target.student_id != event.student_id:
            _skip(event, "student_mismatch")
            return
        if target.activity_type != INTERNSHIP_TYPE:
            _skip(event, "not_internship")
            return
        mentor_id = str(event.payload.get("mentor_id") or "").strip()
        if not mentor_id:
            _skip(event, "missing_mentor")
            return
        if target.active_confirm_event_id is not None:
            if target.active_mentor_id == mentor_id:
                _skip(event, "duplicate_confirm")
            else:
                _skip(event, "already_confirmed")
            return
        target.active_confirm_event_id = event.event_id
        target.active_mentor_id = mentor_id

        delegation = _match_delegation(
            delegations, event.student_id, mentor_id, target.activity_id
        )
        for day, seg_start, seg_end in split_by_academic_day(
            target.start_utc, target.end_utc, timezone_name
        ):
            seconds = elapsed_seconds(seg_start, seg_end)
            if seconds <= 0:
                continue
            day_iso = day.isoformat()
            period = day_iso[:7]
            if delegation is None:
                entries.append(
                    WorkloadEntry(
                        entry_id=f"{event.event_id}:{day_iso}:{ROLE_PRIMARY}",
                        role=ROLE_PRIMARY,
                        mentor_id=mentor_id,
                        student_id=event.student_id,
                        checkin_event_id=target.event_id,
                        confirm_event_id=event.event_id,
                        activity_id=target.activity_id,
                        academic_day=day_iso,
                        period=period,
                        seconds=seconds,
                        share_bps=10000,
                        delegation_id="",
                    )
                )
            else:
                delegator_seconds = seconds * delegation.share_bps // 10000
                entries.append(
                    WorkloadEntry(
                        entry_id=f"{event.event_id}:{day_iso}:{ROLE_DELEGATOR}",
                        role=ROLE_DELEGATOR,
                        mentor_id=delegation.from_mentor_id,
                        student_id=event.student_id,
                        checkin_event_id=target.event_id,
                        confirm_event_id=event.event_id,
                        activity_id=target.activity_id,
                        academic_day=day_iso,
                        period=period,
                        seconds=delegator_seconds,
                        share_bps=delegation.share_bps,
                        delegation_id=delegation.delegation_id,
                    )
                )
                entries.append(
                    WorkloadEntry(
                        entry_id=f"{event.event_id}:{day_iso}:{ROLE_DELEGATE}",
                        role=ROLE_DELEGATE,
                        mentor_id=mentor_id,
                        student_id=event.student_id,
                        checkin_event_id=target.event_id,
                        confirm_event_id=event.event_id,
                        activity_id=target.activity_id,
                        academic_day=day_iso,
                        period=period,
                        seconds=seconds - delegator_seconds,
                        share_bps=10000 - delegation.share_bps,
                        delegation_id=delegation.delegation_id,
                    )
                )

    def _handle_revoke(event: Event) -> None:
        target = checkins.get(event.payload.get("checkin_event_id") or "")
        if target is None:
            _skip(event, "unknown_checkin")
            return
        if target.student_id != event.student_id:
            _skip(event, "student_mismatch")
            return
        if target.active_confirm_event_id is None:
            _skip(event, "nothing_to_revoke")
            return
        mentor_id = str(event.payload.get("mentor_id") or "").strip()
        if mentor_id and mentor_id != target.active_mentor_id:
            _skip(event, "mentor_mismatch")
            return
        revoked_confirm_id = target.active_confirm_event_id
        entries[:] = [
            e for e in entries if e.confirm_event_id != revoked_confirm_id
        ]
        target.active_confirm_event_id = None
        target.active_mentor_id = None

    def _handle_delegate(event: Event) -> None:
        payload = event.payload
        delegation_id = str(payload.get("delegation_id") or "").strip()
        from_mentor = str(payload.get("from_mentor_id") or "").strip()
        to_mentor = str(payload.get("to_mentor_id") or "").strip()
        if not delegation_id or not from_mentor or not to_mentor:
            _skip(event, "invalid_delegation")
            return
        if from_mentor == to_mentor:
            _skip(event, "self_delegation")
            return
        if delegation_id in delegations:
            _skip(event, "duplicate_delegation")
            return
        raw_share = payload.get("share_bps")
        if raw_share is None:
            share_bps = rule.delegator_share_bps
        else:
            try:
                share_bps = int(raw_share)
            except (TypeError, ValueError):
                _skip(event, "invalid_share_bps")
                return
            if not 0 <= share_bps <= 10000:
                _skip(event, "invalid_share_bps")
                return
        activity_id = str(payload.get("activity_id") or "").strip() or None
        delegations[delegation_id] = _Delegation(
            delegation_id=delegation_id,
            student_id=event.student_id,
            from_mentor_id=from_mentor,
            to_mentor_id=to_mentor,
            share_bps=share_bps,
            activity_id=activity_id,
            registered_event_id=event.event_id,
        )

    def _handle_delegate_revoke(event: Event) -> None:
        delegation = delegations.get(event.payload.get("delegation_id") or "")
        if delegation is None:
            _skip(event, "unknown_delegation")
            return
        if delegation.student_id != event.student_id:
            _skip(event, "student_mismatch")
            return
        if not delegation.active:
            _skip(event, "delegation_already_revoked")
            return
        delegation.active = False

    for event in sorted_events:
        if event.event_type == EventType.CHECKIN:
            _handle_checkin(event)
        elif event.event_type == EventType.MENTOR_CONFIRM:
            _handle_confirm(event)
        elif event.event_type == EventType.MENTOR_CONFIRM_REVOKE:
            _handle_revoke(event)
        elif event.event_type == EventType.MENTOR_DELEGATE:
            _handle_delegate(event)
        elif event.event_type == EventType.MENTOR_DELEGATE_REVOKE:
            _handle_delegate_revoke(event)

    entries.sort(key=lambda e: e.entry_id)
    return entries, skipped


def summarize_mentors(
    entries: Iterable[dict[str, Any]],
    adjustments: Iterable[dict[str, Any]] = (),
    *,
    seconds_per_unit: int = DEFAULT_SECONDS_PER_UNIT,
) -> list[dict[str, Any]]:
    """执行确定性的业务处理。"""
    entries_by_mentor: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        entries_by_mentor.setdefault(entry["mentor_id"], []).append(entry)
    adjustment_by_mentor: dict[str, int] = {}
    for adjustment in adjustments:
        mentor_id = adjustment["mentor_id"]
        adjustment_by_mentor[mentor_id] = adjustment_by_mentor.get(
            mentor_id, 0
        ) + int(adjustment["seconds"])

    summaries: list[dict[str, Any]] = []
    for mentor_id in sorted(set(entries_by_mentor) | set(adjustment_by_mentor)):
        mine = entries_by_mentor.get(mentor_id, [])
        confirmed_seconds = sum(e["seconds"] for e in mine)
        periods: dict[str, int] = {}
        for entry in mine:
            periods[entry["period"]] = (
                periods.get(entry["period"], 0) + entry["seconds"]
            )
        adjustment_seconds = adjustment_by_mentor.get(mentor_id, 0)
        total_seconds = confirmed_seconds + adjustment_seconds
        if total_seconds < 0:
            total_seconds = 0
        summaries.append(
            {
                "mentor_id": mentor_id,
                "valid_confirmations": len(
                    {
                        e["checkin_event_id"]
                        for e in mine
                        if e["role"] in _ACTING_ROLES
                    }
                ),
                "confirmed_seconds": confirmed_seconds,
                "primary_seconds": sum(
                    e["seconds"] for e in mine if e["role"] == ROLE_PRIMARY
                ),
                "delegate_seconds": sum(
                    e["seconds"] for e in mine if e["role"] == ROLE_DELEGATE
                ),
                "delegator_seconds": sum(
                    e["seconds"] for e in mine if e["role"] == ROLE_DELEGATOR
                ),
                "periods": dict(sorted(periods.items())),
                "units": confirmed_seconds // seconds_per_unit,
                "adjustment_seconds": adjustment_seconds,
                "total_seconds": total_seconds,
                "total_units": total_seconds // seconds_per_unit,
            }
        )
    return summaries


def diff_entries(
    stored: Iterable[dict[str, Any]], recomputed: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """执行确定性的业务处理。"""
    stored_by_id = {e["entry_id"]: e for e in stored}
    recomputed_by_id = {e["entry_id"]: e for e in recomputed}
    differences: list[dict[str, Any]] = []
    for entry_id in sorted(set(stored_by_id) | set(recomputed_by_id)):
        before = stored_by_id.get(entry_id)
        after = recomputed_by_id.get(entry_id)
        if before is None:
            differences.append(
                {"entry_id": entry_id, "change": "missing_in_batch"}
            )
        elif after is None:
            differences.append(
                {"entry_id": entry_id, "change": "missing_in_replay"}
            )
        elif before != after:
            changed = {
                key: {"stored": before.get(key), "recomputed": after.get(key)}
                for key in sorted(set(before) | set(after))
                if before.get(key) != after.get(key)
            }
            differences.append(
                {"entry_id": entry_id, "change": "modified", "fields": changed}
            )
    return differences

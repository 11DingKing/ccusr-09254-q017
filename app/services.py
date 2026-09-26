"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .core.workload import (
    SettlementRule,
    build_accruals,
    diff_entries,
    summarize_mentors,
)
from .repository import (
    get_batch,
    get_freeze,
    get_plan,
    get_rule,
    insert_adjustment,
    insert_batch,
    insert_events,
    insert_freeze,
    list_adjustments,
    load_events,
    load_events_up_to,
    max_event_id,
    upsert_plan,
    upsert_rule,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


class BatchNotFoundError(Exception):
    pass


def _iso_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def get_plan_plain(db: Session, plan_version: str) -> dict[str, Any] | None:
    plan = get_plan(db, plan_version)
    if plan is None:
        return None
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def ensure_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> dict[str, Any]:
    plan = upsert_plan(
        db,
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def import_events(
    db: Session, *, plan_version: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    accepted, duplicates = insert_events(
        db, plan_version=plan_version, events=events
    )
    return {
        "accepted": len(accepted),
        "duplicates": duplicates,
        "rejected": [],
    }


def current_snapshot(db: Session, plan_version: str) -> Snapshot:
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
    )


def student_progress(
    db: Session, plan_version: str, student_id: str
) -> dict[str, Any] | None:
    snap = current_snapshot(db, plan_version)
    return explain_student(snap, student_id)


def freeze_semester(
    db: Session, *, plan_version: str, freeze_id: str
) -> tuple[Snapshot, bool]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    existing = get_freeze(db, plan_version, freeze_id)
    if existing is not None:
        return Snapshot.from_dict(existing.snapshot), False

    cutoff = max_event_id(db, plan_version)
    events = load_events(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
    )
    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff,
    )
    if row is None:
        existing = get_freeze(db, plan_version, freeze_id)
        assert existing is not None
        return Snapshot.from_dict(existing.snapshot), False
    return snap, True


def get_frozen_snapshot(
    db: Session, plan_version: str, freeze_id: str
) -> Snapshot:
    _require_plan(db, plan_version)
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise FreezeNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    return Snapshot.from_dict(row.snapshot)


def explain_frozen_student(
    db: Session, plan_version: str, freeze_id: str, student_id: str
) -> dict[str, Any] | None:
    snap = get_frozen_snapshot(db, plan_version, freeze_id)
    return explain_student(snap, student_id)


def diff_freezes(
    db: Session, plan_version: str, old_freeze_id: str, new_freeze_id: str
) -> dict[str, Any]:
    old = get_frozen_snapshot(db, plan_version, old_freeze_id)
    new = get_frozen_snapshot(db, plan_version, new_freeze_id)
    return diff_snapshots(old, new)


def _rule_view(db: Session, plan_version: str) -> tuple[SettlementRule, bool, str | None]:
    row = get_rule(db, plan_version)
    if row is None:
        return SettlementRule(), True, None
    return (
        SettlementRule(
            seconds_per_unit=row.seconds_per_unit,
            delegator_share_bps=row.delegator_share_bps,
        ),
        False,
        _iso_z(row.updated_at),
    )


def ensure_settlement_rule(
    db: Session,
    *,
    plan_version: str,
    seconds_per_unit: int,
    delegator_share_bps: int,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    row = upsert_rule(
        db,
        plan_version=plan_version,
        seconds_per_unit=seconds_per_unit,
        delegator_share_bps=delegator_share_bps,
    )
    return {
        "plan_version": row.plan_version,
        "seconds_per_unit": row.seconds_per_unit,
        "delegator_share_bps": row.delegator_share_bps,
        "is_default": False,
        "updated_at": _iso_z(row.updated_at),
    }


def get_settlement_rule(db: Session, plan_version: str) -> dict[str, Any]:
    _require_plan(db, plan_version)
    rule, is_default, updated_at = _rule_view(db, plan_version)
    return {
        "plan_version": plan_version,
        "seconds_per_unit": rule.seconds_per_unit,
        "delegator_share_bps": rule.delegator_share_bps,
        "is_default": is_default,
        "updated_at": updated_at,
    }


def _build_preview(
    plan: Any,
    events: list[Any],
    rule: SettlementRule,
    *,
    up_to_event_id: str | None,
) -> dict[str, Any]:
    entries, skipped = build_accruals(
        events,
        plan_version=plan.plan_version,
        timezone_name=plan.iana_timezone,
        rule=rule,
        up_to_event_id=up_to_event_id,
    )
    entry_dicts = [e.to_dict() for e in entries]
    return {
        "plan_version": plan.plan_version,
        "generated_at": _iso_z(datetime.now(timezone.utc)),
        "event_cutoff_id": up_to_event_id,
        "rule": rule.to_dict(),
        "entries": entry_dicts,
        "mentors": summarize_mentors(
            entry_dicts, (), seconds_per_unit=rule.seconds_per_unit
        ),
        "skipped": skipped,
    }


def preview_workload(db: Session, plan_version: str) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    rule, _, _ = _rule_view(db, plan_version)
    cutoff = max_event_id(db, plan_version)
    events = load_events(db, plan_version) if cutoff is not None else []
    return _build_preview(plan, events, rule, up_to_event_id=cutoff)


def _adjustment_dict(row: Any) -> dict[str, Any]:
    return {
        "adjustment_id": row.adjustment_id,
        "mentor_id": row.mentor_id,
        "seconds": row.seconds,
        "reason": row.reason,
        "actor": row.actor,
        "created_at": _iso_z(row.created_at),
    }


def _batch_view(db: Session, row: Any) -> dict[str, Any]:
    snapshot = dict(row.snapshot)
    rule = snapshot["rule"]
    adjustments = [
        _adjustment_dict(a)
        for a in list_adjustments(db, row.plan_version, row.batch_id)
    ]
    return {
        "plan_version": row.plan_version,
        "batch_id": row.batch_id,
        "note": row.note,
        "issued_at": _iso_z(row.issued_at),
        "event_cutoff_id": row.event_cutoff_id,
        "generated_at": snapshot["generated_at"],
        "rule": rule,
        "entries": snapshot["entries"],
        "mentors": summarize_mentors(
            snapshot["entries"],
            adjustments,
            seconds_per_unit=rule["seconds_per_unit"],
        ),
        "adjustments": adjustments,
        "skipped": snapshot["skipped"],
    }


def issue_workload_batch(
    db: Session, *, plan_version: str, batch_id: str, note: str = ""
) -> tuple[dict[str, Any], bool]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    existing = get_batch(db, plan_version, batch_id)
    if existing is not None:
        return _batch_view(db, existing), False

    rule, _, _ = _rule_view(db, plan_version)
    cutoff = max_event_id(db, plan_version)
    events = load_events(db, plan_version) if cutoff is not None else []
    preview = _build_preview(plan, events, rule, up_to_event_id=cutoff)
    snapshot = {
        "generated_at": preview["generated_at"],
        "rule": preview["rule"],
        "entries": preview["entries"],
        "skipped": preview["skipped"],
    }
    row = insert_batch(
        db,
        plan_version=plan_version,
        batch_id=batch_id,
        snapshot=snapshot,
        event_cutoff_id=cutoff,
        note=note,
    )
    if row is None:
        existing = get_batch(db, plan_version, batch_id)
        assert existing is not None
        return _batch_view(db, existing), False
    return _batch_view(db, row), True


def get_workload_batch(
    db: Session, plan_version: str, batch_id: str
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    row = get_batch(db, plan_version, batch_id)
    if row is None:
        raise BatchNotFoundError(
            f"batch '{batch_id}' for plan '{plan_version}' does not exist"
        )
    return _batch_view(db, row)


def append_workload_adjustment(
    db: Session,
    *,
    plan_version: str,
    batch_id: str,
    adjustment_id: str,
    mentor_id: str,
    seconds: int,
    reason: str,
    actor: str,
) -> tuple[dict[str, Any], bool]:
    """执行确定性的业务处理。"""
    _require_plan(db, plan_version)
    if get_batch(db, plan_version, batch_id) is None:
        raise BatchNotFoundError(
            f"batch '{batch_id}' for plan '{plan_version}' does not exist"
        )
    row, created = insert_adjustment(
        db,
        plan_version=plan_version,
        batch_id=batch_id,
        adjustment_id=adjustment_id,
        mentor_id=mentor_id,
        seconds=seconds,
        reason=reason,
        actor=actor,
    )
    return _adjustment_dict(row), created


def mentor_workload(
    db: Session, plan_version: str, mentor_id: str, batch_id: str | None = None
) -> dict[str, Any] | None:
    """执行确定性的业务处理。"""
    _require_plan(db, plan_version)
    if batch_id is None:
        view = preview_workload(db, plan_version)
        source = "preview"
    else:
        view = get_workload_batch(db, plan_version, batch_id)
        source = "batch"
    entries = [e for e in view["entries"] if e["mentor_id"] == mentor_id]
    adjustments = [a for a in view["adjustments"] if a["mentor_id"] == mentor_id] if source == "batch" else []
    if not entries and not adjustments:
        return None
    summaries = summarize_mentors(
        entries, adjustments, seconds_per_unit=view["rule"]["seconds_per_unit"]
    )
    return {
        "plan_version": plan_version,
        "mentor_id": mentor_id,
        "source": source,
        "batch_id": batch_id,
        "summary": summaries[0],
        "entries": entries,
        "adjustments": adjustments,
    }


def reconcile_workload_batch(
    db: Session, plan_version: str, batch_id: str
) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    row = get_batch(db, plan_version, batch_id)
    if row is None:
        raise BatchNotFoundError(
            f"batch '{batch_id}' for plan '{plan_version}' does not exist"
        )
    snapshot = dict(row.snapshot)
    rule = SettlementRule.from_dict(snapshot["rule"])
    cutoff = row.event_cutoff_id
    events = load_events(db, plan_version) if cutoff is not None else []
    entries, _ = build_accruals(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        rule=rule,
        up_to_event_id=cutoff,
    )
    recomputed = [e.to_dict() for e in entries]
    differences = diff_entries(snapshot["entries"], recomputed)
    return {
        "plan_version": plan_version,
        "batch_id": batch_id,
        "consistent": not differences,
        "differences": differences,
    }

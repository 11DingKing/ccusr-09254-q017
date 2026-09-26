"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import Event as EventModel
from .models import (
    Freeze,
    Plan,
    SettlementRule,
    WorkloadAdjustment,
    WorkloadBatch,
)


def get_plan(db: Session, plan_version: str) -> Plan | None:
    return db.get(Plan, plan_version)


def upsert_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> Plan:
    stmt = sqlite_insert(Plan).values(
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "iana_timezone": iana_timezone,
            "required_seconds": required_seconds,
        },
    )
    db.execute(stmt)
    db.commit()
    plan = db.get(Plan, plan_version)
    assert plan is not None
    return plan


def _to_core_event(row: EventModel) -> CoreEvent:
    return CoreEvent(
        event_id=row.event_id,
        plan_version=row.plan_version,
        event_type=EventType(row.event_type),
        student_id=row.student_id,
        payload=dict(row.payload),
        created_at=row.created_at,
    )


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """执行确定性的业务处理。"""
    accepted: list[str] = []
    duplicates: list[str] = []
    for e in events:
        stmt = sqlite_insert(EventModel).values(
            event_id=e["event_id"],
            plan_version=plan_version,
            student_id=e["student_id"],
            event_type=e["event_type"],
            payload=e["payload"],
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["event_id", "plan_version"]
        ).returning(EventModel.id)
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            accepted.append(e["event_id"])
        else:
            duplicates.append(e["event_id"])
    db.commit()
    return accepted, duplicates


def load_events(db: Session, plan_version: str) -> list[CoreEvent]:
    stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def load_events_up_to(
    db: Session, plan_version: str, max_event_id: str
) -> list[CoreEvent]:
    """执行确定性的业务处理。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id <= max_event_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def max_event_id(db: Session, plan_version: str) -> str | None:
    stmt = (
        select(EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.event_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> Freeze | None:
    return db.get(Freeze, (plan_version, freeze_id))


def insert_freeze(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
) -> Freeze | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Freeze).values(
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "freeze_id"]
    ).returning(Freeze.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None


def get_rule(db: Session, plan_version: str) -> SettlementRule | None:
    return db.get(SettlementRule, plan_version)


def upsert_rule(
    db: Session,
    *,
    plan_version: str,
    seconds_per_unit: int,
    delegator_share_bps: int,
) -> SettlementRule:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(SettlementRule).values(
        plan_version=plan_version,
        seconds_per_unit=seconds_per_unit,
        delegator_share_bps=delegator_share_bps,
        updated_at=datetime.now(timezone.utc),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "seconds_per_unit": seconds_per_unit,
            "delegator_share_bps": delegator_share_bps,
            "updated_at": datetime.now(timezone.utc),
        },
    )
    db.execute(stmt)
    db.commit()
    rule = db.get(SettlementRule, plan_version)
    assert rule is not None
    return rule


def get_batch(db: Session, plan_version: str, batch_id: str) -> WorkloadBatch | None:
    return db.get(WorkloadBatch, (plan_version, batch_id))


def insert_batch(
    db: Session,
    *,
    plan_version: str,
    batch_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
    note: str,
) -> WorkloadBatch | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(WorkloadBatch).values(
        plan_version=plan_version,
        batch_id=batch_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
        note=note,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "batch_id"]
    ).returning(WorkloadBatch.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(WorkloadBatch, (plan_version, batch_id))
    return None


def list_adjustments(
    db: Session, plan_version: str, batch_id: str
) -> list[WorkloadAdjustment]:
    stmt = (
        select(WorkloadAdjustment)
        .where(WorkloadAdjustment.plan_version == plan_version)
        .where(WorkloadAdjustment.batch_id == batch_id)
        .order_by(WorkloadAdjustment.created_at, WorkloadAdjustment.adjustment_id)
    )
    return list(db.execute(stmt).scalars().all())


def insert_adjustment(
    db: Session,
    *,
    plan_version: str,
    batch_id: str,
    adjustment_id: str,
    mentor_id: str,
    seconds: int,
    reason: str,
    actor: str,
) -> tuple[WorkloadAdjustment, bool]:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(WorkloadAdjustment).values(
        plan_version=plan_version,
        batch_id=batch_id,
        adjustment_id=adjustment_id,
        mentor_id=mentor_id,
        seconds=seconds,
        reason=reason,
        actor=actor,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "batch_id", "adjustment_id"]
    ).returning(WorkloadAdjustment.adjustment_id)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    row = db.get(WorkloadAdjustment, (plan_version, batch_id, adjustment_id))
    assert row is not None
    return row, inserted is not None

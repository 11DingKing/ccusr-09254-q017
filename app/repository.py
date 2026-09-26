"""服务端业务模块。"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import Event as EventModel
from .models import (
    Freeze,
    Plan,
    SettlementAdjustment,
    SettlementBatch,
    SettlementEntry,
    SettlementRule,
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


def _is_busy_error(exc: OperationalError) -> bool:
    message = str(getattr(exc, "orig", exc)).lower()
    return "database is locked" in message or "database table is locked" in message


def _retry_sqlite_busy(operation, *, attempts: int = 8, base_delay: float = 0.01):
    """重试 SQLite 写-写并发产生的 SQLITE_BUSY（立即死锁不会等待 busy_timeout）。

    操作必须幂等（本仓储的写入均带 ON CONFLICT 语义）；命中 busy 时回滚后
    指数退避重试。生产数据库（PostgreSQL 等）不会走到这里。
    """
    last_exc: OperationalError | None = None
    for index in range(attempts):
        try:
            return operation()
        except OperationalError as exc:
            if not _is_busy_error(exc) or index == attempts - 1:
                raise
            last_exc = exc
            time.sleep(base_delay * (2 ** index))
    assert last_exc is not None
    raise last_exc


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """执行确定性的业务处理。"""

    def _attempt() -> tuple[list[str], list[str]]:
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

    def _rolled_back_attempt() -> tuple[list[str], list[str]]:
        try:
            return _attempt()
        except OperationalError:
            db.rollback()
            raise

    return _retry_sqlite_busy(_rolled_back_attempt)


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

    def _attempt() -> str | None:
        inserted = db.execute(stmt).scalar_one_or_none()
        db.commit()
        return inserted

    def _rolled_back_attempt() -> str | None:
        try:
            return _attempt()
        except OperationalError:
            db.rollback()
            raise

    inserted = _retry_sqlite_busy(_rolled_back_attempt)
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None


# ---------------------------------------------------------------------------
# 导师工作量结算
# ---------------------------------------------------------------------------


def get_settlement_rule(db: Session, plan_version: str) -> SettlementRule | None:
    return db.get(SettlementRule, plan_version)


def upsert_settlement_rule(
    db: Session,
    *,
    plan_version: str,
    delegation_primary_share_bp: int,
    delegation_secondary_share_bp: int,
) -> SettlementRule:
    stmt = sqlite_insert(SettlementRule).values(
        plan_version=plan_version,
        delegation_primary_share_bp=delegation_primary_share_bp,
        delegation_secondary_share_bp=delegation_secondary_share_bp,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "delegation_primary_share_bp": delegation_primary_share_bp,
            "delegation_secondary_share_bp": delegation_secondary_share_bp,
        },
    )
    db.execute(stmt)
    db.commit()
    row = db.get(SettlementRule, plan_version)
    assert row is not None
    return row


def get_settlement_batch(
    db: Session, plan_version: str, batch_id: str
) -> SettlementBatch | None:
    return db.get(SettlementBatch, (plan_version, batch_id))


def list_settlement_batches(db: Session, plan_version: str) -> list[SettlementBatch]:
    stmt = (
        select(SettlementBatch)
        .where(SettlementBatch.plan_version == plan_version)
        .order_by(SettlementBatch.created_at, SettlementBatch.batch_id)
    )
    return list(db.execute(stmt).scalars().all())


def latest_settlement_batch(
    db: Session, plan_version: str
) -> SettlementBatch | None:
    stmt = (
        select(SettlementBatch)
        .where(SettlementBatch.plan_version == plan_version)
        .order_by(SettlementBatch.created_at.desc(), SettlementBatch.batch_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def settled_checkin_mentor_pairs(db: Session, plan_version: str) -> set[tuple[str, str, str]]:
    """已在任何批次中结算过的 (打卡, 导师, 月份) 组合，用于签发时排除。"""
    stmt = (
        select(
            SettlementEntry.checkin_event_id,
            SettlementEntry.mentor_id,
            SettlementEntry.period,
        )
        .where(SettlementEntry.plan_version == plan_version)
    )
    return {(cid, mid, period) for cid, mid, period in db.execute(stmt).all()}


def insert_settlement_batch(
    db: Session,
    *,
    plan_version: str,
    batch_id: str,
    period: str,
    rules_snapshot: dict[str, Any],
    event_cutoff_id: str | None,
    line_count: int,
    total_seconds: int,
    prev_hash: str | None,
    entry_hash: str,
) -> SettlementBatch | None:
    """插入批次主记录（不提交，事务由调用方统一管理）。

    同 (plan, batch_id) 冲突时返回 None，但不回滚——签发关键区已在
    BEGIN IMMEDIATE 下提前查重并串行化，这里只是幂等兜底。
    """
    stmt = sqlite_insert(SettlementBatch).values(
        plan_version=plan_version,
        batch_id=batch_id,
        period=period,
        status="frozen",
        rules_snapshot=rules_snapshot,
        event_cutoff_id=event_cutoff_id,
        line_count=line_count,
        total_seconds=total_seconds,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    ).on_conflict_do_nothing(
        index_elements=["plan_version", "batch_id"]
    ).returning(SettlementBatch.batch_id)
    inserted = db.execute(stmt).scalar_one_or_none()
    if inserted is None:
        return None
    row = db.get(SettlementBatch, (plan_version, batch_id))
    assert row is not None
    return row


def insert_settlement_entries(
    db: Session,
    *,
    plan_version: str,
    batch_id: str,
    period: str,
    lines: list[dict[str, Any]],
) -> int:
    """批量插入分录（不提交）。调用方负责 commit/rollback 整个批次事务。"""
    if not lines:
        return 0
    rows = [
        {
            "plan_version": plan_version,
            "batch_id": batch_id,
            "period": period,
            "mentor_id": line["mentor_id"],
            "student_id": line["student_id"],
            "checkin_event_id": line["checkin_event_id"],
            "confirm_event_id": line["confirm_event_id"],
            "weight_bp": line["weight_bp"],
            "seconds": line["seconds"],
            "counts_raw": line["counts_raw"],
        }
        for line in lines
    ]
    db.execute(sqlite_insert(SettlementEntry), rows)
    return len(rows)


def list_settlement_entries(
    db: Session, plan_version: str, batch_id: str
) -> list[SettlementEntry]:
    stmt = (
        select(SettlementEntry)
        .where(SettlementEntry.plan_version == plan_version)
        .where(SettlementEntry.batch_id == batch_id)
        .order_by(
            SettlementEntry.mentor_id,
            SettlementEntry.checkin_event_id,
            SettlementEntry.confirm_event_id,
        )
    )
    return list(db.execute(stmt).scalars().all())


def insert_adjustment(
    db: Session,
    *,
    plan_version: str,
    batch_id: str,
    adjustment_id: str,
    mentor_id: str,
    checkin_event_id: str,
    weight_bp: int,
    delta_seconds: int,
    reason: str,
    created_by: str,
) -> SettlementAdjustment | None:
    stmt = sqlite_insert(SettlementAdjustment).values(
        plan_version=plan_version,
        batch_id=batch_id,
        adjustment_id=adjustment_id,
        mentor_id=mentor_id,
        checkin_event_id=checkin_event_id,
        weight_bp=weight_bp,
        delta_seconds=delta_seconds,
        reason=reason,
        created_by=created_by,
    ).on_conflict_do_nothing(
        index_elements=["plan_version", "batch_id", "adjustment_id"]
    ).returning(SettlementAdjustment.id)
    def _attempt() -> int | None:
        inserted = db.execute(stmt).scalar_one_or_none()
        if inserted is None:
            db.rollback()
            return None
        db.commit()
        return inserted

    def _rolled_back_attempt() -> int | None:
        try:
            return _attempt()
        except OperationalError:
            db.rollback()
            raise

    inserted = _retry_sqlite_busy(_rolled_back_attempt)
    if inserted is None:
        return None
    return db.get(SettlementAdjustment, inserted)


def list_adjustments(
    db: Session, plan_version: str, batch_id: str
) -> list[SettlementAdjustment]:
    stmt = (
        select(SettlementAdjustment)
        .where(SettlementAdjustment.plan_version == plan_version)
        .where(SettlementAdjustment.batch_id == batch_id)
        .order_by(SettlementAdjustment.id)
    )
    return list(db.execute(stmt).scalars().all())


def list_adjustments_for_mentor(
    db: Session, plan_version: str, mentor_id: str
) -> list[SettlementAdjustment]:
    stmt = (
        select(SettlementAdjustment)
        .where(SettlementAdjustment.plan_version == plan_version)
        .where(SettlementAdjustment.mentor_id == mentor_id)
        .order_by(SettlementAdjustment.id)
    )
    return list(db.execute(stmt).scalars().all())


def list_entries_for_mentor(
    db: Session, plan_version: str, mentor_id: str
) -> list[SettlementEntry]:
    stmt = (
        select(SettlementEntry)
        .where(SettlementEntry.plan_version == plan_version)
        .where(SettlementEntry.mentor_id == mentor_id)
        .order_by(SettlementEntry.period, SettlementEntry.batch_id, SettlementEntry.id)
    )
    return list(db.execute(stmt).scalars().all())

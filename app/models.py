"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Plan(Base):
    __tablename__ = "plans"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    iana_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    required_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("required_seconds >= 0", name="ck_plans_required_nonneg"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("event_id", "plan_version", name="uq_events_event_id_plan"),
        Index("ix_events_plan_student", "plan_version", "student_id"),
    )


class Freeze(Base):
    __tablename__ = "freezes"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    freeze_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    event_cutoff_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class SettlementRule(Base):
    """导师工作量结算规则（每培养方案一行），签发批次后冻结参数快照。"""

    __tablename__ = "settlement_rules"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    delegation_primary_share_bp: Mapped[int] = mapped_column(Integer, nullable=False)
    delegation_secondary_share_bp: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "delegation_primary_share_bp >= 0 "
            "AND delegation_secondary_share_bp >= 0 "
            "AND delegation_primary_share_bp + delegation_secondary_share_bp = 10000",
            name="ck_settlement_rules_shares",
        ),
    )


class SettlementBatch(Base):
    """不可变结算批次。封存后规则参数、截止事件与哈希链均不可变。"""

    __tablename__ = "settlement_batches"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    period: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="frozen")
    rules_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    event_cutoff_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("status IN ('frozen')", name="ck_settlement_batches_status"),
        Index("ix_settlement_batches_plan_seq", "plan_version", "created_at"),
    )


class SettlementEntry(Base):
    """批次内的工作量归属分录，封存后只读。"""

    __tablename__ = "settlement_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(128), nullable=False)
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    mentor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False)
    checkin_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    confirm_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    weight_bp: Mapped[int] = mapped_column(Integer, nullable=False)
    seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    counts_raw: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "plan_version",
            "batch_id",
            "mentor_id",
            "checkin_event_id",
            "confirm_event_id",
            name="uq_settlement_entries_identity",
        ),
        # 数据库级保障：同一(打卡,导师,月份)在任何批次中只能结算一次，并发签发也不会双算。
        UniqueConstraint(
            "plan_version",
            "checkin_event_id",
            "mentor_id",
            "period",
            name="uq_settlement_entries_settled_once",
        ),
        Index("ix_settlement_entries_mentor", "plan_version", "mentor_id", "period"),
    )


class SettlementAdjustment(Base):
    """冻结批次的纠错凭证：只追加调整分录，不改动历史。"""

    __tablename__ = "settlement_adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(128), nullable=False)
    adjustment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mentor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    checkin_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    weight_bp: Mapped[int] = mapped_column(Integer, nullable=False, default=10000)
    delta_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "plan_version", "batch_id", "adjustment_id",
            name="uq_settlement_adjustments_id",
        ),
        CheckConstraint("reason <> ''", name="ck_settlement_adjustments_reason"),
        Index("ix_settlement_adjustments_mentor", "plan_version", "mentor_id"),
    )

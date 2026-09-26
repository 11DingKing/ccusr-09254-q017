"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class MentorConfirmPayload(BaseModel):
    checkin_event_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal[
        "checkin",
        "mentor_confirm",
        "mentor_confirm_revoke",
        "mentor_delegate",
        "mentor_delegate_revoke",
        "leave_correction",
    ]
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventOut(BaseModel):
    event_id: str
    plan_version: str
    event_type: str
    student_id: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    academic_days: list[dict[str, Any]]


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str


class StudentProgressOut(BaseModel):
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]


class FreezeIn(BaseModel):
    pass


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    student_changes: list[dict[str, Any]]
    students_affected: int


class SettlementRuleIn(BaseModel):
    seconds_per_unit: int = Field(2700, gt=0)
    delegator_share_bps: int = Field(2000, ge=0, le=10000)


class SettlementRuleOut(BaseModel):
    plan_version: str
    seconds_per_unit: int
    delegator_share_bps: int
    is_default: bool
    updated_at: str | None


class WorkloadPreviewOut(BaseModel):
    plan_version: str
    generated_at: str
    event_cutoff_id: str | None
    rule: dict[str, Any]
    entries: list[dict[str, Any]]
    mentors: list[dict[str, Any]]
    skipped: list[dict[str, Any]]


class BatchIssueIn(BaseModel):
    note: str = Field("", max_length=512)


class WorkloadBatchOut(BaseModel):
    plan_version: str
    batch_id: str
    note: str
    issued_at: str
    generated_at: str
    event_cutoff_id: str | None
    rule: dict[str, Any]
    entries: list[dict[str, Any]]
    mentors: list[dict[str, Any]]
    adjustments: list[dict[str, Any]]
    skipped: list[dict[str, Any]]


class AdjustmentIn(BaseModel):
    adjustment_id: str = Field(..., min_length=1, max_length=128)
    mentor_id: str = Field(..., min_length=1, max_length=128)
    seconds: int
    reason: str = Field("", max_length=512)
    actor: str = Field("", max_length=128)


class AdjustmentResult(BaseModel):
    adjustment_id: str
    mentor_id: str
    seconds: int
    reason: str
    actor: str
    created_at: str
    created: bool


class ReconcileOut(BaseModel):
    plan_version: str
    batch_id: str
    consistent: bool
    differences: list[dict[str, Any]]


class MentorWorkloadOut(BaseModel):
    plan_version: str
    mentor_id: str
    source: str
    batch_id: str | None
    summary: dict[str, Any]
    entries: list[dict[str, Any]]
    adjustments: list[dict[str, Any]]

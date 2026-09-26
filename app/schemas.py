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


class MentorDelegatePayload(BaseModel):
    primary_mentor_id: str
    delegate_mentor_id: str
    revoked: bool = False


class MentorConfirmRevokePayload(BaseModel):
    checkin_event_id: str
    mentor_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal[
        "checkin",
        "mentor_confirm",
        "leave_correction",
        "mentor_delegate",
        "mentor_confirm_revoke",
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


# ---------------------------------------------------------------------------
# 导师工作量结算
# ---------------------------------------------------------------------------


class SettlementRulesIn(BaseModel):
    delegation_primary_share_bp: int = Field(6000, ge=0, le=10000)
    delegation_secondary_share_bp: int = Field(4000, ge=0, le=10000)

    @model_validator(mode="after")
    def _shares_sum_to_whole(self) -> "SettlementRulesIn":
        if (
            self.delegation_primary_share_bp
            + self.delegation_secondary_share_bp
            != 10000
        ):
            raise ValueError("delegation shares must sum to 10000 basis points")
        return self


class SettlementRulesOut(BaseModel):
    delegation_primary_share_bp: int
    delegation_secondary_share_bp: int


class SettlementBatchIn(BaseModel):
    period: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    event_cutoff_id: str | None = None


class SettlementPreviewIn(BaseModel):
    period: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    event_cutoff_id: str | None = None


class SettlementAdjustmentIn(BaseModel):
    adjustment_id: str = Field(..., min_length=1, max_length=128)
    mentor_id: str = Field(..., min_length=1, max_length=128)
    checkin_event_id: str = Field(..., min_length=1, max_length=128)
    delta_seconds: int
    weight_bp: int = Field(10000, ge=0, le=10000)
    reason: str = Field(..., min_length=1, max_length=512)
    created_by: str = Field(..., min_length=1, max_length=128)

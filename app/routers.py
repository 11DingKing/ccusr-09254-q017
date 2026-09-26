"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from . import services, settlement_service
from .db import get_db
from .schemas import (
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    PlanIn,
    PlanOut,
    SettlementAdjustmentIn,
    SettlementBatchIn,
    SettlementPreviewIn,
    SettlementRulesIn,
    SettlementRulesOut,
    SnapshotOut,
    StudentProgressOut,
)

router = APIRouter(prefix="/api")


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(body: PlanIn, db: Session = Depends(get_db)) -> Any:
    return services.ensure_plan(
        db,
        plan_version=body.plan_version,
        iana_timezone=body.iana_timezone,
        required_seconds=body.required_seconds,
    )


@router.get("/plans/{plan_version}", response_model=PlanOut)
def read_plan(plan_version: str, db: Session = Depends(get_db)) -> Any:
    plan = services.get_plan_plain(db, plan_version)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@router.post(
    "/plans/{plan_version}/events",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_events(
    plan_version: str, body: EventBatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.import_events(
            db,
            plan_version=plan_version,
            events=[e.model_dump() for e in body.events],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/snapshot",
    response_model=SnapshotOut,
)
def get_snapshot(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        snap = services.current_snapshot(db, plan_version)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/progress",
    response_model=StudentProgressOut,
)
def get_progress(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = services.student_progress(db, plan_version, student_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze(
    plan_version: str,
    freeze_id: str,
    body: FreezeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap, _ = services.freeze_semester(
            db, plan_version=plan_version, freeze_id=freeze_id
        )
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
)
def get_freeze(
    plan_version: str, freeze_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        snap = services.get_frozen_snapshot(db, plan_version, freeze_id)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.FreezeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/explain/{student_id}",
    response_model=StudentProgressOut,
)
def explain_freeze_student(
    plan_version: str,
    freeze_id: str,
    student_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.explain_frozen_student(
            db, plan_version, freeze_id, student_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/diff/{other_freeze_id}",
    response_model=DiffOut,
)
def get_diff(
    plan_version: str,
    freeze_id: str,
    other_freeze_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.diff_freezes(
            db, plan_version, freeze_id, other_freeze_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 导师工作量结算账本
# ---------------------------------------------------------------------------


_PLAN_NOT_FOUND = (settlement_service.PlanNotFoundError,)
_SETTLE_NOT_FOUND = (
    settlement_service.PlanNotFoundError,
    settlement_service.BatchNotFoundError,
)


@router.put(
    "/plans/{plan_version}/settlement/rules",
    response_model=SettlementRulesOut,
)
def put_settlement_rules(
    plan_version: str, body: SettlementRulesIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return settlement_service.configure_rules(
            db,
            plan_version=plan_version,
            delegation_primary_share_bp=body.delegation_primary_share_bp,
            delegation_secondary_share_bp=body.delegation_secondary_share_bp,
        )
    except _PLAN_NOT_FOUND as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except settlement_service.RuleValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/settlement/rules",
    response_model=SettlementRulesOut,
)
def get_settlement_rules(
    plan_version: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return settlement_service.get_rules(db, plan_version)
    except _PLAN_NOT_FOUND as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/plans/{plan_version}/settlements/preview")
def preview_settlement(
    plan_version: str, body: SettlementPreviewIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return settlement_service.preview_settlement(
            db,
            plan_version,
            body.period,
            event_cutoff_id=body.event_cutoff_id,
        )
    except _PLAN_NOT_FOUND as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except settlement_service.RuleValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/settlements/{batch_id}",
)
def issue_settlement(
    plan_version: str,
    batch_id: str,
    body: SettlementBatchIn,
    response: Response,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result, created = settlement_service.issue_batch(
            db,
            plan_version=plan_version,
            batch_id=batch_id,
            period=body.period,
            event_cutoff_id=body.event_cutoff_id,
        )
    except _SETTLE_NOT_FOUND as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except settlement_service.RuleValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except settlement_service.SettlementConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if response is not None:
        response.status_code = (
            status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )
    return result


@router.get("/plans/{plan_version}/settlements")
def list_settlements(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return {"batches": settlement_service.list_batches(db, plan_version)}
    except _PLAN_NOT_FOUND as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/plans/{plan_version}/settlements/{batch_id}")
def get_settlement(
    plan_version: str, batch_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return settlement_service.read_batch(db, plan_version, batch_id)
    except _SETTLE_NOT_FOUND as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/settlements/{batch_id}/adjustments",
    status_code=status.HTTP_201_CREATED,
)
def post_settlement_adjustment(
    plan_version: str,
    batch_id: str,
    body: SettlementAdjustmentIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return settlement_service.add_adjustment(
            db,
            plan_version=plan_version,
            batch_id=batch_id,
            adjustment_id=body.adjustment_id,
            mentor_id=body.mentor_id,
            checkin_event_id=body.checkin_event_id,
            delta_seconds=body.delta_seconds,
            reason=body.reason,
            created_by=body.created_by,
            weight_bp=body.weight_bp,
        )
    except _SETTLE_NOT_FOUND as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except settlement_service.AdjustmentTargetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except settlement_service.RuleValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except settlement_service.AdjustmentConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/plans/{plan_version}/mentors/{mentor_id}/settlement")
def get_mentor_settlement(
    plan_version: str, mentor_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return settlement_service.mentor_detail(db, plan_version, mentor_id)
    except _PLAN_NOT_FOUND as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

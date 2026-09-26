"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import services
from .db import get_db
from .schemas import (
    AdjustmentIn,
    AdjustmentResult,
    BatchIssueIn,
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    MentorWorkloadOut,
    PlanIn,
    PlanOut,
    ReconcileOut,
    SettlementRuleIn,
    SettlementRuleOut,
    SnapshotOut,
    StudentProgressOut,
    WorkloadBatchOut,
    WorkloadPreviewOut,
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


@router.put("/plans/{plan_version}/settlement-rules", response_model=SettlementRuleOut)
def put_settlement_rule(
    plan_version: str, body: SettlementRuleIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.ensure_settlement_rule(
            db,
            plan_version=plan_version,
            seconds_per_unit=body.seconds_per_unit,
            delegator_share_bps=body.delegator_share_bps,
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/plans/{plan_version}/settlement-rules", response_model=SettlementRuleOut)
def get_settlement_rule(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.get_settlement_rule(db, plan_version)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/plans/{plan_version}/workload/preview", response_model=WorkloadPreviewOut)
def get_workload_preview(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.preview_workload(db, plan_version)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/workload-batches/{batch_id}",
    response_model=WorkloadBatchOut,
    status_code=status.HTTP_201_CREATED,
)
def post_workload_batch(
    plan_version: str,
    batch_id: str,
    body: BatchIssueIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        view, _ = services.issue_workload_batch(
            db, plan_version=plan_version, batch_id=batch_id, note=body.note
        )
        return view
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/workload-batches/{batch_id}",
    response_model=WorkloadBatchOut,
)
def get_workload_batch(
    plan_version: str, batch_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.get_workload_batch(db, plan_version, batch_id)
    except (services.PlanNotFoundError, services.BatchNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/workload-batches/{batch_id}/adjustments",
    response_model=AdjustmentResult,
    status_code=status.HTTP_201_CREATED,
)
def post_adjustment(
    plan_version: str,
    batch_id: str,
    body: AdjustmentIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        adjustment, created = services.append_workload_adjustment(
            db,
            plan_version=plan_version,
            batch_id=batch_id,
            adjustment_id=body.adjustment_id,
            mentor_id=body.mentor_id,
            seconds=body.seconds,
            reason=body.reason,
            actor=body.actor,
        )
        return {**adjustment, "created": created}
    except (services.PlanNotFoundError, services.BatchNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/workload-batches/{batch_id}/reconcile",
    response_model=ReconcileOut,
)
def get_reconcile(
    plan_version: str, batch_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.reconcile_workload_batch(db, plan_version, batch_id)
    except (services.PlanNotFoundError, services.BatchNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/mentors/{mentor_id}/workload",
    response_model=MentorWorkloadOut,
)
def get_mentor_workload(
    plan_version: str,
    mentor_id: str,
    batch_id: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.mentor_workload(db, plan_version, mentor_id, batch_id)
    except (services.PlanNotFoundError, services.BatchNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="mentor not found")
    return result

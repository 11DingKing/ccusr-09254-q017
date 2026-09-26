"""导师工作量结算服务。

在 append-only 事件流之上派生工作量账本，并把每月的最终结果封存为不可变
结算批次；冻结之后的纠错只通过 *调整分录* 反映，历史分录永不删除或改写。
"""

from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from hashlib import sha256
from typing import Any

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .core.ledger import (
    BASIS_POINTS,
    LedgerLine,
    RuleSet,
    build_ledger,
    summarize,
)
from .repository import (
    get_plan,
    get_settlement_batch,
    insert_adjustment as repo_insert_adjustment,
    insert_settlement_batch,
    insert_settlement_entries,
    latest_settlement_batch,
    list_adjustments,
    list_adjustments_for_mentor,
    list_entries_for_mentor,
    list_settlement_batches,
    list_settlement_entries,
    load_events,
    max_event_id,
    settled_checkin_mentor_pairs,
    upsert_settlement_rule,
    get_settlement_rule,
)

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class SettlementError(Exception):
    pass


class PlanNotFoundError(Exception):
    pass


class BatchNotFoundError(Exception):
    pass


class RuleValidationError(SettlementError):
    pass


class SettlementConflictError(SettlementError):
    pass


class AdjustmentConflictError(SettlementError):
    pass


class AdjustmentTargetError(SettlementError):
    pass


@contextmanager
def _immediate_lock(db: Session):
    """把传入会话的下一个事务升级为 BEGIN IMMEDIATE（SQLite）。

    进入前先 ``commit`` 释放该会话可能因只读查询持有的 SHARED 锁，避免
    “同线程另一连接提交”自锁；随后显式开启事务（begin 钩子据此发
    BEGIN IMMEDIATE），事务一开始即持有写锁，把并发签发串行化。取锁若遇到
    SQLITE_BUSY（写-写死锁会立即返回而非等待 busy_timeout），退避重试。
    退出时复位标志。其他数据库忽略该标志，仍依赖唯一约束兜底。
    """
    from . import db as db_module
    from .repository import _is_busy_error

    db.commit()  # 结束任何只读事务，释放 SHARED 锁
    db_module.set_immediate(True)
    acquired = False
    try:
        for attempt in range(8):
            try:
                db.begin()  # 触发 begin 钩子发出 BEGIN IMMEDIATE
                acquired = True
                break
            except OperationalError as exc:
                if not _is_busy_error(exc) or attempt == 7:
                    raise
                db.rollback()
                time.sleep(0.01 * (2 ** attempt))
        if not acquired:
            raise SettlementConflictError("could not acquire settlement write lock")
        yield
    finally:
        db_module.set_immediate(False)


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def _validate_period(period: str) -> str:
    if not PERIOD_RE.match(period):
        raise RuleValidationError("period must have the form 'YYYY-MM'")
    return period


def _ruleset(primary_bp: int, secondary_bp: int) -> RuleSet:
    rules = RuleSet(
        delegation_primary_share_bp=primary_bp,
        delegation_secondary_share_bp=secondary_bp,
    )
    if not rules.is_valid():
        raise RuleValidationError(
            "delegation shares must be in [0, 10000] and sum to 10000"
        )
    return rules


def configure_rules(
    db: Session,
    *,
    plan_version: str,
    delegation_primary_share_bp: int,
    delegation_secondary_share_bp: int,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    _ruleset(delegation_primary_share_bp, delegation_secondary_share_bp)
    row = upsert_settlement_rule(
        db,
        plan_version=plan_version,
        delegation_primary_share_bp=delegation_primary_share_bp,
        delegation_secondary_share_bp=delegation_secondary_share_bp,
    )
    return _rule_dict(row)


def get_rules(db: Session, plan_version: str) -> dict[str, Any]:
    _require_plan(db, plan_version)
    row = get_settlement_rule(db, plan_version)
    if row is None:
        return _rule_dict(RuleSet())
    return _rule_dict(row)


def _rule_dict(row: Any) -> dict[str, Any]:
    return {
        "delegation_primary_share_bp": row.delegation_primary_share_bp,
        "delegation_secondary_share_bp": row.delegation_secondary_share_bp,
    }


def _live_rules(db: Session, plan_version: str) -> RuleSet:
    row = get_settlement_rule(db, plan_version)
    if row is None:
        return RuleSet()
    return RuleSet(
        delegation_primary_share_bp=row.delegation_primary_share_bp,
        delegation_secondary_share_bp=row.delegation_secondary_share_bp,
    )


def _ledger_at(
    db: Session,
    plan,
    *,
    rules: RuleSet,
    event_cutoff_id: str | None,
):
    events = load_events(db, plan.plan_version)
    return build_ledger(
        events,
        plan_version=plan.plan_version,
        timezone_name=plan.iana_timezone,
        rules=rules,
        up_to_event_id=event_cutoff_id,
    )


def _eligible_lines(
    lines: list[LedgerLine],
    *,
    period: str,
    settled_pairs: set[tuple[str, str, str]],
) -> tuple[list[LedgerLine], int]:
    eligible: list[LedgerLine] = []
    excluded = 0
    for line in lines:
        if line.period != period:
            continue
        if (line.checkin_event_id, line.mentor_id, line.period) in settled_pairs:
            excluded += 1
            continue
        eligible.append(line)
    return eligible, excluded


def preview_settlement(
    db: Session,
    plan_version: str,
    period: str,
    *,
    event_cutoff_id: str | None = None,
) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    _validate_period(period)
    rules = _live_rules(db, plan_version)
    cutoff = event_cutoff_id if event_cutoff_id is not None else max_event_id(
        db, plan_version
    )
    state = _ledger_at(db, plan, rules=rules, event_cutoff_id=cutoff)
    settled_pairs = settled_checkin_mentor_pairs(db, plan_version)
    eligible, excluded = _eligible_lines(
        state.lines, period=period, settled_pairs=settled_pairs
    )
    line_dicts = [line.to_dict() for line in eligible]
    return {
        "plan_version": plan_version,
        "period": period,
        "rules": _rule_dict(rules),
        "event_cutoff_id": cutoff,
        "lines": line_dicts,
        "mentor_totals": list(summarize(eligible).values()),
        "excluded_already_settled": excluded,
        "invalid_confirmations": [item.to_dict() for item in state.invalid_confirmations],
    }


def _canonical_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
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


def compute_entry_hash(
    *,
    plan_version: str,
    batch_id: str,
    period: str,
    rules: dict[str, Any],
    event_cutoff_id: str | None,
    prev_hash: str | None,
    lines: list[dict[str, Any]],
) -> str:
    payload = {
        "plan_version": plan_version,
        "batch_id": batch_id,
        "period": period,
        "rules": rules,
        "event_cutoff_id": event_cutoff_id,
        "prev_hash": prev_hash,
        "lines": _canonical_lines(lines),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(blob.encode("utf-8")).hexdigest()


def _batch_dict(
    batch: Any,
    entries: list[Any],
    adjustments: list[Any],
    *,
    verified: bool | None = None,
) -> dict[str, Any]:
    entry_dicts = [_entry_dict(e) for e in entries]
    adjustment_dicts = [_adjustment_dict(a) for a in adjustments]
    original_seconds = sum(e.seconds for e in entries)
    delta_seconds = sum(a.delta_seconds for a in adjustments)
    return {
        "plan_version": batch.plan_version,
        "batch_id": batch.batch_id,
        "period": batch.period,
        "status": batch.status,
        "rules": dict(batch.rules_snapshot),
        "event_cutoff_id": batch.event_cutoff_id,
        "line_count": batch.line_count,
        "total_seconds": batch.total_seconds,
        "current_total_seconds": original_seconds + delta_seconds,
        "delta_seconds": delta_seconds,
        "prev_hash": batch.prev_hash,
        "entry_hash": batch.entry_hash,
        "hash_verified": verified,
        "created_at": batch.created_at.isoformat(),
        "entries": entry_dicts,
        "adjustments": adjustment_dicts,
    }


def _entry_dict(entry: Any) -> dict[str, Any]:
    return {
        "period": entry.period,
        "mentor_id": entry.mentor_id,
        "student_id": entry.student_id,
        "checkin_event_id": entry.checkin_event_id,
        "confirm_event_id": entry.confirm_event_id,
        "weight_bp": entry.weight_bp,
        "seconds": entry.seconds,
        "counts_raw": entry.counts_raw,
    }


def _adjustment_dict(adjustment: Any) -> dict[str, Any]:
    return {
        "adjustment_id": adjustment.adjustment_id,
        "mentor_id": adjustment.mentor_id,
        "checkin_event_id": adjustment.checkin_event_id,
        "weight_bp": adjustment.weight_bp,
        "delta_seconds": adjustment.delta_seconds,
        "reason": adjustment.reason,
        "created_by": adjustment.created_by,
        "created_at": adjustment.created_at.isoformat(),
    }


def issue_batch(
    db: Session,
    *,
    plan_version: str,
    batch_id: str,
    period: str,
    event_cutoff_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    _validate_period(period)

    # 签发关键区在同一个会话/连接上完成：先释放既有只读事务的 SHARED 锁，
    # 再以 BEGIN IMMEDIATE 启动新事务，事务一开始即持有写锁，把并发签发
    # 串行化；唯一约束再做数据库级兜底。
    with _immediate_lock(db):
        plan = _require_plan(db, plan_version)

        if get_settlement_batch(db, plan_version, batch_id) is not None:
            already_exists = True
            db.commit()  # 幂等路径：结束 IMMEDIATE 事务，释放写锁
        else:
            already_exists = False
            rules = _live_rules(db, plan_version)
            cutoff = event_cutoff_id if event_cutoff_id is not None else max_event_id(
                db, plan_version
            )
            state = _ledger_at(db, plan, rules=rules, event_cutoff_id=cutoff)
            settled_pairs = settled_checkin_mentor_pairs(db, plan_version)
            eligible, _excluded = _eligible_lines(
                state.lines, period=period, settled_pairs=settled_pairs
            )
            line_dicts = [line.to_dict() for line in eligible]

            # 并发失败者：拿到写锁后重算，发现该月已无可结算分录且已有同月批次，
            # 不能再封存一个空批次，交由调用方重新预览。
            if not line_dicts and any(
                b.period == period for b in list_settlement_batches(db, plan_version)
            ):
                db.rollback()
                raise SettlementConflictError(
                    f"period '{period}' is already settled and has no new confirmations; "
                    "re-preview and retry"
                )

            prev = latest_settlement_batch(db, plan_version)
            prev_hash = prev.entry_hash if prev is not None else None
            rules_dict = _rule_dict(rules)
            entry_hash = compute_entry_hash(
                plan_version=plan_version,
                batch_id=batch_id,
                period=period,
                rules=rules_dict,
                event_cutoff_id=cutoff,
                prev_hash=prev_hash,
                lines=line_dicts,
            )
            total_seconds = sum(line["seconds"] for line in line_dicts)

            batch_row = insert_settlement_batch(
                db,
                plan_version=plan_version,
                batch_id=batch_id,
                period=period,
                rules_snapshot=rules_dict,
                event_cutoff_id=cutoff,
                line_count=len(line_dicts),
                total_seconds=total_seconds,
                prev_hash=prev_hash,
                entry_hash=entry_hash,
            )
            if batch_row is None:
                db.rollback()
                raise SettlementConflictError(
                    f"settlement batch '{batch_id}' already exists"
                )
            try:
                insert_settlement_entries(
                    db,
                    plan_version=plan_version,
                    batch_id=batch_id,
                    period=period,
                    lines=line_dicts,
                )
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise SettlementConflictError(
                    "concurrent settlement sealed overlapping entries; "
                    "re-preview and retry"
                ) from exc

    # 写锁已释放、IMMEDIATE 标志已复位，再以普通只读事务读取结果。
    return read_batch(db, plan_version, batch_id), not already_exists


def read_batch(db: Session, plan_version: str, batch_id: str) -> dict[str, Any]:
    batch = get_settlement_batch(db, plan_version, batch_id)
    if batch is None:
        raise BatchNotFoundError(
            f"settlement batch '{batch_id}' for plan '{plan_version}' does not exist"
        )
    entries = list_settlement_entries(db, plan_version, batch_id)
    adjustments = list_adjustments(db, plan_version, batch_id)
    recomputed = compute_entry_hash(
        plan_version=plan_version,
        batch_id=batch_id,
        period=batch.period,
        rules=dict(batch.rules_snapshot),
        event_cutoff_id=batch.event_cutoff_id,
        prev_hash=batch.prev_hash,
        lines=[_entry_dict(e) for e in entries],
    )
    verified = recomputed == batch.entry_hash
    return _batch_dict(batch, entries, adjustments, verified=verified)


def list_batches(db: Session, plan_version: str) -> list[dict[str, Any]]:
    _require_plan(db, plan_version)
    result = []
    for batch in list_settlement_batches(db, plan_version):
        entries = list_settlement_entries(db, plan_version, batch.batch_id)
        adjustments = list_adjustments(db, plan_version, batch.batch_id)
        result.append(_batch_dict(batch, entries, adjustments))
    return result


def add_adjustment(
    db: Session,
    *,
    plan_version: str,
    batch_id: str,
    adjustment_id: str,
    mentor_id: str,
    checkin_event_id: str,
    delta_seconds: int,
    reason: str,
    created_by: str,
    weight_bp: int = BASIS_POINTS,
) -> dict[str, Any]:
    batch = get_settlement_batch(db, plan_version, batch_id)
    if batch is None:
        raise BatchNotFoundError(
            f"settlement batch '{batch_id}' for plan '{plan_version}' does not exist"
        )
    if delta_seconds == 0:
        raise RuleValidationError("delta_seconds must be non-zero")
    if not 0 <= weight_bp <= BASIS_POINTS:
        raise RuleValidationError("weight_bp must be within [0, 10000]")
    if not reason.strip() or not created_by.strip():
        raise RuleValidationError("reason and created_by are required")

    entries = list_settlement_entries(db, plan_version, batch_id)
    target = next(
        (
            e
            for e in entries
            if e.mentor_id == mentor_id and e.checkin_event_id == checkin_event_id
        ),
        None,
    )
    if target is None:
        raise AdjustmentTargetError(
            "adjustment must reference an entry already settled in this batch"
        )

    row = repo_insert_adjustment(
        db,
        plan_version=plan_version,
        batch_id=batch_id,
        adjustment_id=adjustment_id,
        mentor_id=mentor_id,
        checkin_event_id=checkin_event_id,
        weight_bp=weight_bp,
        delta_seconds=delta_seconds,
        reason=reason.strip(),
        created_by=created_by.strip(),
    )
    if row is None:
        raise AdjustmentConflictError(
            f"adjustment '{adjustment_id}' already exists in batch '{batch_id}'"
        )
    return _adjustment_dict(row)


def mentor_detail(db: Session, plan_version: str, mentor_id: str) -> dict[str, Any]:
    _require_plan(db, plan_version)
    entries = list_entries_for_mentor(db, plan_version, mentor_id)
    adjustments = list_adjustments_for_mentor(db, plan_version, mentor_id)

    per_batch: dict[str, dict[str, Any]] = {}
    for entry in entries:
        bucket = per_batch.setdefault(
            entry.batch_id,
            {
                "batch_id": entry.batch_id,
                "period": entry.period,
                "original_seconds": 0,
                "delta_seconds": 0,
                "current_seconds": 0,
                "raw_confirmations": 0,
                "weighted_count_bp": 0,
                "entries": [],
            },
        )
        bucket["original_seconds"] += entry.seconds
        bucket["raw_confirmations"] += 1 if entry.counts_raw else 0
        bucket["weighted_count_bp"] += entry.weight_bp
        bucket["entries"].append(_entry_dict(entry))

    for adjustment in adjustments:
        bucket = per_batch.get(adjustment.batch_id)
        if bucket is not None:
            bucket["delta_seconds"] += adjustment.delta_seconds

    batches: list[dict[str, Any]] = []
    for bucket in sorted(per_batch.values(), key=lambda b: (b["period"], b["batch_id"])):
        bucket["current_seconds"] = (
            bucket["original_seconds"] + bucket["delta_seconds"]
        )
        bucket["weighted_count"] = round(
            bucket["weighted_count_bp"] / BASIS_POINTS, 4
        )
        batches.append(bucket)

    original_seconds = sum(b["original_seconds"] for b in batches)
    delta_seconds = sum(b["delta_seconds"] for b in batches)
    return {
        "plan_version": plan_version,
        "mentor_id": mentor_id,
        "batches": batches,
        "original_seconds": original_seconds,
        "delta_seconds": delta_seconds,
        "current_seconds": original_seconds + delta_seconds,
        "raw_confirmations": sum(b["raw_confirmations"] for b in batches),
    }

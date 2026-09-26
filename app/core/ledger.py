"""导师工作量账本的确定性领域内核。

事件流仍然是唯一事实来源（append-only），本模块在重放时回答一个问题：
哪些导师确认 *最终有效*，各自应归属多少工作量。

确认失效的情形（都会在 ``invalid_confirmations`` 中留痕，便于审计）：

* ``unknown_checkin``       —— 确认指向不存在的打卡；
* ``student_mismatch``      —— 确认事件的学生与打卡学生不一致；
* ``delegation_not_active`` —— 以委托身份确认，但委托从未发生或已被撤销；
* ``malformed_shares``      —— 显式份额不为非负整数或合计不是 10000（basis points）；
* ``duplicate``             —— 同一条打卡仍被一条未撤销的确认占用（“重复确认”只计一次）；
* ``revoked``               —— 确认被相关导师撤销；
* ``delegation_revoked``    —— 委托授权被主带导师撤销，受托确认随之作废。

被撤销/委托作废的确认会释放占用：撤销事件之后到来的新确认可以重新占用该打卡，
旧确认本身保留在失效清单中（历史不删除）。

委托份额以万分之一（basis points）为单位，默认主带导师 60% / 受托导师 40%。
一条确认产生的工作量秒数按份额精确切分，尾差给排序后的最后一位导师，
保证合计恒等于打卡时长。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Iterable

from .clock import elapsed_seconds, split_by_academic_day, to_utc
from .replay import Event, EventType

BASIS_POINTS = 10_000
DEFAULT_PRIMARY_SHARE_BP = 6_000
DEFAULT_SECONDARY_SHARE_BP = 4_000
DEFAULT_MENTOR_ID = "M-DEFAULT"


class InvalidReason(StrEnum):
    UNKNOWN_CHECKIN = "unknown_checkin"
    STUDENT_MISMATCH = "student_mismatch"
    DELEGATION_NOT_ACTIVE = "delegation_not_active"
    MALFORMED_SHARES = "malformed_shares"
    DUPLICATE = "duplicate"
    REVOKED = "revoked"
    DELEGATION_REVOKED = "delegation_revoked"


@dataclass(frozen=True)
class RuleSet:
    """结算规则。委托份额以 basis points 表示，必须合计为 10000。"""

    delegation_primary_share_bp: int = DEFAULT_PRIMARY_SHARE_BP
    delegation_secondary_share_bp: int = DEFAULT_SECONDARY_SHARE_BP

    def is_valid(self) -> bool:
        return (
            0 <= self.delegation_primary_share_bp <= BASIS_POINTS
            and 0 <= self.delegation_secondary_share_bp <= BASIS_POINTS
            and self.delegation_primary_share_bp
            + self.delegation_secondary_share_bp
            == BASIS_POINTS
        )


@dataclass(frozen=True)
class LedgerLine:
    """一条归属到导师的工作量分录（一条有效确认可拆成多条）。"""

    period: str
    mentor_id: str
    student_id: str
    checkin_event_id: str
    confirm_event_id: str
    weight_bp: int
    seconds: int
    # 每条有效确认只在确认人身上计 1 次“原始确认数”，避免拆账后虚增。
    counts_raw: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "mentor_id": self.mentor_id,
            "student_id": self.student_id,
            "checkin_event_id": self.checkin_event_id,
            "confirm_event_id": self.confirm_event_id,
            "weight_bp": self.weight_bp,
            "weighted_count": round(self.weight_bp / BASIS_POINTS, 4),
            "seconds": self.seconds,
            "counts_raw": self.counts_raw,
        }


@dataclass(frozen=True)
class InvalidConfirmation:
    checkin_event_id: str
    confirm_event_id: str
    student_id: str
    mentor_id: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkin_event_id": self.checkin_event_id,
            "confirm_event_id": self.confirm_event_id,
            "student_id": self.student_id,
            "mentor_id": self.mentor_id,
            "reason": self.reason,
        }


@dataclass
class LedgerState:
    plan_version: str
    timezone: str
    rules: RuleSet
    lines: list[LedgerLine] = field(default_factory=list)
    invalid_confirmations: list[InvalidConfirmation] = field(default_factory=list)

    def lines_for_period(self, period: str) -> list[LedgerLine]:
        return [line for line in self.lines if line.period == period]


@dataclass
class _RawConfirmation:
    checkin_event_id: str
    confirm_event_id: str
    student_id: str
    confirmer_id: str
    shares: tuple[tuple[str, int], ...]
    primary_id: str | None = None
    revoked: bool = False
    void_reason: str = InvalidReason.REVOKED.value


def _parse_shares(
    payload: dict[str, Any], primary: str, confirmer: str, rules: RuleSet
) -> tuple[tuple[str, int], ...] | None:
    explicit = payload.get("shares")
    if explicit is not None:
        if not isinstance(explicit, dict) or not explicit:
            return None
        try:
            pairs = tuple(sorted((str(k), int(v)) for k, v in explicit.items()))
        except (TypeError, ValueError):
            return None
        if any(weight < 0 for _, weight in pairs):
            return None
        if sum(weight for _, weight in pairs) != BASIS_POINTS:
            return None
        return pairs
    return tuple(
        sorted(
            (
                (primary, rules.delegation_primary_share_bp),
                (confirmer, rules.delegation_secondary_share_bp),
            )
        )
    )


def _allocate_seconds(
    total: int, shares: tuple[tuple[str, int], ...]
) -> dict[str, int]:
    """按份额切分秒数，尾差补给排序后的最后一位导师，保证合计精确。"""
    ordered = sorted(shares, key=lambda pair: pair[0])
    allocated: dict[str, int] = {}
    running = 0
    for index, (mentor_id, weight) in enumerate(ordered):
        if index == len(ordered) - 1:
            allocated[mentor_id] = total - running
        else:
            part = total * weight // BASIS_POINTS
            allocated[mentor_id] = part
            running += part
    return allocated


def build_ledger(
    events: Iterable[Event],
    *,
    plan_version: str,
    timezone_name: str,
    rules: RuleSet | None = None,
    up_to_event_id: str | None = None,
) -> LedgerState:
    """确定性重放事件流，产出最终有效的导师工作量分录。"""
    rules = rules or RuleSet()
    if not rules.is_valid():
        raise ValueError("ledger rules must split BASIS_POINTS into non-negative shares")

    sorted_events = sorted(
        (e for e in events if e.plan_version == plan_version),
        key=lambda e: e.event_id,
    )
    if up_to_event_id is not None:
        sorted_events = [e for e in sorted_events if e.event_id <= up_to_event_id]

    # checkin_event_id -> (student_id, start_utc, end_utc, duration_seconds)
    checkins: dict[str, tuple[str, datetime, datetime, int]] = {}
    # (student_id, primary, delegate) -> 委托当前是否生效
    grants: dict[tuple[str, str, str], bool] = {}
    # 形式合格的确认（可能之后被撤销），按事件序到达
    candidates: list[_RawConfirmation] = []
    # checkin_event_id -> 当前占用该打卡的确认
    winner_by_checkin: dict[str, _RawConfirmation] = {}
    invalid: list[InvalidConfirmation] = []

    for event in sorted_events:
        payload = event.payload or {}

        if event.event_type == EventType.CHECKIN:
            start = to_utc(datetime.fromisoformat(payload["check_in_at"]))
            end = to_utc(datetime.fromisoformat(payload["check_out_at"]))
            checkins[event.event_id] = (
                event.student_id,
                start,
                end,
                elapsed_seconds(start, end),
            )
            continue

        if event.event_type == EventType.MENTOR_DELEGATE:
            primary = str(payload.get("primary_mentor_id", "")).strip()
            delegate = str(payload.get("delegate_mentor_id", "")).strip()
            if not primary or not delegate:
                continue
            key = (event.student_id, primary, delegate)
            if bool(payload.get("revoked", False)):
                grants[key] = False
                # 撤销授权：该委托当前占用的确认全部作废并释放打卡槽位。
                for checkin_id, winner in list(winner_by_checkin.items()):
                    if (
                        winner.student_id == event.student_id
                        and winner.primary_id == primary
                        and winner.confirmer_id == delegate
                    ):
                        winner.revoked = True
                        winner.void_reason = InvalidReason.DELEGATION_REVOKED.value
                        invalid.append(
                            InvalidConfirmation(
                                checkin_event_id=winner.checkin_event_id,
                                confirm_event_id=winner.confirm_event_id,
                                student_id=winner.student_id,
                                mentor_id=winner.confirmer_id,
                                reason=InvalidReason.DELEGATION_REVOKED.value,
                            )
                        )
                        del winner_by_checkin[checkin_id]
            else:
                grants[key] = True
            continue

        if event.event_type == EventType.MENTOR_CONFIRM_REVOKE:
            target_id = payload.get("checkin_event_id")
            mentor_id = str(payload.get("mentor_id", "")).strip()
            winner = winner_by_checkin.get(target_id) if target_id else None
            if (
                winner is not None
                and mentor_id in {m for m, _ in winner.shares}
            ):
                winner.revoked = True
                winner.void_reason = InvalidReason.REVOKED.value
                invalid.append(
                    InvalidConfirmation(
                        checkin_event_id=winner.checkin_event_id,
                        confirm_event_id=winner.confirm_event_id,
                        student_id=winner.student_id,
                        mentor_id=winner.confirmer_id,
                        reason=InvalidReason.REVOKED.value,
                    )
                )
                del winner_by_checkin[winner.checkin_event_id]
            continue

        if event.event_type != EventType.MENTOR_CONFIRM:
            continue

        target_id = payload.get("checkin_event_id")
        confirmer = (
            str(payload.get("mentor_id", DEFAULT_MENTOR_ID)).strip()
            or DEFAULT_MENTOR_ID
        )

        def _invalidate(reason: InvalidReason, checkin_id: str = target_id or "") -> None:
            invalid.append(
                InvalidConfirmation(
                    checkin_event_id=checkin_id,
                    confirm_event_id=event.event_id,
                    student_id=event.student_id,
                    mentor_id=confirmer,
                    reason=reason.value,
                )
            )

        checkin = checkins.get(target_id) if target_id else None
        if checkin is None:
            _invalidate(InvalidReason.UNKNOWN_CHECKIN)
            continue
        if checkin[0] != event.student_id:
            _invalidate(InvalidReason.STUDENT_MISMATCH)
            continue

        if bool(payload.get("delegated", False)):
            primary = str(payload.get("primary_mentor_id", "")).strip()
            if not primary or not grants.get(
                (event.student_id, primary, confirmer), False
            ):
                _invalidate(InvalidReason.DELEGATION_NOT_ACTIVE)
                continue
            shares = _parse_shares(payload, primary, confirmer, rules)
            if shares is None:
                _invalidate(InvalidReason.MALFORMED_SHARES)
                continue
        else:
            primary = None
            shares = ((confirmer, BASIS_POINTS),)

        if target_id in winner_by_checkin:
            _invalidate(InvalidReason.DUPLICATE)
            continue

        raw = _RawConfirmation(
            checkin_event_id=target_id,
            confirm_event_id=event.event_id,
            student_id=event.student_id,
            confirmer_id=confirmer,
            shares=shares,
            primary_id=primary,
        )
        candidates.append(raw)
        winner_by_checkin[target_id] = raw

    lines: list[LedgerLine] = []
    for raw in candidates:
        if raw.revoked:
            continue
        student_id, start_utc, end_utc, _duration = checkins[raw.checkin_event_id]
        # 跨月打卡按培养方案时区切到各自然月，每月分别拆账。
        period_seconds: dict[str, int] = {}
        for day, seg_start, seg_end in split_by_academic_day(
            start_utc, end_utc, timezone_name
        ):
            month = day.strftime("%Y-%m")
            period_seconds[month] = period_seconds.get(month, 0) + elapsed_seconds(
                seg_start, seg_end
            )
        for period in sorted(period_seconds):
            seconds_split = _allocate_seconds(period_seconds[period], raw.shares)
            for mentor_id, weight in sorted(raw.shares, key=lambda pair: pair[0]):
                lines.append(
                    LedgerLine(
                        period=period,
                        mentor_id=mentor_id,
                        student_id=student_id,
                        checkin_event_id=raw.checkin_event_id,
                        confirm_event_id=raw.confirm_event_id,
                        weight_bp=weight,
                        seconds=seconds_split[mentor_id],
                        counts_raw=(mentor_id == raw.confirmer_id),
                    )
                )

    lines.sort(
        key=lambda line: (
            line.period,
            line.mentor_id,
            line.checkin_event_id,
            line.confirm_event_id,
        )
    )
    invalid.sort(key=lambda item: (item.confirm_event_id, item.mentor_id))
    return LedgerState(
        plan_version=plan_version,
        timezone=timezone_name,
        rules=rules,
        lines=lines,
        invalid_confirmations=invalid,
    )


def summarize(lines: Iterable[LedgerLine]) -> dict[str, dict[str, Any]]:
    """按导师汇总加权确认量与秒数。"""
    totals: dict[str, dict[str, Any]] = {}
    for line in lines:
        bucket = totals.setdefault(
            line.mentor_id,
            {
                "mentor_id": line.mentor_id,
                "weighted_count_bp": 0,
                "weighted_count": 0.0,
                "raw_confirmations": 0,
                "seconds": 0,
            },
        )
        bucket["weighted_count_bp"] += line.weight_bp
        bucket["raw_confirmations"] += 1 if line.counts_raw else 0
        bucket["seconds"] += line.seconds
    for bucket in totals.values():
        bucket["weighted_count"] = round(
            bucket["weighted_count_bp"] / BASIS_POINTS, 4
        )
    return dict(sorted(totals.items()))

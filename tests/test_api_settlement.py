"""导师工作量结算 API 与服务集成测试。"""

from __future__ import annotations

import threading

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import settlement_service
from tests.conftest import SHANGHAI_PLAN

PV = SHANGHAI_PLAN["plan_version"]


def _create_plan(client):
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text


def _checkin(eid, student, start, end, activity_type="internship"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _confirm(eid, student, checkin_id, mentor, **extra):
    return {
        "event_id": eid,
        "event_type": "mentor_confirm",
        "student_id": student,
        "payload": {"checkin_event_id": checkin_id, "mentor_id": mentor, **extra},
    }


def _delegate(eid, student, primary, delegate, **extra):
    return {
        "event_id": eid,
        "event_type": "mentor_delegate",
        "student_id": student,
        "payload": {
            "primary_mentor_id": primary,
            "delegate_mentor_id": delegate,
            **extra,
        },
    }


def _revoke(eid, student, checkin_id, mentor):
    return {
        "event_id": eid,
        "event_type": "mentor_confirm_revoke",
        "student_id": student,
        "payload": {"checkin_event_id": checkin_id, "mentor_id": mentor},
    }


def _post_events(client, *events):
    resp = client.post(f"/api/plans/{PV}/events", json={"events": list(events)})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------


def test_rules_default_to_60_40_and_validate(client):
    _create_plan(client)
    rules = client.get(f"/api/plans/{PV}/settlement/rules").json()
    assert rules == {
        "delegation_primary_share_bp": 6000,
        "delegation_secondary_share_bp": 4000,
    }

    bad = client.put(
        f"/api/plans/{PV}/settlement/rules",
        json={"delegation_primary_share_bp": 7000,
              "delegation_secondary_share_bp": 4000},
    )
    assert bad.status_code == 422

    ok = client.put(
        f"/api/plans/{PV}/settlement/rules",
        json={"delegation_primary_share_bp": 7000,
              "delegation_secondary_share_bp": 3000},
    )
    assert ok.status_code == 200
    assert ok.json()["delegation_primary_share_bp"] == 7000


# ---------------------------------------------------------------------------
# 预览与签发
# ---------------------------------------------------------------------------


def _seed_delegated_month(client, period_start_day="2024-03-15"):
    _create_plan(client)
    _post_events(
        client,
        _checkin("E-01", "S1", f"{period_start_day}T08:00:00+08:00",
                 f"{period_start_day}T10:00:00+08:00"),
        _delegate("E-02", "S1", "M-PRIMARY", "M-DELEGATE"),
        _confirm("E-03", "S1", "E-01", "M-DELEGATE", delegated=True,
                 primary_mentor_id="M-PRIMARY"),
    )


def test_preview_splits_delegated_confirmation_and_lists_invalid(client):
    _create_plan(client)
    _post_events(
        client,
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00",
                 "2024-03-15T10:00:00+08:00"),
        _delegate("E-02", "S1", "M-PRIMARY", "M-DELEGATE"),
        _confirm("E-03", "S1", "E-01", "M-DELEGATE", delegated=True,
                 primary_mentor_id="M-PRIMARY"),
        # 重复确认
        _confirm("E-04", "S1", "E-01", "M-OTHER"),
        # 无委托授权
        _confirm("E-05", "S1", "E-01", "M-X", delegated=True,
                 primary_mentor_id="M-PRIMARY"),
    )
    preview = client.post(
        f"/api/plans/{PV}/settlements/preview", json={"period": "2024-03"}
    ).json()
    assert preview["event_cutoff_id"] == "E-05"
    seconds = {line["mentor_id"]: line["seconds"] for line in preview["lines"]}
    assert seconds == {"M-DELEGATE": 2880, "M-PRIMARY": 4320}
    reasons = {item["confirm_event_id"]: item["reason"]
               for item in preview["invalid_confirmations"]}
    assert reasons["E-04"] == "duplicate"
    assert reasons["E-05"] == "delegation_not_active"
    totals = {t["mentor_id"]: t for t in preview["mentor_totals"]}
    assert totals["M-PRIMARY"]["weighted_count"] == 0.6


def test_issue_creates_immutable_verified_batch(client):
    _seed_delegated_month(client)
    resp = client.post(
        f"/api/plans/{PV}/settlements/B-2024-03", json={"period": "2024-03"}
    )
    assert resp.status_code == 201, resp.text
    batch = resp.json()
    assert batch["status"] == "frozen"
    assert batch["period"] == "2024-03"
    assert batch["line_count"] == 2
    assert batch["total_seconds"] == 7200
    assert batch["delta_seconds"] == 0
    assert batch["current_total_seconds"] == 7200
    assert batch["hash_verified"] is True
    assert batch["prev_hash"] is None
    assert len(batch["entry_hash"]) == 64

    # 规则快照：之后修改规则不影响已封批次。
    client.put(
        f"/api/plans/{PV}/settlement/rules",
        json={"delegation_primary_share_bp": 7000,
              "delegation_secondary_share_bp": 3000},
    )
    again = client.post(
        f"/api/plans/{PV}/settlements/B-2024-03", json={"period": "2024-03"}
    )
    assert again.status_code == 200  # 幂等返回既有批次
    assert again.json()["total_seconds"] == 7200
    assert again.json()["rules"]["delegation_primary_share_bp"] == 6000
    assert again.json()["hash_verified"] is True


def test_second_period_excludes_already_settled_and_chains_hash(client):
    _seed_delegated_month(client)
    client.post(
        f"/api/plans/{PV}/settlements/B-2024-03", json={"period": "2024-03"}
    )
    # 4 月新增一条打卡与确认。
    _post_events(
        client,
        _checkin("E-10", "S1", "2024-04-10T08:00:00+08:00",
                 "2024-04-10T09:00:00+08:00"),
        _confirm("E-11", "S1", "E-10", "M1"),
    )
    # 3 月预览应为空（已全部结算）。
    march_preview = client.post(
        f"/api/plans/{PV}/settlements/preview", json={"period": "2024-03"}
    ).json()
    assert march_preview["lines"] == []
    assert march_preview["excluded_already_settled"] == 2

    resp = client.post(
        f"/api/plans/{PV}/settlements/B-2024-04", json={"period": "2024-04"}
    )
    assert resp.status_code == 201
    april = resp.json()
    assert april["total_seconds"] == 3600
    assert april["prev_hash"] is not None

    march = client.get(f"/api/plans/{PV}/settlements/B-2024-03").json()
    assert april["prev_hash"] == march["entry_hash"]
    assert april["hash_verified"] is True

    listing = client.get(f"/api/plans/{PV}/settlements").json()
    assert [b["batch_id"] for b in listing["batches"]] == [
        "B-2024-03",
        "B-2024-04",
    ]


# ---------------------------------------------------------------------------
# 调整
# ---------------------------------------------------------------------------


def test_adjustment_is_appended_and_history_preserved(client):
    _seed_delegated_month(client)
    client.post(
        f"/api/plans/{PV}/settlements/B-2024-03", json={"period": "2024-03"}
    )
    adj = client.post(
        f"/api/plans/{PV}/settlements/B-2024-03/adjustments",
        json={
            "adjustment_id": "ADJ-1",
            "mentor_id": "M-DELEGATE",
            "checkin_event_id": "E-01",
            "delta_seconds": -600,
            "reason": "duplicated 10 minutes with another activity",
            "created_by": "admin-1",
        },
    )
    assert adj.status_code == 201, adj.text

    batch = client.get(f"/api/plans/{PV}/settlements/B-2024-03").json()
    # 历史原额与分录不变，调整追加反映。
    assert batch["total_seconds"] == 7200
    assert batch["delta_seconds"] == -600
    assert batch["current_total_seconds"] == 7200 - 600
    assert len(batch["entries"]) == 2
    assert batch["adjustments"][0]["adjustment_id"] == "ADJ-1"
    assert batch["hash_verified"] is True  # 调整不触碰原始封存哈希

    # 调整幂等：重复 adjustment_id 冲突。
    dup = client.post(
        f"/api/plans/{PV}/settlements/B-2024-03/adjustments",
        json={
            "adjustment_id": "ADJ-1",
            "mentor_id": "M-DELEGATE",
            "checkin_event_id": "E-01",
            "delta_seconds": -600,
            "reason": "dup",
            "created_by": "admin-1",
        },
    )
    assert dup.status_code == 409

    # 零额调整与引用不存在分录都被拒绝。
    zero = client.post(
        f"/api/plans/{PV}/settlements/B-2024-03/adjustments",
        json={
            "adjustment_id": "ADJ-2",
            "mentor_id": "M-DELEGATE",
            "checkin_event_id": "E-01",
            "delta_seconds": 0,
            "reason": "x",
            "created_by": "admin-1",
        },
    )
    assert zero.status_code == 422
    missing = client.post(
        f"/api/plans/{PV}/settlements/B-2024-03/adjustments",
        json={
            "adjustment_id": "ADJ-3",
            "mentor_id": "M-NOPE",
            "checkin_event_id": "E-01",
            "delta_seconds": 100,
            "reason": "x",
            "created_by": "admin-1",
        },
    )
    assert missing.status_code == 422


# ---------------------------------------------------------------------------
# 个人明细
# ---------------------------------------------------------------------------


def test_mentor_detail_aggregates_batches_and_adjustments(client):
    _seed_delegated_month(client)
    client.post(
        f"/api/plans/{PV}/settlements/B-2024-03", json={"period": "2024-03"}
    )
    client.post(
        f"/api/plans/{PV}/settlements/B-2024-03/adjustments",
        json={
            "adjustment_id": "ADJ-1",
            "mentor_id": "M-DELEGATE",
            "checkin_event_id": "E-01",
            "delta_seconds": -180,
            "reason": "correction",
            "created_by": "admin-1",
        },
    )
    detail = client.get(f"/api/plans/{PV}/mentors/M-DELEGATE/settlement").json()
    assert detail["mentor_id"] == "M-DELEGATE"
    assert detail["original_seconds"] == 2880
    assert detail["delta_seconds"] == -180
    assert detail["current_seconds"] == 2880 - 180
    assert detail["raw_confirmations"] == 1
    assert detail["batches"][0]["batch_id"] == "B-2024-03"
    assert detail["batches"][0]["weighted_count"] == 0.4


# ---------------------------------------------------------------------------
# 委托撤销端到端
# ---------------------------------------------------------------------------


def test_delegation_revoke_end_to_end_releases_confirmation(client):
    _create_plan(client)
    _post_events(
        client,
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00",
                 "2024-03-15T10:00:00+08:00"),
        _delegate("E-02", "S1", "M-PRIMARY", "M-DELEGATE"),
        _confirm("E-03", "S1", "E-01", "M-DELEGATE", delegated=True,
                 primary_mentor_id="M-PRIMARY"),
    )
    # 主带导师撤销委托授权。
    _post_events(client, _delegate("E-04", "S1", "M-PRIMARY", "M-DELEGATE",
                                   revoked=True))
    preview = client.post(
        f"/api/plans/{PV}/settlements/preview", json={"period": "2024-03"}
    ).json()
    # 委托确认作废，尚无新确认 → 无分录。
    assert preview["lines"] == []
    assert any(
        item["reason"] == "delegation_revoked"
        and item["confirm_event_id"] == "E-03"
        for item in preview["invalid_confirmations"]
    )
    # 主带导师重新确认后生效。
    _post_events(client, _confirm("E-05", "S1", "E-01", "M-PRIMARY"))
    preview2 = client.post(
        f"/api/plans/{PV}/settlements/preview", json={"period": "2024-03"}
    ).json()
    assert len(preview2["lines"]) == 1
    assert preview2["lines"][0]["mentor_id"] == "M-PRIMARY"
    assert preview2["lines"][0]["seconds"] == 7200


def test_confirm_revoke_end_to_end_releases_confirmation(client):
    _create_plan(client)
    _post_events(
        client,
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00",
                 "2024-03-15T10:00:00+08:00"),
        _confirm("E-02", "S1", "E-01", "M1"),
        _revoke("E-03", "S1", "E-01", "M1"),
        _confirm("E-04", "S1", "E-01", "M2"),
    )
    preview = client.post(
        f"/api/plans/{PV}/settlements/preview", json={"period": "2024-03"}
    ).json()
    assert len(preview["lines"]) == 1
    assert preview["lines"][0]["mentor_id"] == "M2"


# ---------------------------------------------------------------------------
# 跨月时区
# ---------------------------------------------------------------------------


def test_month_boundary_uses_plan_timezone(client):
    _create_plan(client)
    # 上海 4/1 00:30-01:30（UTC 仍在 3 月）→ 归属 4 月。
    _post_events(
        client,
        _checkin("E-01", "S1", "2024-04-01T00:30:00+08:00",
                 "2024-04-01T01:30:00+08:00"),
        _confirm("E-02", "S1", "E-01", "M1"),
    )
    march = client.post(
        f"/api/plans/{PV}/settlements/preview", json={"period": "2024-03"}
    ).json()
    april = client.post(
        f"/api/plans/{PV}/settlements/preview", json={"period": "2024-04"}
    ).json()
    assert march["lines"] == []
    assert len(april["lines"]) == 1
    assert april["lines"][0]["seconds"] == 3600


# ---------------------------------------------------------------------------
# 并发确认
# ---------------------------------------------------------------------------


def test_concurrent_duplicate_confirmation_counts_once(client):
    _create_plan(client)
    _post_events(
        client,
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00",
                 "2024-03-15T10:00:00+08:00"),
    )
    from tests.conftest import TestSessionLocal

    accepted_count = 0
    duplicate_count = 0
    lock = threading.Lock()

    def _import(eid, mentor):
        session = TestSessionLocal()
        try:
            from app import services as core_services
            out = core_services.import_events(
                session,
                plan_version=PV,
                events=[{
                    "event_id": eid,
                    "event_type": "mentor_confirm",
                    "student_id": "S1",
                    "payload": {"checkin_event_id": "E-01", "mentor_id": mentor},
                }],
            )
            with lock:
                nonlocal_accepted[0] += out["accepted"]
                nonlocal_duplicate[0] += len(out["duplicates"])
        finally:
            session.close()

    nonlocal_accepted = [0]
    nonlocal_duplicate = [0]

    # 同一 event_id 并发导入：数据库去重，仅一条落库。
    threads = [
        threading.Thread(target=_import, args=("E-DUP", f"M{i}")) for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert nonlocal_accepted[0] == 1
    assert nonlocal_duplicate[0] == 3

    preview = client.post(
        f"/api/plans/{PV}/settlements/preview", json={"period": "2024-03"}
    ).json()
    assert len(preview["lines"]) == 1
    assert preview["lines"][0]["seconds"] == 7200


# ---------------------------------------------------------------------------
# 并发签发
# ---------------------------------------------------------------------------


def test_concurrent_batch_issue_only_one_seals_entries(client):
    _seed_delegated_month(client)
    from tests.conftest import TestSessionLocal

    outcomes: list[tuple[str, bool]] = []
    errors: list[str] = []
    lock = threading.Lock()

    def _issue(batch_id):
        session = TestSessionLocal()
        try:
            try:
                _body, created = settlement_service.issue_batch(
                    session,
                    plan_version=PV,
                    batch_id=batch_id,
                    period="2024-03",
                )
                with lock:
                    outcomes.append((batch_id, created))
            except settlement_service.SettlementConflictError as exc:
                with lock:
                    errors.append(str(exc))
        finally:
            session.close()

    threads = [
        threading.Thread(target=_issue, args=("B-A",)),
        threading.Thread(target=_issue, args=("B-B",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 恰好一个批次封存成功，另一个因唯一约束整批回滚。
    winners = [bid for bid, created in outcomes if created]
    assert len(winners) == 1
    assert len(errors) == 1

    listing = client.get(f"/api/plans/{PV}/settlements").json()
    assert len(listing["batches"]) == 1
    winner = listing["batches"][0]
    assert winner["batch_id"] == winners[0]
    assert winner["total_seconds"] == 7200
    # 分录只结算一次，没有翻倍。
    detail = client.get(f"/api/plans/{PV}/mentors/M-PRIMARY/settlement").json()
    assert detail["original_seconds"] == 4320


# ---------------------------------------------------------------------------
# 重启核对
# ---------------------------------------------------------------------------


def test_restart_recomputes_hashes_and_chain(client):
    _seed_delegated_month(client)
    client.post(
        f"/api/plans/{PV}/settlements/B-2024-03", json={"period": "2024-03"}
    )
    _post_events(
        client,
        _checkin("E-10", "S1", "2024-04-10T08:00:00+08:00",
                 "2024-04-10T09:00:00+08:00"),
        _confirm("E-11", "S1", "E-10", "M1"),
    )
    client.post(
        f"/api/plans/{PV}/settlements/B-2024-04", json={"period": "2024-04"}
    )

    # 用全新引擎/会话重新打开同一数据库文件，模拟进程重启后的只读核对。
    restart_engine = create_engine(
        "sqlite:///./practice_hours_test.db",
        connect_args={"check_same_thread": False},
    )
    RestartSession = sessionmaker(bind=restart_engine, future=True)
    session = RestartSession()
    try:
        march = settlement_service.read_batch(session, PV, "B-2024-03")
        april = settlement_service.read_batch(session, PV, "B-2024-04")
        assert march["hash_verified"] is True
        assert april["hash_verified"] is True
        assert april["prev_hash"] == march["entry_hash"]
        assert march["total_seconds"] == 7200
        assert april["total_seconds"] == 3600
        # 个人明细在重启后同样可重算。
        detail = settlement_service.mentor_detail(session, PV, "M-PRIMARY")
        assert detail["original_seconds"] == 4320
        assert detail["current_seconds"] == 4320
    finally:
        session.close()
        restart_engine.dispose()

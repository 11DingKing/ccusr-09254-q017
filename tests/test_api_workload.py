"""服务端业务模块。"""

from __future__ import annotations

import threading

from tests.conftest import SHANGHAI_PLAN, TestSessionLocal, test_engine


def _create_plan(client, plan=SHANGHAI_PLAN):
    resp = client.post("/api/plans", json=plan)
    assert resp.status_code == 201, resp.text
    return plan["plan_version"]


def _checkin(eid, student, start, end, activity_type="internship", activity_id="A1"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": activity_id,
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _confirm(eid, student, checkin_event_id, mentor_id):
    return {
        "event_id": eid,
        "event_type": "mentor_confirm",
        "student_id": student,
        "payload": {"checkin_event_id": checkin_event_id, "mentor_id": mentor_id},
    }


def _revoke(eid, student, checkin_event_id, mentor_id=None):
    payload = {"checkin_event_id": checkin_event_id}
    if mentor_id is not None:
        payload["mentor_id"] = mentor_id
    return {
        "event_id": eid,
        "event_type": "mentor_confirm_revoke",
        "student_id": student,
        "payload": payload,
    }


def _delegate(eid, student, delegation_id, from_mentor, to_mentor, **extra):
    payload = {
        "delegation_id": delegation_id,
        "from_mentor_id": from_mentor,
        "to_mentor_id": to_mentor,
    }
    payload.update(extra)
    return {
        "event_id": eid,
        "event_type": "mentor_delegate",
        "student_id": student,
        "payload": payload,
    }


def _delegate_revoke(eid, student, delegation_id):
    return {
        "event_id": eid,
        "event_type": "mentor_delegate_revoke",
        "student_id": student,
        "payload": {"delegation_id": delegation_id},
    }


def _post_events(client, pv, events):
    resp = client.post(f"/api/plans/{pv}/events", json={"events": events})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _preview(client, pv):
    resp = client.get(f"/api/plans/{pv}/workload/preview")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _mentor(summary_list, mentor_id):
    for item in summary_list:
        if item["mentor_id"] == mentor_id:
            return item
    return None


def test_rule_default_then_upsert_and_validation(client):
    pv = _create_plan(client)

    default = client.get(f"/api/plans/{pv}/settlement-rules")
    assert default.status_code == 200
    body = default.json()
    assert body["is_default"] is True
    assert body["seconds_per_unit"] == 2700
    assert body["delegator_share_bps"] == 2000

    resp = client.put(
        f"/api/plans/{pv}/settlement-rules",
        json={"seconds_per_unit": 3600, "delegator_share_bps": 2500},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_default"] is False

    stored = client.get(f"/api/plans/{pv}/settlement-rules").json()
    assert stored["seconds_per_unit"] == 3600
    assert stored["delegator_share_bps"] == 2500
    assert stored["updated_at"] is not None

    bad = client.put(
        f"/api/plans/{pv}/settlement-rules",
        json={"seconds_per_unit": 3600, "delegator_share_bps": 10001},
    )
    assert bad.status_code == 422

    missing = client.get("/api/plans/NOPE/settlement-rules")
    assert missing.status_code == 404


def test_preview_counts_only_final_valid_confirmation(client):
    pv = _create_plan(client)
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T10:00:00+08:00",
            ),
            # Duplicate confirm by the same mentor is ignored.
            _confirm("E-02", "S1", "E-01", "M1"),
            _confirm("E-03", "S1", "E-01", "M1"),
            # A second mentor cannot take over an active confirmation.
            _confirm("E-04", "S1", "E-01", "M2"),
            # Revoke, then the other mentor's confirm becomes the valid one.
            _revoke("E-05", "S1", "E-01", mentor_id="M1"),
            _confirm("E-06", "S1", "E-01", "M2"),
        ],
    )
    preview = _preview(client, pv)
    assert {e["confirm_event_id"] for e in preview["entries"]} == {"E-06"}
    assert len(preview["entries"]) == 1
    entry = preview["entries"][0]
    assert entry["mentor_id"] == "M2"
    assert entry["role"] == "primary"
    assert entry["seconds"] == 7200

    reasons = [s["reason"] for s in preview["skipped"]]
    assert reasons == ["duplicate_confirm", "already_confirmed"]

    mentors = preview["mentors"]
    assert _mentor(mentors, "M1") is None
    m2 = _mentor(mentors, "M2")
    assert m2["valid_confirmations"] == 1
    assert m2["confirmed_seconds"] == 7200
    assert m2["units"] == 7200 // 2700


def test_revoke_restores_pending_hours_and_clears_workload(client):
    pv = _create_plan(client)
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T10:00:00+08:00",
            ),
            _confirm("E-02", "S1", "E-01", "M1"),
        ],
    )
    confirmed = client.get(f"/api/plans/{pv}/students/S1/progress").json()
    assert confirmed["confirmed_seconds"] == 7200
    assert _preview(client, pv)["mentors"][0]["mentor_id"] == "M1"

    _post_events(client, pv, [_revoke("E-03", "S1", "E-01", mentor_id="M1")])

    progress = client.get(f"/api/plans/{pv}/students/S1/progress").json()
    assert progress["confirmed_seconds"] == 0
    assert progress["pending_seconds"] == 7200

    preview = _preview(client, pv)
    assert preview["entries"] == []
    assert preview["mentors"] == []

    # A mismatched mentor cannot revoke someone else's confirmation.
    _post_events(
        client,
        pv,
        [
            _confirm("E-04", "S1", "E-01", "M1"),
            _revoke("E-05", "S1", "E-01", mentor_id="M9"),
        ],
    )
    preview = _preview(client, pv)
    assert len(preview["entries"]) == 1
    assert preview["skipped"][-1]["reason"] == "mentor_mismatch"


def test_delegation_split_and_revoke(client):
    pv = _create_plan(client)
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T10:00:00+08:00",
            ),
            _checkin(
                "E-02",
                "S1",
                "2024-03-15T14:00:00+08:00",
                "2024-03-15T16:00:00+08:00",
            ),
            _delegate("E-03", "S1", "D-1", "M1", "M2", share_bps=5000),
            # Confirmed while the delegation is active -> split 50/50.
            _confirm("E-04", "S1", "E-01", "M2"),
            _delegate_revoke("E-05", "S1", "D-1"),
            # Confirmed after the revocation -> fully attributed to M2.
            _confirm("E-06", "S1", "E-02", "M2"),
        ],
    )
    preview = _preview(client, pv)
    by_checkin = {}
    for entry in preview["entries"]:
        by_checkin.setdefault(entry["checkin_event_id"], []).append(entry)

    split = {e["role"]: e for e in by_checkin["E-01"]}
    assert set(split) == {"delegator", "delegate"}
    assert split["delegator"]["mentor_id"] == "M1"
    assert split["delegator"]["seconds"] == 3600
    assert split["delegator"]["delegation_id"] == "D-1"
    assert split["delegate"]["mentor_id"] == "M2"
    assert split["delegate"]["seconds"] == 3600

    full = by_checkin["E-02"]
    assert len(full) == 1
    assert full[0]["role"] == "primary"
    assert full[0]["mentor_id"] == "M2"
    assert full[0]["seconds"] == 7200

    m1 = _mentor(preview["mentors"], "M1")
    assert m1["delegator_seconds"] == 3600
    assert m1["valid_confirmations"] == 0
    m2 = _mentor(preview["mentors"], "M2")
    assert m2["delegate_seconds"] == 3600
    assert m2["primary_seconds"] == 7200
    assert m2["confirmed_seconds"] == 10800
    assert m2["valid_confirmations"] == 2


def test_delegation_uses_rule_default_share(client):
    pv = _create_plan(client)
    client.put(
        f"/api/plans/{pv}/settlement-rules",
        json={"seconds_per_unit": 2700, "delegator_share_bps": 2500},
    )
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T10:00:00+08:00",
            ),
            _delegate("E-02", "S1", "D-1", "M1", "M2"),
            _confirm("E-03", "S1", "E-01", "M2"),
        ],
    )
    preview = _preview(client, pv)
    roles = {e["role"]: e for e in preview["entries"]}
    assert roles["delegator"]["seconds"] == 7200 * 2500 // 10000
    assert roles["delegate"]["seconds"] == 7200 - 7200 * 2500 // 10000


def test_cross_month_timezone_attribution(client):
    pv = _create_plan(client)
    # 22:00-02:00 across the March/April boundary in Asia/Shanghai.
    # In UTC the whole session stays inside March 31, so a UTC-based ledger
    # would misattribute the second half.
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-31T22:00:00+08:00",
                "2024-04-01T02:00:00+08:00",
            ),
            _confirm("E-02", "S1", "E-01", "M1"),
        ],
    )
    preview = _preview(client, pv)
    by_day = {e["academic_day"]: e for e in preview["entries"]}
    assert set(by_day) == {"2024-03-31", "2024-04-01"}
    assert by_day["2024-03-31"]["seconds"] == 2 * 3600
    assert by_day["2024-03-31"]["period"] == "2024-03"
    assert by_day["2024-04-01"]["seconds"] == 2 * 3600
    assert by_day["2024-04-01"]["period"] == "2024-04"

    m1 = _mentor(preview["mentors"], "M1")
    assert m1["periods"] == {"2024-03": 7200, "2024-04": 7200}
    assert m1["confirmed_seconds"] == 4 * 3600


def test_concurrent_confirmations_count_once(client):
    pv = _create_plan(client)
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T10:00:00+08:00",
            )
        ],
    )

    from app import services

    def _confirm_concurrently(eid, mentor):
        session = TestSessionLocal()
        try:
            services.import_events(
                session,
                plan_version=pv,
                events=[_confirm(eid, "S1", "E-01", mentor)],
            )
        finally:
            session.close()

    threads = [
        threading.Thread(target=_confirm_concurrently, args=(f"E-C{i}", f"M{i}"))
        for i in range(1, 5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    preview = _preview(client, pv)
    # Exactly one confirmation is final and valid; the rest are skipped.
    assert len(preview["entries"]) == 1
    entry = preview["entries"][0]
    assert entry["confirm_event_id"] == "E-C1"
    assert entry["mentor_id"] == "M1"
    assert entry["seconds"] == 7200
    assert len(preview["mentors"]) == 1
    assert preview["mentors"][0]["valid_confirmations"] == 1
    assert sorted(s["reason"] for s in preview["skipped"]) == [
        "already_confirmed"
    ] * 3


def test_issue_batch_is_immutable_and_idempotent(client):
    pv = _create_plan(client)
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T10:00:00+08:00",
            ),
            _confirm("E-02", "S1", "E-01", "M1"),
        ],
    )
    issued = client.post(f"/api/plans/{pv}/workload-batches/B-1", json={})
    assert issued.status_code == 201, issued.text
    batch = issued.json()
    assert batch["batch_id"] == "B-1"
    assert batch["event_cutoff_id"] == "E-02"
    assert len(batch["entries"]) == 1
    assert _mentor(batch["mentors"], "M1")["confirmed_seconds"] == 7200

    # More confirmations arrive after the cutoff.
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-03",
                "S1",
                "2024-03-16T08:00:00+08:00",
                "2024-03-16T10:00:00+08:00",
            ),
            _confirm("E-04", "S1", "E-03", "M1"),
        ],
    )

    # The frozen batch does not change; re-issuing is idempotent.
    again = client.post(f"/api/plans/{pv}/workload-batches/B-1", json={}).json()
    assert again["entries"] == batch["entries"]
    assert again["event_cutoff_id"] == "E-02"
    stored = client.get(f"/api/plans/{pv}/workload-batches/B-1").json()
    assert _mentor(stored["mentors"], "M1")["confirmed_seconds"] == 7200

    # The live preview reflects the new confirmation.
    assert _mentor(_preview(client, pv)["mentors"], "M1")["confirmed_seconds"] == 14400

    # A new batch captures the later state.
    batch2 = client.post(f"/api/plans/{pv}/workload-batches/B-2", json={}).json()
    assert batch2["event_cutoff_id"] == "E-04"
    assert _mentor(batch2["mentors"], "M1")["confirmed_seconds"] == 14400

    missing = client.get(f"/api/plans/{pv}/workload-batches/NOPE")
    assert missing.status_code == 404


def test_concurrent_issue_only_one_wins(client):
    pv = _create_plan(client)
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T10:00:00+08:00",
            ),
            _confirm("E-02", "S1", "E-01", "M1"),
        ],
    )

    from app import services

    created_flags: list[bool] = []
    lock = threading.Lock()

    def _issue():
        session = TestSessionLocal()
        try:
            _, created = services.issue_workload_batch(
                session, plan_version=pv, batch_id="B-CONCURRENT"
            )
            with lock:
                created_flags.append(created)
        finally:
            session.close()

    threads = [threading.Thread(target=_issue) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for c in created_flags if c) == 1
    assert sum(1 for c in created_flags if not c) == 3

    stored = client.get(f"/api/plans/{pv}/workload-batches/B-CONCURRENT").json()
    assert stored["batch_id"] == "B-CONCURRENT"
    assert len(stored["entries"]) == 1


def test_adjustments_append_without_rewriting_history(client):
    pv = _create_plan(client)
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T10:00:00+08:00",
            ),
            _confirm("E-02", "S1", "E-01", "M1"),
        ],
    )
    client.post(f"/api/plans/{pv}/workload-batches/B-1", json={})
    base_entries = client.get(f"/api/plans/{pv}/workload-batches/B-1").json()[
        "entries"
    ]

    adj = client.post(
        f"/api/plans/{pv}/workload-batches/B-1/adjustments",
        json={
            "adjustment_id": "ADJ-1",
            "mentor_id": "M1",
            "seconds": 1800,
            "reason": "manual review correction",
            "actor": "admin-1",
        },
    )
    assert adj.status_code == 201, adj.text
    assert adj.json()["created"] is True

    # Re-posting the same adjustment id is idempotent.
    dup = client.post(
        f"/api/plans/{pv}/workload-batches/B-1/adjustments",
        json={
            "adjustment_id": "ADJ-1",
            "mentor_id": "M1",
            "seconds": 1800,
            "reason": "manual review correction",
            "actor": "admin-1",
        },
    )
    assert dup.status_code == 201
    assert dup.json()["created"] is False

    batch = client.get(f"/api/plans/{pv}/workload-batches/B-1").json()
    # Base accrual entries are untouched; the correction is an appended entry.
    assert batch["entries"] == base_entries
    assert len(batch["adjustments"]) == 1
    assert batch["adjustments"][0]["adjustment_id"] == "ADJ-1"
    m1 = _mentor(batch["mentors"], "M1")
    assert m1["confirmed_seconds"] == 7200
    assert m1["adjustment_seconds"] == 1800
    assert m1["total_seconds"] == 9000

    # A large negative adjustment clamps the payable total at zero.
    client.post(
        f"/api/plans/{pv}/workload-batches/B-1/adjustments",
        json={
            "adjustment_id": "ADJ-2",
            "mentor_id": "M1",
            "seconds": -99999,
            "reason": "clawback",
            "actor": "admin-1",
        },
    )
    batch = client.get(f"/api/plans/{pv}/workload-batches/B-1").json()
    m1 = _mentor(batch["mentors"], "M1")
    assert m1["adjustment_seconds"] == 1800 - 99999
    assert m1["total_seconds"] == 0
    assert len(batch["adjustments"]) == 2

    # Adjustments require an existing batch.
    missing = client.post(
        f"/api/plans/{pv}/workload-batches/NOPE/adjustments",
        json={
            "adjustment_id": "ADJ-9",
            "mentor_id": "M1",
            "seconds": 1,
        },
    )
    assert missing.status_code == 404


def test_restart_reconcile_consistent(client):
    pv = _create_plan(client)
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-31T22:00:00+08:00",
                "2024-04-01T02:00:00+08:00",
            ),
            _delegate("E-02", "S1", "D-1", "M1", "M2", share_bps=5000),
            _confirm("E-03", "S1", "E-01", "M2"),
        ],
    )
    issued = client.post(f"/api/plans/{pv}/workload-batches/B-1", json={}).json()
    assert len(issued["entries"]) == 4  # 2 academic days x 2 split roles

    # Simulate a service restart: drop all connections and reopen from disk.
    test_engine.dispose()
    session = TestSessionLocal()
    try:
        from app import services

        result = services.reconcile_workload_batch(session, pv, "B-1")
        assert result["consistent"] is True
        assert result["differences"] == []
    finally:
        session.close()

    via_api = client.get(f"/api/plans/{pv}/workload-batches/B-1/reconcile")
    assert via_api.status_code == 200
    assert via_api.json()["consistent"] is True

    # Events arriving after the cutoff do not disturb reconciliation.
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-04",
                "S1",
                "2024-04-02T08:00:00+08:00",
                "2024-04-02T10:00:00+08:00",
            ),
            _confirm("E-05", "S1", "E-04", "M2"),
        ],
    )
    still = client.get(f"/api/plans/{pv}/workload-batches/B-1/reconcile").json()
    assert still["consistent"] is True

    missing = client.get(f"/api/plans/{pv}/workload-batches/NOPE/reconcile")
    assert missing.status_code == 404


def test_mentor_personal_detail_preview_and_batch(client):
    pv = _create_plan(client)
    _post_events(
        client,
        pv,
        [
            _checkin(
                "E-01",
                "S1",
                "2024-03-15T08:00:00+08:00",
                "2024-03-15T10:00:00+08:00",
            ),
            _checkin(
                "E-02",
                "S1",
                "2024-03-15T14:00:00+08:00",
                "2024-03-15T16:00:00+08:00",
            ),
            _delegate("E-03", "S1", "D-1", "M1", "M2", share_bps=5000),
            _confirm("E-04", "S1", "E-01", "M2"),
            _confirm("E-05", "S1", "E-02", "M3"),
        ],
    )

    live = client.get(f"/api/plans/{pv}/mentors/M2/workload")
    assert live.status_code == 200, live.text
    body = live.json()
    assert body["source"] == "preview"
    assert body["batch_id"] is None
    assert body["summary"]["delegate_seconds"] == 3600
    assert {e["mentor_id"] for e in body["entries"]} == {"M2"}

    client.post(f"/api/plans/{pv}/workload-batches/B-1", json={})
    client.post(
        f"/api/plans/{pv}/workload-batches/B-1/adjustments",
        json={
            "adjustment_id": "ADJ-1",
            "mentor_id": "M2",
            "seconds": 900,
            "reason": "rounding correction",
            "actor": "admin-1",
        },
    )
    frozen = client.get(f"/api/plans/{pv}/mentors/M2/workload?batch_id=B-1")
    assert frozen.status_code == 200
    body = frozen.json()
    assert body["source"] == "batch"
    assert body["batch_id"] == "B-1"
    assert body["summary"]["confirmed_seconds"] == 3600
    assert body["summary"]["adjustment_seconds"] == 900
    assert body["summary"]["total_seconds"] == 4500
    assert len(body["adjustments"]) == 1

    # The delegator's kept share is visible in their own detail view.
    delegator = client.get(f"/api/plans/{pv}/mentors/M1/workload").json()
    assert delegator["summary"]["delegator_seconds"] == 3600
    assert delegator["summary"]["valid_confirmations"] == 0

    unknown = client.get(f"/api/plans/{pv}/mentors/NOPE/workload")
    assert unknown.status_code == 404
    missing_batch = client.get(f"/api/plans/{pv}/mentors/M2/workload?batch_id=NOPE")
    assert missing_batch.status_code == 404

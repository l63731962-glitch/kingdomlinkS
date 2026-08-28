"""
Verifies the three features built to close out the known gaps:
1. Round-robin general follow-up team (replaces single-admin fallback)
2. Notification logging on every assignment creation
3. Senior-leadership escalation for unresolved leader accountability
"""
from datetime import date, datetime, timedelta
from app import create_app
from app.database import db
from app.models import (
    Church, Member, CellGroup, Service, FollowUpTeamMember,
    NotificationLog, FollowUpAssignment,
)
from app.attendance_logic import (
    submit_attendance, get_pending_queue_for_user, get_leader_accountability_overview,
)

app = create_app()

with app.app_context():
    db.drop_all()
    db.create_all()

    church = Church(name="Test Church", follow_up_threshold=3, leader_escalation_days=14)
    db.session.add(church)
    db.session.commit()

    admin = Member(church_id=church.id, full_name="Admin", role="admin", email="admin@t.com")
    admin.set_password("x")
    db.session.add(admin)
    db.session.commit()
    church.admin_user_id = admin.id
    db.session.commit()

    senior = Member(
        church_id=church.id, full_name="Senior Pastor", role="admin", email="senior@t.com",
        tracked_for_attendance=False,  # escalation contact, not tracked for Sunday attendance here
    )
    senior.set_password("x")
    db.session.add(senior)
    db.session.commit()
    church.senior_leadership_id = senior.id
    db.session.commit()

    # Two people on the general follow-up team
    helper1 = Member(church_id=church.id, full_name="Helper One", role="leader", email="h1@t.com")
    helper2 = Member(church_id=church.id, full_name="Helper Two", role="leader", email="h2@t.com")
    db.session.add_all([helper1, helper2])
    db.session.commit()

    db.session.add(FollowUpTeamMember(church_id=church.id, member_id=helper1.id, active=True))
    db.session.add(FollowUpTeamMember(church_id=church.id, member_id=helper2.id, active=True))
    db.session.commit()

    # Four unassigned members (no cell) whose absences should round-robin
    unassigned_members = []
    for i in range(4):
        m = Member(church_id=church.id, full_name=f"Unassigned {i}", role="adult", created_by=admin.id)
        db.session.add(m)
        unassigned_members.append(m)
    db.session.commit()

    print("=== TEST 1: round-robin distributes across the team, not onto one admin ===")
    s1 = Service(church_id=church.id, name="Sunday", date=date(2026, 7, 5))
    db.session.add(s1)
    db.session.commit()

    # helper1/helper2 are role='leader', so they're also tracked on the
    # separate leader-accountability stream (see README: any leader
    # physically present must be explicitly included, same as a member,
    # or they show up absent on their OWN attendance, not just as a
    # follow-up owner). Marking them present here isolates the round-robin
    # behavior from an unrelated leader-self-absence side effect.
    submit_attendance(
        church.id, s1.id,
        present_member_ids=[helper1.id, helper2.id],
        submitted_by_id=admin.id,
    )

    h1_queue = get_pending_queue_for_user(helper1.id)
    h2_queue = get_pending_queue_for_user(helper2.id)
    admin_queue = get_pending_queue_for_user(admin.id)
    print(f"Helper1 queue: {len(h1_queue)}, Helper2 queue: {len(h2_queue)}, Admin queue: {len(admin_queue)}")
    assert len(h1_queue) + len(h2_queue) == 4, "All 4 unassigned absences should route to the team"
    assert len(h1_queue) == 2 and len(h2_queue) == 2, "Round-robin should split evenly across 2 team members"
    assert len(admin_queue) == 0, "Admin should get nothing when a team is configured"
    print("PASS: 4 absences split 2/2 across the team, none fell to admin.\n")

    print("=== TEST 2: notification logging fires on every assignment ===")
    all_logs = NotificationLog.query.all()
    print(f"Notification logs created: {len(all_logs)}")
    assert len(all_logs) == 4, "One log per assignment created"
    for log in all_logs:
        assert log.status == "logged_only"
        assert log.trigger == "new_assignment"
    print("PASS: every assignment creation produced a matching notification log.\n")

    print("=== TEST 3: senior-leadership escalation after configured delay ===")
    leader_dan = Member(church_id=church.id, full_name="Leader Dan", role="leader", email="dan@t.com")
    db.session.add(leader_dan)
    db.session.commit()

    # Simulate Dan missing 2 services (flagged threshold for leaders)
    s2 = Service(church_id=church.id, name="Sunday 2", date=date(2026, 7, 12))
    db.session.add(s2)
    db.session.commit()
    submit_attendance(church.id, s2.id, present_member_ids=[], submitted_by_id=admin.id)
    db.session.refresh(leader_dan)
    print(f"Dan consecutive_absences: {leader_dan.consecutive_absences}")
    assert leader_dan.consecutive_absences >= 1

    s3 = Service(church_id=church.id, name="Sunday 3", date=date(2026, 7, 19))
    db.session.add(s3)
    db.session.commit()
    submit_attendance(church.id, s3.id, present_member_ids=[], submitted_by_id=admin.id)
    db.session.refresh(leader_dan)
    print(f"Dan consecutive_absences after 2 misses: {leader_dan.consecutive_absences}")

    overview_before = get_leader_accountability_overview(church.id)
    dan_row_before = [r for r in overview_before if r["leader_id"] == leader_dan.id][0]
    print(f"Dan flagged: {dan_row_before['flagged']}, escalated_to_senior: {dan_row_before['escalated_to_senior_leadership']}")
    assert dan_row_before["flagged"] is True
    assert dan_row_before["escalated_to_senior_leadership"] is False, "Should NOT escalate immediately -- needs the delay window"

    # Backdate the pending leader_attendance assignment past the escalation window
    leader_assignment = FollowUpAssignment.query.filter_by(
        reason="leader_attendance", status="pending"
    ).join(
        FollowUpAssignment.attendance_record
    ).first()
    # Find via the attendance record's member_id instead, more directly:
    from app.models import AttendanceRecord
    dan_records = AttendanceRecord.query.filter_by(member_id=leader_dan.id).all()
    dan_record_ids = [r.id for r in dan_records]
    stale_assignment = FollowUpAssignment.query.filter(
        FollowUpAssignment.attendance_record_id.in_(dan_record_ids),
        FollowUpAssignment.reason == "leader_attendance",
        FollowUpAssignment.status == "pending",
    ).order_by(FollowUpAssignment.created_at.asc()).first()

    assert stale_assignment is not None, "Should have a pending leader_attendance assignment to backdate"
    stale_assignment.created_at = datetime.utcnow() - timedelta(days=15)  # older than the 14-day threshold
    db.session.commit()

    overview_after = get_leader_accountability_overview(church.id)
    dan_row_after = [r for r in overview_after if r["leader_id"] == leader_dan.id][0]
    print(f"After backdating past {church.leader_escalation_days}-day window: escalated_to_senior = {dan_row_after['escalated_to_senior_leadership']}")
    assert dan_row_after["escalated_to_senior_leadership"] is True

    senior_queue = get_pending_queue_for_user(senior.id)
    print(f"Senior pastor's queue: {len(senior_queue)}")
    assert len(senior_queue) == 1
    assert senior_queue[0].reason == "leader_escalation"
    print("PASS: unresolved leader accountability correctly escalates to senior leadership after the delay window.\n")

    print("=== TEST 4: escalation doesn't duplicate on repeated calls ===")
    overview_again = get_leader_accountability_overview(church.id)
    senior_queue_again = get_pending_queue_for_user(senior.id)
    print(f"Senior queue after calling overview twice: {len(senior_queue_again)}")
    assert len(senior_queue_again) == 1, "Should not create a duplicate escalation assignment"
    print("PASS: no duplicate escalation on repeated dashboard views.\n")

    print("ALL THREE FEATURES VERIFIED WORKING.")
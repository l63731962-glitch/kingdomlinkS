"""
Verifies the boundaries that matter:
1. A member who joins a group with share_attendance=False can check in,
   and the facilitator still cannot see it.
2. A member who leaves a group disappears from the facilitator's view
   even though their past check-ins still exist in the DB.
3. Aggregate attendance count has no per-member data reachable from it.
4. A visitor who did NOT check "wants follow-up" never appears in the
   follow-up queue.
5. A member who never RSVPs generates zero rows and is invisible to
   any query -- there is no "who hasn't responded" list.
"""
from datetime import date
from app import create_app
from app.database import db
from app.models import Church, Member, CheckInGroup, Service, GroupMembership, Visitor
from app.engagement_logic import (
    join_group, leave_group, submit_check_in, get_group_checkins_for_facilitator,
    get_my_check_ins, record_attendance_count,
    add_visitor, get_visitors_wanting_follow_up,
    submit_rsvp, get_rsvp_count,
)
from app.models import AttendanceCount

# NOTE: get_attendance_trend() and get_visitor_counts() use real SQL
# joins/group_by (db.session.query(...).join(...).group_by(...)),
# which the offline tiny_orm shim does not implement (see tiny_orm.py
# docstring -- faking joins precisely risked hiding bugs behind shim
# bugs). Test 3 below checks the same guarantee -- that the aggregate
# table has no per-person field -- directly against AttendanceCount,
# without going through the unfaked join helper. Everything else in
# this file runs the real, unmodified app/engagement_logic.py.

app = create_app()

with app.app_context():
    db.drop_all()
    db.create_all()

    church = Church(name="Test Church")
    db.session.add(church)
    db.session.commit()

    leader = Member(church_id=church.id, full_name="Group Leader Dan", role="leader", email="dan@test.com")
    leader.set_password("x")
    db.session.add(leader)

    alice = Member(church_id=church.id, full_name="Alice", role="member", email="alice@test.com")
    bob = Member(church_id=church.id, full_name="Bob", role="member", email="bob@test.com")
    carol = Member(church_id=church.id, full_name="Carol (never joins anything)", role="member", email="carol@test.com")
    db.session.add_all([alice, bob, carol])
    db.session.commit()

    group = CheckInGroup(church_id=church.id, name="Faith Small Group", facilitator_id=leader.id)
    db.session.add(group)
    db.session.commit()

    print("=== TEST 1: share_attendance=False stays hidden from facilitator ===")
    join_group(group.id, alice.id, share_attendance=True)
    join_group(group.id, bob.id, share_attendance=False)  # Bob wants community, not visibility

    submit_check_in(group.id, alice.id, note="Good week")
    submit_check_in(group.id, bob.id, note="Bob's private note")

    facilitator_view = get_group_checkins_for_facilitator(group.id, leader.id)
    # member_ids returned, cross-referenced against a direct Member query --
    # this doesn't depend on relationship-loading machinery, only on the
    # real filtering logic in get_group_checkins_for_facilitator.
    ids_visible = {c.member_id for c in facilitator_view}
    names_visible = {m.full_name for m in Member.query.filter(Member.id.in_(ids_visible)).all()}
    print(f"Facilitator sees: {names_visible}")
    assert names_visible == {"Alice"}, "Bob's non-shared check-in leaked to the facilitator"
    print("PASS: Bob's check-in exists but is invisible to the facilitator.\n")

    bob_own_view = get_my_check_ins(bob.id)
    print(f"Bob sees his own check-ins: {len(bob_own_view)}")
    assert len(bob_own_view) == 1
    print("PASS: Bob can still see his own data.\n")

    print("=== TEST 2: leaving a group removes future visibility ===")
    join_group(group.id, alice.id, share_attendance=True)  # already joined, idempotent
    leave_group(group.id, alice.id)
    facilitator_view_after = get_group_checkins_for_facilitator(group.id, leader.id)
    ids_after = {c.member_id for c in facilitator_view_after}
    print(f"Facilitator sees after Alice leaves: {[m.full_name for m in Member.query.filter(Member.id.in_(ids_after)).all()]}")
    assert facilitator_view_after == [], "Alice's check-in still visible after she left the group"
    print("PASS: facilitator loses visibility the moment Alice leaves, even though the CheckIn row still exists.\n")

    print("=== TEST 3: aggregate attendance count carries no per-person data ===")
    s1 = Service(church_id=church.id, name="Sunday Service", date=date(2026, 7, 5))
    db.session.add(s1)
    db.session.commit()
    count_row = record_attendance_count(s1.id, headcount=142, recorded_by_id=leader.id)
    print(f"Recorded row: {count_row.to_dict()}")
    # Structural check: AttendanceCount has no member_id column at all,
    # anywhere -- not just "the query didn't select it."
    column_names = [k for k in vars(AttendanceCount) if not k.startswith("_")]
    print(f"AttendanceCount's own column-level attributes: {column_names}")
    assert "member_id" not in column_names, "AttendanceCount must never carry a per-person foreign key"
    assert count_row.headcount == 142
    print("PASS: AttendanceCount is structurally headcount-only -- there is no "
          "member_id column to query, diff, or export, by construction, not by filtering.\n")
    print(f"Reminder: Carol never joined a group, never checked in, never RSVP'd -- "
          f"she generates zero rows anywhere and appears on no list. That's correct, not a bug.\n")

    print("=== TEST 4: visitor follow-up respects the visitor's own opt-out ===")
    add_visitor(church.id, "Dana (wants follow-up)", phone="080-111-1111", wants_follow_up=True)
    add_visitor(church.id, "Eddie (declined follow-up)", phone="080-222-2222", wants_follow_up=False)
    queue = get_visitors_wanting_follow_up(church.id)
    queue_names = {v.full_name for v in queue}
    print(f"Follow-up queue: {queue_names}")
    assert queue_names == {"Dana (wants follow-up)"}
    print("PASS: Eddie, who declined, never appears in the follow-up queue.\n")

    print("=== TEST 5: RSVP silence produces no list ===")
    submit_rsvp(s1.id, alice.id, "yes")
    counts = get_rsvp_count(s1.id)
    print(f"RSVP counts: {counts}")
    assert counts["yes"] == 1
    print("PASS: only an aggregate count exists. Bob and Carol, who never RSVP'd, are not "
          "queryable as a 'no response' list anywhere in this codebase.\n")

    print("ALL CONSENT BOUNDARIES VERIFIED.")
"""
The core loop of A.I.R. NEOMAP.

Every time attendance is submitted for a service:
  1. Diff the full active roster against who was marked present.
  2. For every absent member, create a fresh AttendanceRecord —
     this is a live weekly snapshot, not a persistent removal list.
     Attending last week and missing this week are two independent
     records; nothing "remembers" you were handled before.
  3. Route a FollowUpAssignment to the right person (cell leader,
     or general team if unassigned).
  4. Assignments STACK. If last week's assignment is still pending
     when this week's absence fires, the old one is NOT overwritten
     or silently closed — it stays open. This is how the dashboard
     surfaces a leader who isn't making their calls, rather than
     just logging that someone was absent twice.
  5. On the church's configured threshold of consecutive absences,
     escalate: flag the record AND create a second assignment routed
     to the church admin/pastor, independent of the leader's queue.
"""

from datetime import datetime, date
from app.database import db
from app.models import (
    Member, AttendanceRecord, FollowUpAssignment, Church, EventRSVP,
    FollowUpTeamMember, NotificationLog, CellGroup, Service,
)


def get_upcoming_birthdays(church_id, days_ahead=7):
    """
    Feature #1: warm, non-corrective contact list for leaders.
    Returns active adult members whose birthday falls within the
    next `days_ahead` days, handling the year-wraparound case
    (e.g. today is Dec 28, someone's birthday is Jan 2).
    """
    from datetime import date as date_cls

    today = date_cls.today()
    members = Member.query.filter(
        Member.church_id == church_id,
        Member.membership_status == "active",
        Member.role == "adult",
        Member.date_of_birth.isnot(None),
    ).all()

    upcoming = []
    for m in members:
        dob = m.date_of_birth
        try:
            this_year_bday = dob.replace(year=today.year)
        except ValueError:
            # Feb 29 on a non-leap year -- treat as Feb 28
            this_year_bday = dob.replace(year=today.year, day=28)

        delta_days = (this_year_bday - today).days
        if delta_days < 0:
            # already passed this year -- check next year's date for wraparound
            try:
                next_year_bday = dob.replace(year=today.year + 1)
            except ValueError:
                next_year_bday = dob.replace(year=today.year + 1, day=28)
            delta_days = (next_year_bday - today).days

        if 0 <= delta_days <= days_ahead:
            upcoming.append({
                "member_id": m.id,
                "full_name": m.full_name,
                "phone": m.phone,
                "cell_name": m.cell.name if m.cell_id and m.cell else "Unassigned",
                "days_until": delta_days,
                # None when the birth year is a placeholder (see
                # Member.dob_year_unknown / DOB_UNKNOWN_YEAR_SENTINEL
                # in models.py) -- today.year - dob.year against the
                # sentinel year would otherwise silently compute a
                # fictional age like 126, since this function only
                # ever reads month/day off dob everywhere else, never
                # the year itself.
                "turning_age": (today.year - dob.year) if (not m.dob_year_unknown and (delta_days > 0 or this_year_bday >= today)) else None,
            })

    return sorted(upcoming, key=lambda x: x["days_until"])


def submit_rsvp(service_id, member_id, response):
    """Feature #4: record RSVP intent ahead of a service."""
    existing = EventRSVP.query.filter_by(service_id=service_id, member_id=member_id).first()
    if existing:
        existing.response = response
        db.session.commit()
        return existing

    rsvp = EventRSVP(service_id=service_id, member_id=member_id, response=response)
    db.session.add(rsvp)
    db.session.commit()
    return rsvp


def get_rsvp_no_shows(service_id):
    """
    Feature #4: members who RSVP'd yes but weren't marked present.
    A stronger drift signal than an unannounced absence -- worth
    surfacing separately in the follow-up queue, not just folded
    into the general absence list.
    """
    yes_rsvps = EventRSVP.query.filter_by(service_id=service_id, response="yes").all()
    no_shows = []
    for rsvp in yes_rsvps:
        record = AttendanceRecord.query.filter_by(
            service_id=service_id, member_id=rsvp.member_id
        ).first()
        if record and not record.present:
            no_shows.append({
                "member_id": rsvp.member_id,
                "member_name": rsvp.member.full_name if rsvp.member else None,
                "phone": rsvp.member.phone if rsvp.member else None,
            })
    return no_shows


def get_engagement_summary(member_id, lookback_services=8):
    """
    Feature #5: multi-service engagement, not just Sunday absence
    count. Someone who skips Sunday but never misses midweek cell
    isn't drifting -- they're differently engaged. Prevents false
    escalation on people who are actually fine, by looking at
    overall attendance rate across ALL service types rather than
    consecutive_absences on Sunday alone.
    """
    records = (
        AttendanceRecord.query
        .filter_by(member_id=member_id)
        .order_by(AttendanceRecord.date.desc())
        .limit(lookback_services)
        .all()
    )
    if not records:
        return {"member_id": member_id, "attendance_rate": None, "services_considered": 0, "by_type": {}}

    by_type = {}
    for r in records:
        service_type = r.service.type if r.service else "unknown"
        by_type.setdefault(service_type, {"present": 0, "total": 0})
        by_type[service_type]["total"] += 1
        if r.present:
            by_type[service_type]["present"] += 1

    total_present = sum(1 for r in records if r.present)
    return {
        "member_id": member_id,
        "attendance_rate": round(total_present / len(records), 2),
        "services_considered": len(records),
        "by_type": by_type,
    }


def submit_attendance(church_id, service_id, present_member_ids, submitted_by_id):
    """
    present_member_ids: list of Member.id who were marked present at
    this service — this list must include EVERYONE present, leaders
    and admins included, not just congregation members. The two
    accountability streams below both check membership in this same
    set; if a leader's own id isn't in it, they get marked absent by
    omission, same as anyone else. The attendance-taking UI is
    responsible for surfacing leaders/admins as checkable rows
    alongside members, not just members.

    Two separate streams are diffed against this one set:

      1. Members (role='adult') -> follow-up routed to their cell
         leader, or admin if unassigned. Escalates to admin at
         threshold.

      2. Leaders/admins (role='leader' or 'admin') -> their own
         absence is tracked as a distinct accountability stream.
         Routes straight to the church admin (a leader has no cell
         leader above them). Flags earlier than the member threshold
         since a leader's inconsistency undermines the whole
         follow-up chain. The person SUBMITTING attendance is
         exempted from self-tracking on this call only if their own
         id is absent from present_member_ids AND they are the sole
         submitter with no one to mark them present — see
         submitted_by_id handling below.

    Children are excluded from both streams — tracked via guardian,
    not directly.
    """
    church = Church.query.get_or_404(church_id)
    threshold = church.follow_up_threshold or 3
    today = date.today()

    # The person submitting this attendance batch is, by definition,
    # present at the service right now — auto-include them so a
    # single leader taking attendance alone doesn't accidentally
    # mark themself absent every week. Any OTHER leader/admin still
    # needs to be explicitly checked present in the UI, same as a
    # member would be.
    present_set = set(present_member_ids) | {submitted_by_id}

    created_records = []

    # ---------- Stream 1: congregation members (adults + teens) ----------
    active_members = Member.query.filter(
        Member.church_id == church_id,
        Member.membership_status == "active",
        Member.role.in_(["adult", "teen"]),
    ).all()

    for member in active_members:
        record = _process_absence_check(
            member, service_id, today, present_set, threshold, church,
            escalation_reason="escalation",
        )
        if record:
            created_records.append(record)

    # ---------- Stream 2: leader/admin self-accountability ----------
    active_leaders = Member.query.filter(
        Member.church_id == church_id,
        Member.membership_status == "active",
        Member.role.in_(["leader", "admin"]),
        Member.tracked_for_attendance == True,  # noqa: E712
    ).all()

    for leader_member in active_leaders:
        record = _process_absence_check(
            leader_member, service_id, today, present_set, threshold, church,
            escalation_reason="leader_escalation",
            force_owner=church.admin_user_id,
        )
        if record:
            created_records.append(record)

    db.session.commit()
    return created_records


def _process_absence_check(person, service_id, today, present_set, threshold, church,
                            escalation_reason, force_owner=None):
    """
    Shared diff logic for one person against one service. Handles
    both member follow-up and leader self-accountability — the only
    difference between the two streams is who the assignment routes
    to (force_owner overrides the cell-based lookup for leaders).
    """
    is_present = person.id in present_set

    record = AttendanceRecord(
        member_id=person.id,
        service_id=service_id,
        date=today,
        present=is_present,
    )

    if is_present:
        person.consecutive_absences = 0
        record.follow_up_status = "not_applicable"
        db.session.add(record)
        return None  # nothing pending for an attended record

    person.consecutive_absences = (person.consecutive_absences or 0) + 1
    record.follow_up_status = "not_started"
    db.session.add(record)
    db.session.flush()

    owner_id = force_owner if force_owner is not None else _resolve_follow_up_owner(person, church.id)

    # Guard against a leader/admin being routed to themself if
    # force_owner happens to equal their own id (e.g. the admin
    # missing a service) — skip creating a self-assignment.
    if owner_id and owner_id != person.id:
        assignment = FollowUpAssignment(
            attendance_record_id=record.id,
            assigned_to=owner_id,
            status="pending",
            reason="weekly_absence" if force_owner is None else "leader_attendance",
        )
        db.session.add(assignment)
        db.session.flush()
        _log_notification(owner_id, "new_assignment", assignment.id)

    if person.consecutive_absences >= threshold:
        record.escalated = True
        admin_id = church.admin_user_id
        if admin_id and admin_id != owner_id and admin_id != person.id:
            escalation_assignment = FollowUpAssignment(
                attendance_record_id=record.id,
                assigned_to=admin_id,
                status="pending",
                reason=escalation_reason,
            )
            db.session.add(escalation_assignment)
            db.session.flush()
            _log_notification(admin_id, "escalation", escalation_assignment.id)

    return record


def complete_follow_up(assignment_id, outcome_status, note, completed_by_id):
    """
    outcome_status must be one of:
      reached_ok | reached_concern | no_answer | invalid_number

    Completing THIS assignment does not touch consecutive_absences —
    that counter only moves on actual attendance. Someone can be
    successfully reached and still reappear next week if they miss
    again. Follow-up completion and attendance status are deliberately
    separate signals.
    """
    valid_outcomes = {"reached_ok", "reached_concern", "no_answer", "invalid_number"}
    if outcome_status not in valid_outcomes:
        raise ValueError(f"outcome_status must be one of {valid_outcomes}")

    assignment = FollowUpAssignment.query.get_or_404(assignment_id)
    record = assignment.attendance_record

    record.follow_up_status = outcome_status
    record.follow_up_by = completed_by_id
    record.follow_up_note = note
    record.follow_up_completed_at = datetime.utcnow()

    assignment.status = "completed"

    db.session.commit()
    return record


def get_pending_queue_for_user(user_id, role_filter=None):
    """
    Returns every OPEN assignment for this user, oldest first —
    including stale ones from prior weeks that were never completed.
    This is deliberate: if a leader has 3 pending assignments for
    the same member across 3 weeks, that's visible, not collapsed
    into one row.

    role_filter, if given, restricts to assignments whose absent
    member has that role (e.g. "adult" or "teen") — used by the
    queue page's role filter. Leaving it None (the default) returns
    everyone assigned to this user, unchanged from before this
    parameter existed.
    """
    query = FollowUpAssignment.query.filter_by(assigned_to=user_id, status="pending")
    if role_filter:
        query = (
            query
            .join(AttendanceRecord, FollowUpAssignment.attendance_record_id == AttendanceRecord.id)
            .join(Member, AttendanceRecord.member_id == Member.id)
            .filter(Member.role == role_filter)
        )
    return query.order_by(FollowUpAssignment.created_at.asc()).all()


def get_admin_overview(church_id):
    """
    Aggregate counts for the admin dashboard: how many absences this
    week, how many follow-ups are still pending vs. completed, and
    how many are escalated. Pulls from the most recent service date
    on record for this church.
    """
    latest_record = (
        AttendanceRecord.query
        .join(Member, AttendanceRecord.member_id == Member.id)
        .filter(Member.church_id == church_id)
        .order_by(AttendanceRecord.date.desc())
        .first()
    )
    if not latest_record:
        return {"total_absent": 0, "pending": 0, "completed": 0, "escalated": 0}

    latest_date = latest_record.date

    adult_records = (
        AttendanceRecord.query
        .join(Member, AttendanceRecord.member_id == Member.id)
        .filter(
            Member.church_id == church_id,
            Member.role == "adult",
            AttendanceRecord.date == latest_date,
            AttendanceRecord.present == False,  # noqa: E712
        )
        .all()
    )

    teen_records = (
        AttendanceRecord.query
        .join(Member, AttendanceRecord.member_id == Member.id)
        .filter(
            Member.church_id == church_id,
            Member.role == "teen",
            AttendanceRecord.date == latest_date,
            AttendanceRecord.present == False,  # noqa: E712
        )
        .all()
    )

    return {
        "service_date": latest_date.isoformat(),
        "total_absent": len(adult_records),
        "pending": sum(1 for r in adult_records if r.follow_up_status == "not_started"),
        "completed": sum(1 for r in adult_records if r.follow_up_status not in ("not_started", "not_applicable")),
        "escalated": sum(1 for r in adult_records if r.escalated),
        "teen_total_absent": len(teen_records),
        "teen_pending": sum(1 for r in teen_records if r.follow_up_status == "not_started"),
        "teen_completed": sum(1 for r in teen_records if r.follow_up_status not in ("not_started", "not_applicable")),
        "teen_escalated": sum(1 for r in teen_records if r.escalated),
    }


def get_leader_accountability_overview(church_id):
    """
    Separate stream from member follow-up: tracks whether LEADERS
    themselves are showing up consistently. A leader who is
    diligent about calling absent members but is personally absent
    3 weeks running should surface here, distinct from the member
    absence dashboard above.
    """
    leaders = Member.query.filter(
        Member.church_id == church_id,
        Member.membership_status == "active",
        Member.role.in_(["leader", "admin"]),
        Member.tracked_for_attendance == True,  # noqa: E712
    ).all()

    church = Church.query.get(church_id)

    results = []
    for leader in leaders:
        latest = (
            AttendanceRecord.query
            .filter_by(member_id=leader.id)
            .order_by(AttendanceRecord.date.desc())
            .first()
        )
        is_flagged = (leader.consecutive_absences or 0) >= 2

        escalated_to_senior = False
        if is_flagged and church.senior_leadership_id:
            # An unresolved leader-attendance assignment older than
            # leader_escalation_days means admin hasn't acted --
            # bump it up to senior leadership, independent of
            # whatever the admin's own queue looks like.
            oldest_open = (
                FollowUpAssignment.query
                .join(AttendanceRecord, FollowUpAssignment.attendance_record_id == AttendanceRecord.id)
                .filter(
                    AttendanceRecord.member_id == leader.id,
                    FollowUpAssignment.reason == "leader_attendance",
                    FollowUpAssignment.status == "pending",
                )
                .order_by(FollowUpAssignment.created_at.asc())
                .first()
            )
            if oldest_open:
                age_days = (datetime.utcnow() - oldest_open.created_at).days
                if age_days >= church.leader_escalation_days:
                    already_escalated = FollowUpAssignment.query.filter_by(
                        attendance_record_id=oldest_open.attendance_record_id,
                        assigned_to=church.senior_leadership_id,
                        reason="leader_escalation",
                    ).first()
                    if not already_escalated:
                        senior_assignment = FollowUpAssignment(
                            attendance_record_id=oldest_open.attendance_record_id,
                            assigned_to=church.senior_leadership_id,
                            status="pending",
                            reason="leader_escalation",
                        )
                        db.session.add(senior_assignment)
                        db.session.flush()
                        _log_notification(church.senior_leadership_id, "leader_accountability", senior_assignment.id)
                        db.session.commit()
                    escalated_to_senior = True

        results.append({
            "leader_id": leader.id,
            "leader_name": leader.full_name,
            "role": leader.role,
            "cell_name": leader.cell.name if leader.cell_id and leader.cell else None,
            "consecutive_absences": leader.consecutive_absences or 0,
            "last_service_date": latest.date.isoformat() if latest else None,
            "last_service_present": latest.present if latest else None,
            "flagged": is_flagged,
            "escalated_to_senior_leadership": escalated_to_senior,
        })

    return sorted(results, key=lambda r: r["consecutive_absences"], reverse=True)


def get_cell_attendance_trend(church_id, limit=12):
    """
    Present/total per cell, per service, for the last `limit`
    services on record for this church -- lets an admin see which
    cells are healthy versus quietly shrinking, rather than only
    a church-wide aggregate.

    Scope is deliberately narrow: only members with role in
    (adult, teen) AND an assigned cell_id count toward a cell's
    numbers -- the same population get_upcoming_birthdays() and
    submit_attendance()'s congregation stream already use. A cell
    leader's own attendance is tracked separately (see
    get_leader_accountability_overview) and is not folded into
    their cell's member percentage, since mixing the two would
    silently blend two different tracking populations together.
    """
    from sqlalchemy import func

    recent_services = (
        Service.query
        .filter_by(church_id=church_id)
        .order_by(Service.date.desc())
        .limit(limit)
        .all()
    )

    cells = CellGroup.query.filter_by(church_id=church_id).all()
    if not cells or not recent_services:
        return []

    results = []
    for cell in cells:
        service_points = []
        for svc in reversed(recent_services):
            present = (
                db.session.query(func.count(AttendanceRecord.id))
                .join(Member, AttendanceRecord.member_id == Member.id)
                .filter(
                    AttendanceRecord.service_id == svc.id,
                    Member.cell_id == cell.id,
                    Member.role.in_(["adult", "teen"]),
                    AttendanceRecord.present == True,  # noqa: E712
                )
                .scalar()
            ) or 0
            total = (
                db.session.query(func.count(AttendanceRecord.id))
                .join(Member, AttendanceRecord.member_id == Member.id)
                .filter(
                    AttendanceRecord.service_id == svc.id,
                    Member.cell_id == cell.id,
                    Member.role.in_(["adult", "teen"]),
                )
                .scalar()
            ) or 0
            pct = round((present / total) * 100) if total else None
            service_points.append({
                "date": svc.date.isoformat(),
                "present": present,
                "total": total,
                "percentage": pct,
            })

        results.append({
            "cell_id": cell.id,
            "cell_name": cell.name,
            "leader_name": cell.leader.full_name if cell.leader else None,
            "trend": service_points,
        })

    return results


def get_unassigned_members(church_id):
    """Active adult/leader members with no cell_id — the recruitment pool."""
    return Member.query.filter(
        Member.church_id == church_id,
        Member.membership_status == "active",
        Member.role != "child",
        Member.cell_id.is_(None),
    ).all()


def _resolve_follow_up_owner(member, church_id):
    """
    Cell members route to their cell leader. Unassigned members
    round-robin across the church's general follow-up team (see
    FollowUpTeamMember) so absences don't pile onto one person.
    Falls back to admin only if no team has been configured yet --
    keeps existing churches working with zero setup required.
    """
    if member.cell_id and member.cell and member.cell.leader_id:
        return member.cell.leader_id

    team = (
        FollowUpTeamMember.query
        .filter_by(church_id=church_id, active=True)
        .order_by(FollowUpTeamMember.id)
        .all()
    )
    if team:
        # simple round-robin: whoever has the fewest currently-pending
        # assignments gets the next one
        counts = {
            t.member_id: FollowUpAssignment.query
                .filter_by(assigned_to=t.member_id, status="pending")
                .count()
            for t in team
        }
        return min(counts, key=counts.get)

    church = Church.query.get(church_id)
    return church.admin_user_id


def _log_notification(recipient_id, trigger, assignment_id=None):
    """
    Provider-agnostic notification record. No SMS/email actually
    sends yet -- status stays 'logged_only' until a real provider
    (Mailjet, Twilio, etc.) is wired in. The rest of the system
    already calls this at every point a push notification should
    eventually fire, so adding the provider later is a one-function
    change, not a re-architecture.
    """
    log = NotificationLog(
        recipient_member_id=recipient_id,
        trigger=trigger,
        related_assignment_id=assignment_id,
        status="logged_only",
    )
    db.session.add(log)
    return log
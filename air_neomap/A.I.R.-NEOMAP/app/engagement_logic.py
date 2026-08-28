"""
Every function here answers "what is this specific person allowed
to see," and the answer is always derived from a consent record
(GroupMembership.share_attendance, Visitor.wants_follow_up, or the
member's own RSVP/check-in) — never from a global roster diff.
"""

from datetime import date
from app.database import db
from app.models import (
    Member, CheckInGroup, GroupMembership, CheckIn,
    Service, AttendanceCount, Visitor, EventRSVP,
)


# ---------- 1. Opt-in engagement ----------

def join_group(group_id, member_id, share_attendance=True):
    existing = GroupMembership.query.filter_by(group_id=group_id, member_id=member_id).first()
    if existing:
        return existing
    membership = GroupMembership(
        group_id=group_id, member_id=member_id, share_attendance=share_attendance
    )
    db.session.add(membership)
    db.session.commit()
    return membership


def leave_group(group_id, member_id):
    """Deleting this row is immediate and total: the facilitator
    loses visibility into this member from this point on."""
    membership = GroupMembership.query.filter_by(group_id=group_id, member_id=member_id).first()
    if not membership:
        raise ValueError("Not a member of this group")
    db.session.delete(membership)
    db.session.commit()


def submit_check_in(group_id, member_id, note=None, check_date=None):
    """Only the member themselves calls this, for themselves.
    Route-level auth must guarantee member_id == the caller's own id."""
    membership = GroupMembership.query.filter_by(group_id=group_id, member_id=member_id).first()
    if not membership:
        raise ValueError("Must join the group before checking in")

    check_in = CheckIn(
        group_id=group_id,
        member_id=member_id,
        date=check_date or date.today(),
        note=note,
    )
    db.session.add(check_in)
    db.session.commit()
    return check_in


def get_group_checkins_for_facilitator(group_id, facilitator_id):
    """
    Returns check-ins only for members who (a) are still in the
    group and (b) have share_attendance=True. A member who joined
    with share_attendance=False, or who has since left, never
    appears here regardless of what check-ins they submitted.
    """
    group = CheckInGroup.query.get(group_id)
    if not group or group.facilitator_id != facilitator_id:
        raise PermissionError("Not the facilitator of this group")

    sharing_member_ids = {
        m.member_id for m in GroupMembership.query.filter_by(
            group_id=group_id, share_attendance=True
        ).all()
    }

    check_ins = CheckIn.query.filter_by(group_id=group_id).order_by(CheckIn.date.desc()).all()
    return [c for c in check_ins if c.member_id in sharing_member_ids]


def get_my_check_ins(member_id, group_id=None):
    """A member's own check-ins, always fully visible to themselves."""
    q = CheckIn.query.filter_by(member_id=member_id)
    if group_id:
        q = q.filter_by(group_id=group_id)
    return q.order_by(CheckIn.date.desc()).all()


# ---------- 2. Aggregate-only dashboards ----------

def record_attendance_count(service_id, headcount, recorded_by_id):
    """
    Records a single number. There is no member_id parameter
    anywhere in this function's signature or in AttendanceCount
    itself — it is structurally impossible to derive a per-person
    list from this table.
    """
    existing = AttendanceCount.query.filter_by(service_id=service_id).first()
    if existing:
        existing.headcount = headcount
        existing.recorded_by = recorded_by_id
    else:
        existing = AttendanceCount(
            service_id=service_id, headcount=headcount, recorded_by=recorded_by_id
        )
        db.session.add(existing)
    db.session.commit()
    return existing


def get_attendance_trend(church_id, limit=12):
    """Chart-ready series: date + headcount, nothing else."""
    rows = (
        db.session.query(AttendanceCount, Service)
        .join(Service, AttendanceCount.service_id == Service.id)
        .filter(Service.church_id == church_id)
        .order_by(Service.date.desc())
        .limit(limit)
        .all()
    )
    return [
        {"date": svc.date.isoformat(), "service_name": svc.name, "headcount": cnt.headcount}
        for cnt, svc in reversed(rows)
    ]


def get_cell_sizes(church_id):
    """Group sizes by headcount only — who's in which group is
    visible to that group's own facilitator via a separate,
    scoped endpoint, not here."""
    groups = CheckInGroup.query.filter_by(church_id=church_id).all()
    return [
        {"group_id": g.id, "name": g.name, "group_type": g.group_type, "member_count": len(g.memberships)}
        for g in groups
    ]


def get_visitor_counts(church_id, limit_weeks=12):
    """Aggregate visitor counts by week, no names."""
    from sqlalchemy import func
    rows = (
        db.session.query(Visitor.date_visited, func.count(Visitor.id))
        .filter(Visitor.church_id == church_id)
        .group_by(Visitor.date_visited)
        .order_by(Visitor.date_visited.desc())
        .limit(limit_weeks)
        .all()
    )
    return [{"date": d.isoformat(), "visitor_count": c} for d, c in reversed(rows)]


# ---------- 3. Visitor follow-up (separate from member tracking) ----------

def add_visitor(church_id, full_name, phone=None, email=None, wants_follow_up=True):
    """A visitor card. wants_follow_up defaults True but is the
    visitor's own checkbox on the physical/digital card — the
    route layer should pass through whatever they actually chose."""
    visitor = Visitor(
        church_id=church_id,
        full_name=full_name,
        phone=phone,
        email=email,
        wants_follow_up=wants_follow_up,
    )
    db.session.add(visitor)
    db.session.commit()
    return visitor


def get_visitors_wanting_follow_up(church_id):
    """The ONLY list this app ever auto-surfaces contact info on —
    and every row in it exists because the person asked to be on it."""
    return (
        Visitor.query.filter_by(church_id=church_id, wants_follow_up=True)
        .filter(Visitor.follow_up_status == "not_contacted")
        .order_by(Visitor.date_visited.desc())
        .all()
    )


def mark_visitor_followed_up(visitor_id, status, note=None):
    visitor = Visitor.query.get(visitor_id)
    if not visitor:
        raise ValueError("Visitor not found")
    visitor.follow_up_status = status
    if note:
        visitor.follow_up_note = note
    db.session.commit()
    return visitor


def convert_visitor_to_member(visitor_id, member_id):
    """Church's own record-keeping pointer; does not retroactively
    apply any of this app's tracking to the new Member — the new
    Member starts with a blank slate like everyone else."""
    visitor = Visitor.query.get(visitor_id)
    if not visitor:
        raise ValueError("Visitor not found")
    visitor.converted_to_member_id = member_id
    db.session.commit()
    return visitor


# ---------- 4. Self-serve RSVP ----------

def submit_rsvp(service_id, member_id, response="yes"):
    existing = EventRSVP.query.filter_by(service_id=service_id, member_id=member_id).first()
    if existing:
        existing.response = response
        db.session.commit()
        return existing
    rsvp = EventRSVP(service_id=service_id, member_id=member_id, response=response)
    db.session.add(rsvp)
    db.session.commit()
    return rsvp


def withdraw_rsvp(service_id, member_id):
    existing = EventRSVP.query.filter_by(service_id=service_id, member_id=member_id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()


def get_my_rsvps(member_id):
    return EventRSVP.query.filter_by(member_id=member_id).order_by(EventRSVP.created_at.desc()).all()


def get_rsvp_count(service_id):
    """Aggregate count for whoever is organizing — e.g. how many
    said yes — with no obligation to reveal who said nothing.
    Names of respondents are visible only via get_my_rsvps (self)
    or a facilitator viewing their own group's context, never as
    a bare 'everyone who didn't answer' list."""
    yes = EventRSVP.query.filter_by(service_id=service_id, response="yes").count()
    maybe = EventRSVP.query.filter_by(service_id=service_id, response="maybe").count()
    return {"service_id": service_id, "yes": yes, "maybe": maybe}
from datetime import datetime, date
from werkzeug.security import generate_password_hash, check_password_hash
from app.database import db

# Placeholder year used when a member's date_of_birth is entered as
# month+day only (dob_year_unknown=True on Member). 1900 rather than
# something like 1 or 9999 because SQLite's date type and Python's
# date.fromisoformat both need a real, in-range year, and no living
# member will ever genuinely have this birth year, so it can never
# collide with an actually-known birth year on file.
DOB_UNKNOWN_YEAR_SENTINEL = 1900


class Church(db.Model):
    __tablename__ = "churches"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    denomination = db.Column(db.String(100))
    address = db.Column(db.String(300))
    admin_user_id = db.Column(db.Integer, db.ForeignKey("members.id", use_alter=True))
    senior_leadership_id = db.Column(db.Integer, db.ForeignKey("members.id", use_alter=True), nullable=True)
    follow_up_threshold = db.Column(db.Integer, default=3)
    leader_escalation_days = db.Column(db.Integer, default=14)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    members = db.relationship(
        "Member", backref="church", lazy=True, foreign_keys="Member.church_id"
    )
    cells = db.relationship("CellGroup", backref="church", lazy=True)
    services = db.relationship("Service", backref="church", lazy=True)
    visitors = db.relationship("Visitor", backref="church", lazy=True)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "denomination": self.denomination,
            "follow_up_threshold": self.follow_up_threshold,
            "leader_escalation_days": self.leader_escalation_days,
        }

class Service(db.Model):
    __tablename__ = "services"

    id = db.Column(db.Integer, primary_key=True)
    church_id = db.Column(db.Integer, db.ForeignKey("churches.id"), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    date = db.Column(db.Date, nullable=False, default=date.today)
    type = db.Column(db.String(20), default="sunday")  # sunday | midweek | special
    venue = db.Column(db.String(200), nullable=True)
    start_time = db.Column(db.String(10), nullable=True)  # stored as "HH:MM AM/PM" string
    end_time = db.Column(db.String(10), nullable=True)
    description = db.Column(db.Text, nullable=True)
    visible_to = db.Column(db.String(30), default="everyone")  # everyone | leaders | admins
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Explicit cancel flag. Previously the frontend checked
    # `e.status === 'cancelled'`, but no status column existed
    # anywhere on this model -- that check silently always failed,
    # so "cancelled" events never actually hid their action buttons.
    is_cancelled = db.Column(db.Boolean, nullable=False, default=False)

    attendance_records = db.relationship("AttendanceRecord", backref="service", lazy=True)

    def to_dict(self):
        # attendance_taken is derived, not stored: an event has had
        # attendance taken if and only if at least one AttendanceRecord
        # exists for it. submit_attendance() creates one record per
        # active adult/teen/tracked-leader on every call it makes
        # (present or absent alike -- see _process_absence_check),
        # so "any records exist" is equivalent to "attendance was
        # submitted for this event," with no separate flag to drift
        # out of sync with reality.
        attendance_taken = len(self.attendance_records) > 0
        return {
            "id": self.id,
            "name": self.name,
            "date": self.date.isoformat(),
            "type": self.type,
            "venue": self.venue,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "description": self.description,
            "visible_to": self.visible_to,
            "is_cancelled": self.is_cancelled,
            "attendance_taken": attendance_taken,
        }

class FollowUpTeamMember(db.Model):
    """
    Real team concept, replacing the old single-admin fallback.
    Unassigned members' absences round-robin across everyone in
    this table for a church, rather than dumping every unassigned
    absence on one person.
    """
    __tablename__ = "follow_up_team_members"

    id = db.Column(db.Integer, primary_key=True)
    church_id = db.Column(db.Integer, db.ForeignKey("churches.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    member = db.relationship("Member", foreign_keys=[member_id])

    def to_dict(self):
        return {
            "id": self.id,
            "member_id": self.member_id,
            "member_name": self.member.full_name if self.member else None,
            "active": self.active,
        }


class NotificationLog(db.Model):
    """
    Push-notification audit trail. The actual SMS/email send is
    provider-agnostic (Mailjet, Twilio, whatever gets picked) --
    this table just records intent and outcome so the system is
    push-ready the moment a provider is wired in, without another
    schema change.
    """
    __tablename__ = "notification_logs"

    id = db.Column(db.Integer, primary_key=True)
    recipient_member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False)
    channel = db.Column(db.String(20), default="none")  # sms | email | none (logged-only, no provider yet)
    trigger = db.Column(db.String(50))  # new_assignment | escalation | leader_accountability
    related_assignment_id = db.Column(db.Integer, db.ForeignKey("follow_up_assignments.id"), nullable=True)
    status = db.Column(db.String(20), default="pending")  # pending | sent | failed | logged_only
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "recipient_member_id": self.recipient_member_id,
            "channel": self.channel,
            "trigger": self.trigger,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
        }


class Member(db.Model):
    __tablename__ = "members"

    id = db.Column(db.Integer, primary_key=True)
    church_id = db.Column(db.Integer, db.ForeignKey("churches.id"), nullable=False)

    full_name = db.Column(db.String(150), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="adult")  # adult | teen | leader | admin | child
    date_of_birth = db.Column(db.Date, nullable=True)
    # True when only month+day were given at registration, no year.
    # date_of_birth still holds a real date in that case (year forced
    # to DOB_UNKNOWN_YEAR_SENTINEL below) so get_upcoming_birthdays()
    # in attendance_logic.py keeps working completely unchanged -- it
    # already does dob.replace(year=today.year) for every member, so
    # the specific year stored has never mattered to that function,
    # only the month/day. This flag exists purely so the UI can be
    # honest that the year is a placeholder, not a real fact on file.
    dob_year_unknown = db.Column(db.Boolean, nullable=False, default=False)

    guardian_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=True)

    phone = db.Column(db.String(30))
    area = db.Column(db.String(150))  # neighborhood/zone, not full street address
    email = db.Column(db.String(150), unique=True, nullable=True)
    password_hash = db.Column(db.String(255), nullable=True)  # null for children

    joined_date = db.Column(db.Date, default=date.today)
    membership_status = db.Column(db.String(20), default="active")  # active | inactive

    cell_id = db.Column(db.Integer, db.ForeignKey("cell_groups.id"), nullable=True)
    consecutive_absences = db.Column(db.Integer, default=0)

    tracked_for_attendance = db.Column(db.Boolean, default=True)
    # Leaders/admins are tracked for their own attendance by default.
    # Set False for roles that don't have a Sunday-attendance
    # expectation at THIS church -- e.g. a senior_leadership_id
    # contact who oversees multiple congregations, or a secondary
    # admin account used only for system configuration. Regular
    # cell leaders should almost always stay True.

    created_by = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    guardian = db.relationship("Member", remote_side=[id], foreign_keys=[guardian_id])
    attendance_records = db.relationship(
        "AttendanceRecord", backref="member", lazy=True, foreign_keys="AttendanceRecord.member_id"
    )

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, raw_password)

    def to_dict(self, include_sensitive=False):
        data = {
            "id": self.id,
            "full_name": self.full_name,
            "role": self.role,
            "membership_status": self.membership_status,
            "cell_id": self.cell_id,
            "cell_name": self.cell.name if self.cell_id and self.cell else None,
            "consecutive_absences": self.consecutive_absences,
            "joined_date": self.joined_date.isoformat() if self.joined_date else None,
            # The Head Admin is the account first bootstrapped for this
            # church (Church.admin_user_id) -- protected from every
            # demote/suspend action, per renderRoleActions()'s own guard
            # comment in index.html. Derived here, not stored twice.
            "is_head_admin": bool(self.church and self.church.admin_user_id == self.id),
        }
        if include_sensitive:
            data.update({
                "phone": self.phone,
                "area": self.area,
                "email": self.email,
                "date_of_birth": self.date_of_birth.isoformat() if self.date_of_birth else None,
                "dob_year_unknown": self.dob_year_unknown,
                # month/day only, safe to show even when the year is a
                # placeholder -- lets the frontend render "Jul 14" for
                # a year-unknown birthday without ever needing to know
                # about DOB_UNKNOWN_YEAR_SENTINEL itself.
                "dob_month_day": self.date_of_birth.strftime("%m-%d") if self.date_of_birth else None,
                "guardian_id": self.guardian_id,
                "guardian_name": self.guardian.full_name if self.guardian else None,
            })
        return data


class CellGroup(db.Model):
    __tablename__ = "cell_groups"

    id = db.Column(db.Integer, primary_key=True)
    church_id = db.Column(db.Integer, db.ForeignKey("churches.id"), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    leader_id = db.Column(db.Integer, db.ForeignKey("members.id", use_alter=True), nullable=False)
    meeting_day = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    members = db.relationship(
        "Member", backref="cell", lazy=True, foreign_keys="Member.cell_id"
    )
    leader = db.relationship("Member", foreign_keys=[leader_id])

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "leader_id": self.leader_id,
            "leader_name": self.leader.full_name if self.leader else None,
            "meeting_day": self.meeting_day,
            "member_count": len(self.members),
        }

class AttendanceRecord(db.Model):
    """
    One row per member per service. Created fresh every time
    submit_attendance() runs — this is the live weekly snapshot,
    not a persistent list. A name reappearing after attending is
    just a new record with present=False, nothing more.
    """
    __tablename__ = "attendance_records"

    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False)
    service_id = db.Column(db.Integer, db.ForeignKey("services.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    present = db.Column(db.Boolean, nullable=False)

    follow_up_status = db.Column(db.String(30), default="not_applicable")
    # not_applicable | not_started | reached_ok | reached_concern |
    # no_answer | invalid_number
    follow_up_by = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=True)
    follow_up_note = db.Column(db.Text)
    follow_up_completed_at = db.Column(db.DateTime, nullable=True)
    escalated = db.Column(db.Boolean, default=False)

    assignments = db.relationship("FollowUpAssignment", backref="attendance_record", lazy=True)

    def to_dict(self):
        return {
            "id": self.id,
            "member_id": self.member_id,
            "member_name": self.member.full_name if self.member else None,
            "member_phone": self.member.phone if self.member else None,
            "member_area": self.member.area if self.member else None,
            "member_role": self.member.role if self.member else None,
            "cell_name": self.member.cell.name if self.member and self.member.cell_id and self.member.cell else "Unassigned",
            "date": self.date.isoformat(),
            "present": self.present,
            "follow_up_status": self.follow_up_status,
            "follow_up_note": self.follow_up_note,
            "escalated": self.escalated,
            "consecutive_absences": self.member.consecutive_absences if self.member else None,
        }


class Visitor(db.Model):
    __tablename__ = "visitors"

    id = db.Column(db.Integer, primary_key=True)
    church_id = db.Column(db.Integer, db.ForeignKey("churches.id"), nullable=False)
    full_name = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(30))
    email = db.Column(db.String(150), nullable=True)
    date_visited = db.Column(db.Date, default=date.today)
    invited_by = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=True)
    converted_to_member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Visitor's own opt-in/out, set from their card at intake --
    # engagement_logic.get_visitors_wanting_follow_up() is the ONLY
    # place this app auto-surfaces contact info, and only for rows
    # where the visitor themselves chose wants_follow_up=True.
    wants_follow_up = db.Column(db.Boolean, nullable=False, default=True)
    follow_up_status = db.Column(db.String(30), default="not_contacted")
    # not_contacted | reached_ok | reached_concern | no_answer | invalid_number
    follow_up_note = db.Column(db.Text, nullable=True)

    def to_dict(self):
        """
        Generic serializer — safe for any route, including a future
        general-purpose visitor list. Deliberately excludes email,
        wants_follow_up, and follow_up_status: those are visible only
        via to_follow_up_dict(), used exclusively by the dedicated
        follow-up-queue endpoint. See engagement_logic.py's module
        docstring — the follow-up queue must be the ONLY place this
        app auto-surfaces a visitor's contact/consent status.
        """
        return {
            "id": self.id,
            "full_name": self.full_name,
            "phone": self.phone,
            "date_visited": self.date_visited.isoformat() if self.date_visited else None,
            "converted": self.converted_to_member_id is not None,
        }

    def to_follow_up_dict(self):
        """
        Only call this from the dedicated follow-up-queue route (the
        one backed by engagement_logic.get_visitors_wanting_follow_up).
        Every visitor reachable through that function already has
        wants_follow_up=True by construction, so exposing these
        fields there does not create a new leak surface — it's the
        one place they're supposed to be visible.
        """
        base = self.to_dict()
        base.update({
            "email": self.email,
            "wants_follow_up": self.wants_follow_up,
            "follow_up_status": self.follow_up_status,
            "follow_up_note": self.follow_up_note,
        })
        return base


class EventRSVP(db.Model):
    """
    RSVP intent, separate from actual AttendanceRecord. A member who
    RSVPs yes and then doesn't show is a stronger drift signal than
    a plain unannounced absence -- the follow-up UI can surface
    "said yes, didn't come" differently from "just missed it."
    """
    __tablename__ = "event_rsvps"

    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(db.Integer, db.ForeignKey("services.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False)
    response = db.Column(db.String(20), default="yes")  # yes | no | maybe
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    service = db.relationship("Service", backref="rsvps")
    member = db.relationship("Member", foreign_keys=[member_id])

    def to_dict(self):
        return {
            "id": self.id,
            "service_id": self.service_id,
            "member_id": self.member_id,
            "member_name": self.member.full_name if self.member else None,
            "response": self.response,
        }


class FollowUpAssignment(db.Model):
    """
    One row per absence that needs a call. Assignments STACK —
    if a leader never completes last week's call and the member
    misses again, the old assignment stays open (status=pending)
    alongside the new one. This is intentional: it's how the app
    surfaces leaders who are dropping the ball, not just how it
    logs absences.
    """
    __tablename__ = "follow_up_assignments"

    id = db.Column(db.Integer, primary_key=True)
    attendance_record_id = db.Column(db.Integer, db.ForeignKey("attendance_records.id"), nullable=False)
    assigned_to = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False)
    status = db.Column(db.String(20), default="pending")  # pending | completed
    reason = db.Column(db.String(30), default="weekly_absence")  # weekly_absence | escalation
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    assignee = db.relationship("Member", foreign_keys=[assigned_to])

    def to_dict(self):
        record = self.attendance_record
        return {
            "id": self.id,
            "status": self.status,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
            "attendance_record": record.to_dict() if record else None,
        }

class CheckInGroup(db.Model):
    """
    A small group / cell-style group with opt-in, per-member
    attendance sharing. Distinct from CellGroup -- this is the
    consent-gated engagement layer, not the church's structural
    cell assignment.
    """
    __tablename__ = "check_in_groups"

    id = db.Column(db.Integer, primary_key=True)
    church_id = db.Column(db.Integer, db.ForeignKey("churches.id"), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    facilitator_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False)
    group_type = db.Column(db.String(30), default="small_group")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    facilitator = db.relationship("Member", foreign_keys=[facilitator_id])
    memberships = db.relationship("GroupMembership", backref="group", lazy=True)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "facilitator_id": self.facilitator_id,
            "group_type": self.group_type,
            "member_count": len(self.memberships),
        }


class GroupMembership(db.Model):
    """
    The consent record. share_attendance is the member's own choice
    at join time -- get_group_checkins_for_facilitator() in
    engagement_logic.py filters on this field directly, so it must
    default to something explicit rather than nullable ambiguity.
    """
    __tablename__ = "group_memberships"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("check_in_groups.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False)
    share_attendance = db.Column(db.Boolean, nullable=False, default=True)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)

    member = db.relationship("Member", foreign_keys=[member_id])

    __table_args__ = (
        db.UniqueConstraint("group_id", "member_id", name="uq_group_member"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "group_id": self.group_id,
            "member_id": self.member_id,
            "share_attendance": self.share_attendance,
        }


class CheckIn(db.Model):
    """
    A member's self-submitted check-in within a group. Visibility
    to the facilitator is NOT determined here -- it's computed at
    query time in get_group_checkins_for_facilitator() by joining
    against GroupMembership.share_attendance. This table itself
    carries no visibility flag; deleting a CheckIn is not how
    privacy is enforced, GroupMembership is.
    """
    __tablename__ = "check_ins"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("check_in_groups.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=False)
    date = db.Column(db.Date, nullable=False, default=date.today)
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    member = db.relationship("Member", foreign_keys=[member_id])

    def to_dict(self):
        return {
            "id": self.id,
            "group_id": self.group_id,
            "member_id": self.member_id,
            "date": self.date.isoformat(),
            "note": self.note,
        }


class AttendanceCount(db.Model):
    """
    Aggregate-only headcount. Deliberately has NO member_id column --
    test_scenario.py asserts this structurally, not just via query
    filtering. Do not add one later without re-reading why.
    """
    __tablename__ = "attendance_counts"

    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(db.Integer, db.ForeignKey("services.id"), nullable=False, unique=True)
    headcount = db.Column(db.Integer, nullable=False)
    recorded_by = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "service_id": self.service_id,
            "headcount": self.headcount,
        }


class AuditLog(db.Model):
    """
    Role-management trail: every promote/demote/suspend/restore.
    Not the same table as NotificationLog (that's push-notification
    delivery state) or NotificationLog's own actor-less design --
    this one exists specifically because index.html's audit-log page
    (loadAuditLog) already expects action/old_value/new_value/
    actor_name/target_name/created_at, and nothing produced that
    shape before this model existed. Names are denormalized (stored,
    not joined) so the log stays readable even if a member is later
    renamed or removed.
    """
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    church_id = db.Column(db.Integer, db.ForeignKey("churches.id"), nullable=False)
    action = db.Column(db.String(30), nullable=False)  # promote_admin | demote_admin | suspend_user | restore_user
    actor_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=True)
    actor_name = db.Column(db.String(150))
    target_id = db.Column(db.Integer, db.ForeignKey("members.id"), nullable=True)
    target_name = db.Column(db.String(150))
    old_value = db.Column(db.String(50))
    new_value = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "action": self.action,
            "actor_name": self.actor_name,
            "target_name": self.target_name,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
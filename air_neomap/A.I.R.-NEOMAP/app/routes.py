from datetime import date as _date, timedelta
from flask import Blueprint, request, jsonify
from app.database import db
from app.models import (
    Member, Church, CellGroup, Service, Visitor, FollowUpAssignment,
    FollowUpTeamMember, NotificationLog, AttendanceRecord, AuditLog,
    DOB_UNKNOWN_YEAR_SENTINEL,
)
from app.auth import (
    login_required, role_required, church_scoped,
    generate_token, ROLE_ADMIN, ROLE_LEADER, ROLE_ADULT, ROLE_CHILD,
)
from app.attendance_logic import (
    submit_attendance, complete_follow_up, get_pending_queue_for_user,
    get_admin_overview, get_unassigned_members, get_leader_accountability_overview,
    get_upcoming_birthdays, submit_rsvp, get_rsvp_no_shows, get_engagement_summary,
    get_cell_attendance_trend,
)
from app import engagement_logic

bp = Blueprint("neomap", __name__, url_prefix="/api")


def _parse_date(value):
    """SQLAlchemy's Date column needs an actual date object, not the
    'YYYY-MM-DD' string an HTML <input type="date"> or JSON body sends.
    Returns None for empty/missing input, and 400s via ValueError on
    anything malformed rather than letting a raw DB error leak out."""
    if not value:
        return None
    if isinstance(value, _date):
        return value
    return _date.fromisoformat(value)


def _parse_dob_fields(data):
    """
    Shared by register_member and update_member. Accepts EITHER a full
    date_of_birth ("YYYY-MM-DD") OR a dob_month_day ("MM-DD") when the
    year genuinely isn't known -- never both on the same request, since
    sending both is an ambiguous instruction about which one is true.

    Returns (date_of_birth_or_None, dob_year_unknown_bool). Raises
    ValueError with a message safe to return directly as the error
    body on anything malformed.

    dob_month_day is stored using DOB_UNKNOWN_YEAR_SENTINEL as the
    year -- get_upcoming_birthdays() in attendance_logic.py only ever
    reads month/day off this field (see that function's own
    dob.replace(year=today.year) calls), so which sentinel year is
    picked has zero effect on birthday matching, only on making sure
    the value is a real, storable date.
    """
    has_full = bool(data.get("date_of_birth"))
    has_month_day = bool(data.get("dob_month_day"))

    if has_full and has_month_day:
        raise ValueError("Provide either date_of_birth or dob_month_day, not both")

    if has_month_day:
        raw = data["dob_month_day"]
        try:
            month, day = raw.split("-")
            month, day = int(month), int(day)
        except (ValueError, AttributeError):
            raise ValueError("dob_month_day must be in MM-DD format")
        try:
            dob = _date(DOB_UNKNOWN_YEAR_SENTINEL, month, day)
        except ValueError:
            # Covers both out-of-range month/day AND Feb 29 -- 1900 is
            # not a leap year, so a Feb 29 birthday would otherwise
            # raise here even though it's a perfectly real birthday.
            # Roll it to Feb 28 rather than rejecting the whole
            # request, same accommodation get_upcoming_birthdays()
            # already makes for known leap-day birthdays.
            if month == 2 and day == 29:
                dob = _date(DOB_UNKNOWN_YEAR_SENTINEL, 2, 28)
            else:
                raise ValueError("dob_month_day must be a valid MM-DD date")
        return dob, True

    if has_full:
        try:
            dob = _parse_date(data["date_of_birth"])
        except ValueError:
            raise ValueError("date_of_birth must be in YYYY-MM-DD format")
        return dob, False

    return None, False


# ---------- AUTH ----------

@bp.route("/auth/login", methods=["POST"])
def login():
    data = request.json or {}
    email = data.get("email")
    password = data.get("password")

    member = Member.query.filter_by(email=email).first()
    if not member or not member.check_password(password):
        return jsonify({"error": "Invalid credentials"}), 401

    if member.role == "child":
        # children never authenticate directly, no exceptions
        return jsonify({"error": "This account cannot log in directly"}), 403

    token = generate_token(member.id, member.church_id, member.role)
    return jsonify({"token": token, "member": member.to_dict(include_sensitive=True)})


@bp.route("/auth/bootstrap", methods=["POST"])
def bootstrap_admin():
    """
    One-time setup: creates the very first admin account for a
    church. Only works when that church has zero members — the
    moment one exists, this route 403s forever. This solves the
    chicken-and-egg problem where register_member() requires an
    admin token, but no admin token can exist until someone is
    registered.
    """
    data = request.json or {}
    church_name = data.get("church_name")
    full_name = data.get("full_name")
    email = data.get("email")
    password = data.get("password")

    missing = [f for f in ["church_name", "full_name", "email", "password"] if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {missing}"}), 400

    existing_email = Member.query.filter_by(email=email).first()
    if existing_email:
        return jsonify({"error": "That email is already registered — sign in instead"}), 409

    church = Church.query.filter_by(name=church_name).first()
    if church:
        already_has_members = Member.query.filter_by(church_id=church.id).first()
        if already_has_members:
            return jsonify({"error": "This church already has an admin — ask them for access, sign in, or use a different church name"}), 403
    else:
        church = Church(name=church_name)
        db.session.add(church)
        db.session.commit()

    admin = Member(
        church_id=church.id,
        full_name=full_name,
        role=ROLE_ADMIN,
        email=email,
    )
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()

    church.admin_user_id = admin.id
    db.session.commit()

    token = generate_token(admin.id, admin.church_id, admin.role)
    return jsonify({"token": token, "member": admin.to_dict(include_sensitive=True)}), 201


@bp.route("/auth/me", methods=["GET"])
@login_required
def me():
    """
    Restores identity from a stored token on page reload — without
    this, a valid token with no cached member data (e.g. after a
    browser refresh) has no way to become a full session again.
    """
    member = Member.query.get_or_404(request.current_member["member_id"])
    return jsonify(member.to_dict(include_sensitive=True))


# ---------- MEMBER REGISTRATION (leader/admin only, server-enforced) ----------

@bp.route("/members/register", methods=["POST"])
@role_required(ROLE_ADMIN, ROLE_LEADER)
@church_scoped
def register_member():
    data = request.json or {}
    church_id = request.current_member["church_id"]

    required = ["full_name", "role"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {missing}"}), 400

    if data["role"] == "child" and not data.get("guardian_id"):
        return jsonify({"error": "Children must have a guardian_id — no independent accounts"}), 400

    email = data.get("email")
    if email:
        existing_email = Member.query.filter_by(email=email).first()
        if existing_email:
            return jsonify({"error": "That email is already registered to another member"}), 409

    try:
        dob, dob_year_unknown = _parse_dob_fields(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    member = Member(
        church_id=church_id,
        full_name=data["full_name"],
        role=data["role"],
        date_of_birth=dob,
        dob_year_unknown=dob_year_unknown,
        # Guardians only make sense for children -- silently accepting
        # one for any other role let a hidden, auto-selected dropdown
        # value leak into storage from the registration form. This is
        # the actual enforcement point; the frontend fix only stops
        # one form from sending it.
        guardian_id=data.get("guardian_id") if data["role"] == "child" else None,
        phone=data.get("phone"),
        area=data.get("area"),
        email=email,
        cell_id=data.get("cell_id"),
        created_by=request.current_member["member_id"],
    )

    if data.get("password") and data["role"] != "child":
        member.set_password(data["password"])

    db.session.add(member)
    db.session.commit()
    return jsonify(member.to_dict(include_sensitive=True)), 201


@bp.route("/members/check-duplicate", methods=["GET"])
@role_required(ROLE_ADMIN, ROLE_LEADER)
@church_scoped
def check_duplicate_member():
    """
    Backs the live "possible duplicate" warning on the register-member
    form. This is a WARNING endpoint, not a block: unlike the email
    uniqueness check in register_member (a hard 409, because the same
    email really shouldn't belong to two people), two different real
    people are allowed to share a name -- a father and namesake son,
    two unrelated "Blessing Okonkwo"s in the same congregation. The
    point here is catching the paper-attendance-book failure mode the
    person described: the SAME person written into the book more than
    once by accident, not preventing legitimate name collisions.

    Query params: full_name (required), phone, date_of_birth
    (YYYY-MM-DD), exclude_id (skip this member's own id -- needed so
    editing a member's own record doesn't flag itself as a duplicate
    of itself, once an edit flow exists).

    Returns a graded match list rather than a single yes/no, ordered
    strongest-first, so the frontend can word the warning by how
    confident it should be:
      - name_dob:   full name AND date_of_birth both match -- strong
      - name_phone: full name AND phone both match -- strong
      - name_only:  full name matches, nothing else does -- weak,
                    worth a nudge, not an alarm
    A single existing member can appear in more than one tier's
    reasons if e.g. both phone and DOB match -- the frontend collapses
    that into one card per person rather than showing them twice.
    """
    church_id = request.current_member["church_id"]
    full_name = (request.args.get("full_name") or "").strip()
    if not full_name:
        return jsonify({"matches": []})

    phone = (request.args.get("phone") or "").strip() or None
    try:
        dob = _parse_date(request.args.get("date_of_birth"))
    except ValueError:
        dob = None
    exclude_id = request.args.get("exclude_id", type=int)

    # Case/whitespace-insensitive on purpose -- "Chidinma Okafor" vs
    # "chidinma  okafor" is the exact kind of variation a name gets
    # typed differently across two separate attendance-book entries,
    # and should still be caught as the same underlying name.
    normalized_target = " ".join(full_name.lower().split())

    candidates = Member.query.filter_by(church_id=church_id).all()

    matches = []
    for m in candidates:
        if exclude_id and m.id == exclude_id:
            continue
        normalized_existing = " ".join((m.full_name or "").lower().split())
        if normalized_existing != normalized_target:
            continue

        # Built as an explicit priority list rather than merged/deduped
        # after the fact -- an earlier version of this tried to merge
        # conditionally-added reasons and silently left a stray
        # 'name_only' in the list whenever only the phone (not the dob)
        # also matched. Building the full set up front and choosing
        # from it removes that whole class of bug.
        strong_reasons = []
        if dob and m.date_of_birth and m.date_of_birth == dob:
            strong_reasons.append("name_dob")
        if phone and m.phone and m.phone.strip() == phone:
            strong_reasons.append("name_phone")
        reasons = strong_reasons if strong_reasons else ["name_only"]

        matches.append({
            "member_id": m.id,
            "full_name": m.full_name,
            "role": m.role,
            "phone": m.phone,
            "date_of_birth": m.date_of_birth.isoformat() if m.date_of_birth else None,
            "cell_name": m.cell.name if m.cell_id and m.cell else None,
            "joined_date": m.joined_date.isoformat() if m.joined_date else None,
            "match_reasons": reasons,
        })

    # Strongest matches first: name+dob and name+phone before bare name_only.
    def _strength(match):
        r = match["match_reasons"]
        if "name_dob" in r or "name_phone" in r:
            return 0
        return 1
    matches.sort(key=_strength)

    return jsonify({"matches": matches})


@bp.route("/members", methods=["GET"])
@login_required
@church_scoped
def list_members():
    """
    Defaults to active-only, matching every existing caller (the
    main Members page, attendance-taking roster, cell-leader
    picker, etc. all expect to never see a suspended person).

    ?include_inactive=true is the one exception: Role management
    (loadRoleMgmt) calls this same endpoint specifically to manage
    admins/leaders including suspended ones -- without this, a
    suspended leader becomes permanently unreachable for Restore,
    since suspending doesn't change their role and every other
    view of the roster filters them out.
    """
    church_id = request.current_member["church_id"]
    query = Member.query.filter_by(church_id=church_id)
    if request.args.get("include_inactive") != "true":
        query = query.filter_by(membership_status="active")
    members = query.all()
    return jsonify([m.to_dict() for m in members])


@bp.route("/members/<int:member_id>", methods=["GET"])
@login_required
@church_scoped
def get_member(member_id):
    """
    Single-member detail fetch. Added because no such route existed —
    only the register (POST) and list (GET, no id) routes were defined,
    so a frontend calling GET /api/members/<id> for a detail view had
    nothing to hit and 404'd.

    church_scoped alone doesn't cover this: it only compares church_id
    against the token when the client explicitly passes one in the
    URL/query/body, and a plain GET /members/3 doesn't pass one. The
    explicit check below is what actually enforces the boundary, same
    pattern as delete_event.
    """
    member = Member.query.get_or_404(member_id)
    if member.church_id != request.current_member["church_id"]:
        return jsonify({"error": "Forbidden — cross-church access denied"}), 403
    return jsonify(member.to_dict(include_sensitive=True))


@bp.route("/members/<int:member_id>", methods=["PATCH"])
@role_required(ROLE_ADMIN, ROLE_LEADER)
def update_member(member_id):
    """
    Corrects a member's own identity/contact details after
    registration -- name, phone, area, email, date of birth.

    Deliberately does NOT accept role, cell_id, or membership_status.
    Those are owned by promote-to-leader, promote/demote-admin, and
    suspend/restore respectively, each with its own guard (Head Admin
    checks, self-action checks, audit logging). Letting a generic
    "edit" endpoint touch those fields would be a quiet backdoor
    around every one of those guards -- so this route 400s if asked
    to, rather than silently ignoring or silently allowing it.

    Same explicit cross-church check as get_member: church_scoped
    alone doesn't cover a plain PATCH /members/<id> with no church_id
    in the URL/query/body.
    """
    data = request.json or {}
    church_id = request.current_member["church_id"]

    member = Member.query.get_or_404(member_id)
    if member.church_id != church_id:
        return jsonify({"error": "Forbidden — cross-church access denied"}), 403

    guarded_fields = {"role", "cell_id", "membership_status", "password", "church_id"}
    rejected = guarded_fields & data.keys()
    if rejected:
        return jsonify({
            "error": f"Cannot edit {', '.join(sorted(rejected))} here — "
                     f"use promote-to-leader, promote/demote-admin, or suspend/restore instead."
        }), 400

    if "full_name" in data:
        full_name = (data["full_name"] or "").strip()
        if not full_name:
            return jsonify({"error": "full_name cannot be empty"}), 400
        member.full_name = full_name

    if "phone" in data:
        member.phone = (data["phone"] or "").strip() or None

    if "area" in data:
        member.area = (data["area"] or "").strip() or None

    if "email" in data:
        email = (data["email"] or "").strip() or None
        if email:
            clash = Member.query.filter(
                Member.email == email, Member.id != member.id
            ).first()
            if clash:
                return jsonify({"error": "That email is already in use by another member"}), 400
        member.email = email

    # date_of_birth and dob_month_day are mutually exclusive alternates
    # for the same underlying field -- see _parse_dob_fields. Handled
    # separately from that helper's register-time logic here because
    # update_member needs to distinguish "field not sent" (leave
    # alone) from "field sent as empty string" (explicitly clear it),
    # which _parse_dob_fields' register-only bool(data.get(...)) check
    # can't tell apart on its own.
    if "date_of_birth" in data and "dob_month_day" in data and data["date_of_birth"] and data["dob_month_day"]:
        return jsonify({"error": "Provide either date_of_birth or dob_month_day, not both"}), 400

    if "date_of_birth" in data:
        raw_dob = data["date_of_birth"]
        if raw_dob:
            try:
                member.date_of_birth = _date.fromisoformat(raw_dob)
                member.dob_year_unknown = False
            except ValueError:
                return jsonify({"error": "date_of_birth must be YYYY-MM-DD"}), 400
        else:
            member.date_of_birth = None
            member.dob_year_unknown = False

    if "dob_month_day" in data:
        raw_month_day = data["dob_month_day"]
        if raw_month_day:
            try:
                dob, _unused = _parse_dob_fields({"dob_month_day": raw_month_day})
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            member.date_of_birth = dob
            member.dob_year_unknown = True
        else:
            member.date_of_birth = None
            member.dob_year_unknown = False

    db.session.commit()
    return jsonify(member.to_dict(include_sensitive=True))


# ---------- CELLS ----------

@bp.route("/cells", methods=["GET"])
@login_required
@church_scoped
def list_cells():
    church_id = request.current_member["church_id"]
    cells = CellGroup.query.filter_by(church_id=church_id).all()
    return jsonify([c.to_dict() for c in cells])


@bp.route("/cells", methods=["POST"])
@role_required(ROLE_ADMIN)
@church_scoped
def create_cell():
    data = request.json or {}
    church_id = request.current_member["church_id"]

    if not data.get("name") or not data.get("leader_id"):
        return jsonify({"error": "name and leader_id are required"}), 400

    cell = CellGroup(
        church_id=church_id,
        name=data["name"],
        leader_id=data["leader_id"],
        meeting_day=data.get("meeting_day"),
    )
    db.session.add(cell)
    db.session.commit()
    return jsonify(cell.to_dict()), 201


@bp.route("/cells/unassigned", methods=["GET"])
@role_required(ROLE_ADMIN, ROLE_LEADER)
@church_scoped
def unassigned_members():
    church_id = request.current_member["church_id"]
    members = get_unassigned_members(church_id)
    return jsonify([m.to_dict(include_sensitive=True) for m in members])


@bp.route("/members/<int:member_id>/cell", methods=["PATCH"])
@role_required(ROLE_ADMIN, ROLE_LEADER)
def reassign_member_cell(member_id):
    """
    Moves a member into a different cell, or out of any cell
    (cell_id: null). Standalone -- reachable from the member's own
    detail view, not tied to browsing a specific cell's page, since
    most of the time you're looking at the person who relocated or
    changed groups, not browsing cells first.

    Only touches cell_id. Does not touch role. A cell's leader can
    change without this route (see reassign_cell_leader below), and
    changing someone's cell here never promotes or demotes them --
    role changes stay owned by promote-to-leader / promote-demote-admin,
    each with its own guard and audit trail. Folding role changes into
    a cell-move would create a second, weaker path to the same effect.
    """
    data = request.json or {}
    church_id = request.current_member["church_id"]

    member = Member.query.get_or_404(member_id)
    if member.church_id != church_id:
        return jsonify({"error": "Forbidden — cross-church access denied"}), 403

    new_cell_id = data.get("cell_id")
    if new_cell_id is not None:
        cell = CellGroup.query.get_or_404(new_cell_id)
        if cell.church_id != church_id:
            return jsonify({"error": "Forbidden — cross-church access denied"}), 403
        member.cell_id = cell.id
    else:
        member.cell_id = None

    db.session.commit()
    return jsonify(member.to_dict(include_sensitive=True))


@bp.route("/cells/<int:cell_id>/leader", methods=["PATCH"])
@role_required(ROLE_ADMIN)
def reassign_cell_leader(cell_id):
    """
    Changes which member leads this cell. Admin-only, matching
    create_cell's own permission level -- leadership assignment is
    a step up from managing membership.

    Deliberately does not touch role. If the new leader isn't
    already role='leader' or 'admin', they can lead the cell without
    being promoted -- promotion is a separate, explicit action via
    promote-to-leader, with its own audit trail. Similarly, the
    outgoing leader is never auto-demoted here; if they're still
    leading other cells or the church wants them to keep the title,
    forcing a demotion would be presumptuous. This mirrors how
    reassign_member_cell above never touches role either -- cell
    membership/leadership and role are kept as separate concerns
    throughout this feature, on purpose.
    """
    data = request.json or {}
    church_id = request.current_member["church_id"]

    cell = CellGroup.query.get_or_404(cell_id)
    if cell.church_id != church_id:
        return jsonify({"error": "Forbidden — cross-church access denied"}), 403

    new_leader_id = data.get("leader_id")
    if not new_leader_id:
        return jsonify({"error": "leader_id is required"}), 400

    new_leader = Member.query.get_or_404(new_leader_id)
    if new_leader.church_id != church_id:
        return jsonify({"error": "Forbidden — cross-church access denied"}), 403
    if new_leader.role == "child":
        return jsonify({"error": "A child cannot lead a cell"}), 400

    cell.leader_id = new_leader.id
    db.session.commit()
    return jsonify(cell.to_dict())


@bp.route("/members/<int:member_id>/promote-to-leader", methods=["POST"])
@role_required(ROLE_ADMIN)
@church_scoped
def promote_to_leader(member_id):
    """
    Creates a CellGroup for member_id and sets their role to
    'leader' in one step. This is the route the "Promote to cell
    leader" modal has always called — it was missing entirely,
    which is why every submission there 405'd against Flask's
    static-file catch-all instead of reaching a real handler.

    role != 'child' mirrors the frontend's own eligibility filter
    (index.html: eligible = ALL_MEMBERS.filter(m => m.role !== 'child')),
    enforced here server-side rather than trusted from the client.
    """
    data = request.json or {}
    church_id = request.current_member["church_id"]

    member = Member.query.get_or_404(member_id)
    if member.church_id != church_id:
        return jsonify({"error": "Forbidden — cross-church access denied"}), 403

    if member.role == "child":
        return jsonify({"error": "Only adults, leaders, and admins can lead a cell"}), 400

    if not data.get("cell_name"):
        return jsonify({"error": "cell_name is required"}), 400

    cell = CellGroup(
        church_id=church_id,
        name=data["cell_name"],
        leader_id=member.id,
        meeting_day=data.get("meeting_day"),
    )
    db.session.add(cell)

    if member.role not in (ROLE_LEADER, ROLE_ADMIN):
        member.role = ROLE_LEADER

    db.session.commit()
    return jsonify({"cell": cell.to_dict(), "member": member.to_dict(include_sensitive=True)}), 201


def _is_head_admin(member):
    return bool(member.church and member.church.admin_user_id == member.id)


def _guard_role_action(church_id, actor_id, target_id, require_head_admin):
    """
    Shared preflight for all four role-management routes below.
    Returns (target_member, error_response_or_None). Every check
    here has a matching client-side condition in index.html's
    renderRoleActions() — this is what actually enforces it, since
    the frontend hiding a button is not the same as the server
    rejecting the request.
    """
    actor = Member.query.get(actor_id)
    target = Member.query.get_or_404(target_id)

    if target.church_id != church_id:
        return None, (jsonify({"error": "Forbidden — cross-church access denied"}), 403)

    if _is_head_admin(target):
        return None, (jsonify({"error": "The Head Admin's role and status can't be changed"}), 403)

    if target.id == actor_id:
        return None, (jsonify({"error": "You can't change your own role or status"}), 400)

    if require_head_admin and not (actor and _is_head_admin(actor)):
        return None, (jsonify({"error": "Only the Head Admin can do that"}), 403)

    return target, None


def _log_role_action(church_id, actor, target, action, old_value, new_value):
    db.session.add(AuditLog(
        church_id=church_id,
        action=action,
        actor_id=actor.id if actor else None,
        actor_name=actor.full_name if actor else "Unknown",
        target_id=target.id,
        target_name=target.full_name,
        old_value=old_value,
        new_value=new_value,
    ))


@bp.route("/admin/promote-to-admin", methods=["POST"])
@role_required(ROLE_ADMIN)
@church_scoped
def promote_to_admin():
    """
    Head-Admin-only, per renderRoleActions(): the "Promote to admin"
    button only ever renders when ME.is_head_admin is true. That's a
    UI convenience, not enforcement — require_head_admin=True below
    is what actually stops any other admin from calling this route
    directly and self-promoting the whole staff.
    """
    data = request.json or {}
    church_id = request.current_member["church_id"]
    actor_id = request.current_member["member_id"]
    member_id = data.get("member_id")

    if not member_id:
        return jsonify({"error": "member_id is required"}), 400

    target, err = _guard_role_action(church_id, actor_id, member_id, require_head_admin=True)
    if err:
        return err

    if target.role == "child":
        return jsonify({"error": "Children can't be promoted to admin"}), 400

    old_role = target.role
    target.role = ROLE_ADMIN
    actor = Member.query.get(actor_id)
    _log_role_action(church_id, actor, target, "promote_admin", old_role, ROLE_ADMIN)
    db.session.commit()
    return jsonify(target.to_dict(include_sensitive=True))


@bp.route("/admin/demote-admin", methods=["POST"])
@role_required(ROLE_ADMIN)
@church_scoped
def demote_admin():
    """
    Head-Admin-only, same reasoning as promote above. new_role is
    accepted from the client (index.html always sends 'adult') but
    constrained here to a safe landing role rather than trusted
    verbatim — an admin should never be "demoted" straight to a
    role the demote flow wasn't designed to validate against.
    """
    data = request.json or {}
    church_id = request.current_member["church_id"]
    actor_id = request.current_member["member_id"]
    member_id = data.get("member_id")
    new_role = data.get("new_role", ROLE_ADULT)

    if not member_id:
        return jsonify({"error": "member_id is required"}), 400
    if new_role not in (ROLE_ADULT, ROLE_LEADER):
        return jsonify({"error": "new_role must be 'adult' or 'leader'"}), 400

    target, err = _guard_role_action(church_id, actor_id, member_id, require_head_admin=True)
    if err:
        return err

    if target.role != ROLE_ADMIN:
        return jsonify({"error": "Member is not currently an admin"}), 400

    actor = Member.query.get(actor_id)
    _log_role_action(church_id, actor, target, "demote_admin", ROLE_ADMIN, new_role)
    target.role = new_role
    db.session.commit()
    return jsonify(target.to_dict(include_sensitive=True))


@bp.route("/admin/suspend-user", methods=["POST"])
@role_required(ROLE_ADMIN)
@church_scoped
def suspend_user():
    """
    Any admin, not just Head Admin — matches renderRoleActions()'s
    `if(ME.role === 'admin')` gate, which is broader than the
    promote/demote buttons above.
    """
    data = request.json or {}
    church_id = request.current_member["church_id"]
    actor_id = request.current_member["member_id"]
    member_id = data.get("member_id")

    if not member_id:
        return jsonify({"error": "member_id is required"}), 400

    target, err = _guard_role_action(church_id, actor_id, member_id, require_head_admin=False)
    if err:
        return err

    if target.membership_status != "active":
        return jsonify({"error": "Member is already suspended"}), 400

    actor = Member.query.get(actor_id)
    _log_role_action(church_id, actor, target, "suspend_user", "active", "inactive")
    target.membership_status = "inactive"
    db.session.commit()
    return jsonify(target.to_dict(include_sensitive=True))


@bp.route("/admin/restore-user", methods=["POST"])
@role_required(ROLE_ADMIN)
@church_scoped
def restore_user():
    data = request.json or {}
    church_id = request.current_member["church_id"]
    actor_id = request.current_member["member_id"]
    member_id = data.get("member_id")

    if not member_id:
        return jsonify({"error": "member_id is required"}), 400

    target, err = _guard_role_action(church_id, actor_id, member_id, require_head_admin=False)
    if err:
        return err

    if target.membership_status == "active":
        return jsonify({"error": "Member is already active"}), 400

    actor = Member.query.get(actor_id)
    _log_role_action(church_id, actor, target, "restore_user", "inactive", "active")
    target.membership_status = "active"
    db.session.commit()
    return jsonify(target.to_dict(include_sensitive=True))


@bp.route("/admin/reclassify-role", methods=["POST"])
@role_required(ROLE_ADMIN, ROLE_LEADER)
@church_scoped
def reclassify_role():
    """
    Corrects an adult/teen/child classification made at registration
    time under uncertainty (see index.html's register-member form:
    "Not sure? Register as Adult — you can correct this here later").
    Deliberately NOT the same route as promote-to-leader or
    promote/demote-admin: this is a lower-stakes, reversible age-
    bracket correction, not a permission grant, so any admin or
    leader can do it -- require_head_admin=False, matching
    suspend/restore rather than the admin-promotion routes above.

    new_role is restricted to {adult, teen, child} on purpose. This
    route must never become a side-door to 'leader' or 'admin' --
    those still only happen through promote_to_leader and
    promote_to_admin, each with its own head-admin/audit handling
    this route doesn't replicate.

    Reclassifying INTO 'child' requires a guardian_id, mirroring the
    same rule register_member enforces at creation time (see line
    ~132) -- otherwise an existing member could end up marked as a
    child with nobody responsible for them on file, a gap
    registration itself was specifically written to prevent.
    Reclassifying OUT of 'child' does not clear guardian_id; a stale
    guardian reference on an adult's record is harmless, unlike a
    child with none.
    """
    data = request.json or {}
    church_id = request.current_member["church_id"]
    actor_id = request.current_member["member_id"]
    member_id = data.get("member_id")
    new_role = data.get("new_role")

    if not member_id:
        return jsonify({"error": "member_id is required"}), 400

    ALLOWED_RECLASSIFY_ROLES = (ROLE_ADULT, "teen", ROLE_CHILD)
    if new_role not in ALLOWED_RECLASSIFY_ROLES:
        return jsonify({"error": f"new_role must be one of {', '.join(ALLOWED_RECLASSIFY_ROLES)}"}), 400

    target, err = _guard_role_action(church_id, actor_id, member_id, require_head_admin=False)
    if err:
        return err

    if target.role not in ALLOWED_RECLASSIFY_ROLES:
        return jsonify({"error": f"{target.full_name} is a {target.role} — use the leader/admin management actions instead"}), 400

    if target.role == new_role:
        return jsonify({"error": f"{target.full_name} is already classified as {new_role}"}), 400

    if new_role == ROLE_CHILD and not (target.guardian_id or data.get("guardian_id")):
        return jsonify({"error": "Reclassifying to child requires a guardian_id"}), 400

    old_role = target.role
    actor = Member.query.get(actor_id)
    _log_role_action(church_id, actor, target, "reclassify_role", old_role, new_role)

    target.role = new_role
    if new_role == ROLE_CHILD and data.get("guardian_id"):
        target.guardian_id = data["guardian_id"]

    db.session.commit()
    return jsonify(target.to_dict(include_sensitive=True))


@bp.route("/admin/audit-log", methods=["GET"])
@role_required(ROLE_ADMIN)
@church_scoped
def get_audit_log():
    church_id = request.current_member["church_id"]
    logs = AuditLog.query.filter_by(church_id=church_id).order_by(AuditLog.created_at.desc()).all()
    return jsonify([l.to_dict() for l in logs])


# ---------- SERVICES ----------

@bp.route("/services", methods=["POST"])
@role_required(ROLE_ADMIN, ROLE_LEADER)
@church_scoped
def create_service():
    data = request.json or {}
    church_id = request.current_member["church_id"]

    if not data.get("name") or not data.get("date"):
        return jsonify({"error": "name and date are required"}), 400

    try:
        service_date = _parse_date(data["date"])
    except ValueError:
        return jsonify({"error": "date must be in YYYY-MM-DD format"}), 400

    service = Service(
        church_id=church_id,
        name=data["name"],
        date=service_date,
        type=data.get("type", "sunday"),
    )
    db.session.add(service)
    db.session.commit()
    return jsonify(service.to_dict()), 201


# ---------- ATTENDANCE — the core loop ----------

@bp.route("/attendance/submit", methods=["POST"])
@role_required(ROLE_ADMIN, ROLE_LEADER)
@church_scoped
def submit_attendance_route():
    data = request.json or {}
    church_id = request.current_member["church_id"]
    service_id = data.get("service_id")
    present_member_ids = data.get("present_member_ids", [])
    new_visitors = data.get("new_visitors", [])  # [{full_name, phone}, ...]

    if not service_id:
        return jsonify({"error": "service_id is required"}), 400

    # Server-side guard, not just a hidden button: without this, a
    # second POST to this same endpoint for a service_id that already
    # has attendance -- whether from a double-click, a stale tab left
    # open, or someone hitting the API directly -- would create a
    # second full batch of AttendanceRecord rows and re-run the entire
    # absence/escalation diff a second time, doubling consecutive-
    # absence counts and follow-up assignments for anyone marked
    # absent on both submissions.
    event = Service.query.get_or_404(service_id)
    if event.church_id != church_id:
        return jsonify({"error": "Forbidden — cross-church access denied"}), 403
    if event.is_cancelled:
        return jsonify({"error": "This event was cancelled — attendance can't be recorded for it"}), 409
    already_taken = AttendanceRecord.query.filter_by(service_id=service_id).first() is not None
    if already_taken:
        return jsonify({"error": "Attendance has already been submitted for this event"}), 409

    # quick-add visitors before the diff runs — they aren't part of
    # the roster yet, so they never appear on an absence list
    for v in new_visitors:
        if v.get("full_name"):
            db.session.add(Visitor(
                church_id=church_id,
                full_name=v["full_name"],
                phone=v.get("phone"),
                invited_by=request.current_member["member_id"],
            ))
    db.session.commit()

    records = submit_attendance(
        church_id=church_id,
        service_id=service_id,
        present_member_ids=present_member_ids,
        submitted_by_id=request.current_member["member_id"],
    )

    return jsonify({
        "absent_count": len(records),
        "records": [r.to_dict() for r in records],
    }), 201


# ---------- FOLLOW-UP QUEUE ----------

@bp.route("/follow-up/queue", methods=["GET"])
@login_required
def my_follow_up_queue():
    """
    Returns ONLY the current user's own pending assignments —
    scoped server-side by their member_id from the token, never
    by a client-supplied filter on WHOSE queue this is. A leader
    cannot pull another leader's queue by changing a query param.

    The optional ?role= param narrows this same, single-user queue
    by the absent member's role (e.g. "adult" or "teen") — it can
    only ever shrink what's already the caller's own queue, not
    redirect to anyone else's.
    """
    user_id = request.current_member["member_id"]
    role_filter = request.args.get("role", default=None, type=str)
    assignments = get_pending_queue_for_user(user_id, role_filter=role_filter)
    return jsonify([a.to_dict() for a in assignments])


@bp.route("/visitors", methods=["GET"])
@role_required(ROLE_ADMIN, ROLE_LEADER)
@church_scoped
def list_visitors():
    """
    General-purpose visitor roster -- everyone who's visited, not
    just the follow-up-wanting subset. Uses to_dict(), not
    to_follow_up_dict(): per Visitor.to_dict()'s own docstring,
    email/wants_follow_up/follow_up_status must stay visible only
    through the dedicated /follow-up/visitors endpoint above, so
    this route can never become a second way to see who opted out.
    """
    church_id = request.current_member["church_id"]
    visitors = Visitor.query.filter_by(church_id=church_id).order_by(Visitor.date_visited.desc()).all()
    return jsonify([v.to_dict() for v in visitors])


@bp.route("/visitors", methods=["POST"])
@role_required(ROLE_ADMIN, ROLE_LEADER)
@church_scoped
def create_visitor():
    """
    Standalone visitor intake -- separate from the new_visitors
    quick-add folded into /attendance/submit. wants_follow_up is
    passed through exactly as submitted, defaulting True only
    because that's the visitor card's own default checkbox state,
    per add_visitor()'s docstring -- never silently forced True
    server-side regardless of what the form actually sends.
    """
    data = request.json or {}
    church_id = request.current_member["church_id"]

    if not data.get("full_name"):
        return jsonify({"error": "full_name is required"}), 400

    visitor = engagement_logic.add_visitor(
        church_id=church_id,
        full_name=data["full_name"],
        phone=data.get("phone"),
        email=data.get("email"),
        wants_follow_up=data.get("wants_follow_up", True),
    )
    return jsonify(visitor.to_dict()), 201


@bp.route("/visitors/<int:visitor_id>/convert", methods=["POST"])
@role_required(ROLE_ADMIN, ROLE_LEADER)
def convert_visitor(visitor_id):
    """
    Points a visitor record at an existing Member. Per
    convert_visitor_to_member()'s docstring, this is purely the
    church's own record-keeping pointer -- it does not retroactively
    apply any tracking to the Member, who starts with a blank slate
    like anyone else. member_id must already exist and belong to
    this church; this route does not create the Member itself, so
    the caller registers the member first (e.g. via /members/register)
    and links the two here.

    engagement_logic.convert_visitor_to_member() has no built-in
    church-ownership check, same gap as mark_visitor_followed_up --
    the explicit checks below are what actually stop a leader at
    Church A from linking Church B's visitor or member by guessing ids.
    """
    data = request.json or {}
    member_id = data.get("member_id")
    church_id = request.current_member["church_id"]

    if not member_id:
        return jsonify({"error": "member_id is required"}), 400

    visitor = Visitor.query.get_or_404(visitor_id)
    if visitor.church_id != church_id:
        return jsonify({"error": "Forbidden — cross-church access denied"}), 403

    member = Member.query.get_or_404(member_id)
    if member.church_id != church_id:
        return jsonify({"error": "Forbidden — cross-church access denied"}), 403

    try:
        updated = engagement_logic.convert_visitor_to_member(visitor_id, member_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify(updated.to_dict())


@bp.route("/follow-up/visitors", methods=["GET"])
@role_required(ROLE_ADMIN, ROLE_LEADER)
@church_scoped
def visitor_follow_up_queue():
    """
    Visitors who checked "wants follow-up" and haven't been reached
    yet — see engagement_logic.get_visitors_wanting_follow_up() and
    its own docstring: "the ONLY list this app ever auto-surfaces
    contact info on... every row exists because the person asked
    to be on it."

    Unlike the member queue above, this list has no per-person
    owner — Visitor carries no assigned_to — so every admin and
    leader at this church sees the same shared list, not a filtered
    slice of it. That's a deliberate difference, not an oversight:
    a visitor's follow-up isn't routed to one specific person the
    way an absent member's is.

    Uses to_follow_up_dict(), not the generic to_dict(), because
    this is exactly the one dedicated endpoint that method exists
    for — see its docstring in models.py.
    """
    church_id = request.current_member["church_id"]
    visitors = engagement_logic.get_visitors_wanting_follow_up(church_id)
    return jsonify([v.to_follow_up_dict() for v in visitors])


@bp.route("/follow-up/visitors/<int:visitor_id>/complete", methods=["POST"])
@role_required(ROLE_ADMIN, ROLE_LEADER)
def complete_visitor_follow_up(visitor_id):
    """
    Marks a visitor as contacted. engagement_logic.mark_visitor_followed_up()
    has no church-ownership check built in -- same gap pattern as
    get_member/delete_event/rsvp_no_shows elsewhere in this file --
    so the explicit check below is what actually prevents a leader
    at Church A from marking Church B's visitor, by guessing an id.
    """
    data = request.json or {}
    status = data.get("status", "contacted")
    note = data.get("note")

    visitor = Visitor.query.get_or_404(visitor_id)
    if visitor.church_id != request.current_member["church_id"]:
        return jsonify({"error": "Forbidden — cross-church access denied"}), 403

    try:
        updated = engagement_logic.mark_visitor_followed_up(visitor_id, status, note)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify(updated.to_follow_up_dict())


@bp.route("/follow-up/<int:assignment_id>/complete", methods=["POST"])
@login_required
def complete_follow_up_route(assignment_id):
    data = request.json or {}
    outcome = data.get("outcome_status")
    note = data.get("note", "")

    assignment = FollowUpAssignment.query.get_or_404(assignment_id)

    # a user can only complete their OWN assignment, admin excepted
    caller_id = request.current_member["member_id"]
    caller_role = request.current_member["role"]
    if assignment.assigned_to != caller_id and caller_role != ROLE_ADMIN:
        return jsonify({"error": "Forbidden — not your assignment"}), 403

    try:
        record = complete_follow_up(
            assignment_id=assignment_id,
            outcome_status=outcome,
            note=note,
            completed_by_id=caller_id,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify(record.to_dict())


# ---------- ADMIN DASHBOARD ----------
# ---------- ADMIN STATISTICS ----------

@bp.route("/admin/statistics", methods=["GET"])
@role_required(ROLE_ADMIN)
@church_scoped
def admin_statistics():
    from sqlalchemy import func

    church_id = request.current_member["church_id"]

    total_members = Member.query.filter_by(church_id=church_id, membership_status="active").count()
    total_cells = CellGroup.query.filter_by(church_id=church_id).count()
    total_visitors = Visitor.query.filter_by(church_id=church_id).count()
    converted_visitors = Visitor.query.filter_by(
        church_id=church_id
    ).filter(Visitor.converted_to_member_id.isnot(None)).count()

    by_role = {
        role: Member.query.filter_by(
            church_id=church_id, membership_status="active", role=role
        ).count()
        for role in ("adult", "teen", "child", "leader", "admin")
    }

    open_follow_ups = (
        FollowUpAssignment.query
        .join(Member, FollowUpAssignment.assigned_to == Member.id)
        .filter(Member.church_id == church_id, FollowUpAssignment.status == "pending")
        .count()
    )

    # range query param: "all" for full history, a bare integer for
    # "last N services", or "6m"/"1y"/"2y" for a real calendar cutoff.
    # Every AttendanceRecord created by submit_attendance() is
    # permanent -- nothing is ever deleted -- so any range value here
    # is purely how far back this one chart looks, not what data
    # still exists in the database.
    range_param = request.args.get("range", default="12", type=str)
    services_query = (
        Service.query
        .filter_by(church_id=church_id)
        .order_by(Service.date.desc())
    )

    CALENDAR_CUTOFFS = {"6m": 182, "1y": 365, "2y": 730}
    if range_param in CALENDAR_CUTOFFS:
        cutoff_date = _date.today() - timedelta(days=CALENDAR_CUTOFFS[range_param])
        services_query = services_query.filter(Service.date >= cutoff_date)
    elif range_param != "all":
        try:
            limit_n = int(range_param)
        except ValueError:
            limit_n = 12
        services_query = services_query.limit(limit_n)
    recent_services = services_query.all()

    attendance_trend = []
    for svc in reversed(recent_services):
        present = (
            db.session.query(func.count(AttendanceRecord.id))
            .join(Member, AttendanceRecord.member_id == Member.id)
            .filter(
                AttendanceRecord.service_id == svc.id,
                Member.role.in_(["adult", "teen"]),
                AttendanceRecord.present == True,  # noqa: E712
            )
            .scalar()
        ) or 0
        absent = (
            db.session.query(func.count(AttendanceRecord.id))
            .join(Member, AttendanceRecord.member_id == Member.id)
            .filter(
                AttendanceRecord.service_id == svc.id,
                Member.role.in_(["adult", "teen"]),
                AttendanceRecord.present == False,  # noqa: E712
            )
            .scalar()
        ) or 0
        attendance_trend.append({
            "service_id": svc.id,
            "date": svc.date.isoformat(),
            "service_name": svc.name,
            "present": present,
            "absent": absent,
        })

    latest_service_date = recent_services[0].date.isoformat() if recent_services else None
    latest_present = attendance_trend[-1]["present"] if attendance_trend else 0

    return jsonify({
        "total_members": total_members,
        "total_cells": total_cells,
        "total_visitors": total_visitors,
        "converted_visitors": converted_visitors,
        "by_role": by_role,
        "open_follow_ups": open_follow_ups,
        "latest_service_date": latest_service_date,
        "latest_attendance": {"present": latest_present},
        "attendance_trend": attendance_trend,
    })


@bp.route("/admin/statistics/service/<int:service_id>/attendees", methods=["GET"])
@role_required(ROLE_ADMIN)
@church_scoped
def statistics_service_attendees(service_id):
    """
    Backs the click-through on the Statistics attendance-trend chart:
    the chart itself only ever shows a present/absent count per point,
    by design (see engagement_logic.py's module docstring — dashboards
    stay aggregate-only). This route is the one deliberate exception,
    scoped to admins only, matching the church-wide visibility an
    admin already has via the attendance-taking screen and the
    per-member engagement endpoint. It returns full present AND
    absent lists, split, for one specific service.

    service_id isn't a body/query param church_scoped can compare
    against a token, so — same pattern as rsvp_no_shows and
    delete_event above — the cross-church ownership check has to be
    explicit here rather than relying on the decorator alone.
    """
    service = Service.query.get_or_404(service_id)
    if service.church_id != request.current_member["church_id"]:
        return jsonify({"error": "Forbidden — cross-church access denied"}), 403

    records = (
        AttendanceRecord.query
        .filter_by(service_id=service_id)
        .join(Member, AttendanceRecord.member_id == Member.id)
        .filter(Member.role.in_(["adult", "teen"]))
        .all()
    )
    present = [r.to_dict() for r in records if r.present]
    absent = [r.to_dict() for r in records if not r.present]
    return jsonify({
        "service_id": service_id,
        "service_name": service.name,
        "date": service.date.isoformat(),
        "present": present,
        "absent": absent,
    })


@bp.route("/admin/follow-up/overview", methods=["GET"])
@role_required(ROLE_ADMIN)
@church_scoped
def admin_overview():
    church_id = request.current_member["church_id"]
    return jsonify(get_admin_overview(church_id))


@bp.route("/admin/leaders/overview", methods=["GET"])
@role_required(ROLE_ADMIN)
@church_scoped
def leader_accountability_overview():
    """
    Separate section, distinct from member follow-up: which leaders
    are showing up consistently and which aren't. Flags at 2
    consecutive absences (earlier than the member threshold) since
    a leader's own inconsistency undermines the whole follow-up chain.
    """
    church_id = request.current_member["church_id"]
    return jsonify(get_leader_accountability_overview(church_id))


@bp.route("/admin/cells/attendance-trend", methods=["GET"])
@role_required(ROLE_ADMIN)
@church_scoped
def cell_attendance_trend():
    """
    Present/total per cell, per service -- shows which cells are
    healthy versus quietly shrinking, distinct from the church-wide
    aggregate on the Statistics page. See get_cell_attendance_trend's
    own docstring for scope (adult+teen members only, cell leaders
    excluded from their own cell's percentage).
    """
    church_id = request.current_member["church_id"]
    limit = request.args.get("limit", default=12, type=int)
    return jsonify(get_cell_attendance_trend(church_id, limit=limit))


# ---------- FEATURE 1: BIRTHDAYS ----------

@bp.route("/members/birthdays", methods=["GET"])
@role_required(ROLE_ADMIN, ROLE_LEADER)
@church_scoped
def upcoming_birthdays():
    """
    Warm, non-corrective contact list for leaders -- includes phone
    numbers (see get_upcoming_birthdays), so this stays admin/leader
    only. A plain member should not be able to see every other
    adult's phone number just by opening the Birthdays page. The
    frontend's unconditional loadBirthdays() call on every login is
    the actual bug -- see enterApp().
    """
    church_id = request.current_member["church_id"]
    days_ahead = request.args.get("days_ahead", default=7, type=int)
    return jsonify(get_upcoming_birthdays(church_id, days_ahead))


# ---------- FEATURE 4: EVENT RSVP ----------

@bp.route("/services/<int:service_id>/rsvp", methods=["POST"])
@login_required
def rsvp_route(service_id):
    data = request.json or {}
    response = data.get("response", "yes")
    if response not in ("yes", "no", "maybe"):
        return jsonify({"error": "response must be yes, no, or maybe"}), 400

    rsvp = submit_rsvp(
        service_id=service_id,
        member_id=request.current_member["member_id"],
        response=response,
    )
    return jsonify(rsvp.to_dict()), 201
# ---------- EVENTS (frontend name for services) ----------

@bp.route("/events", methods=["GET"])
@login_required
@church_scoped
def list_events():
    church_id = request.current_member["church_id"]
    events = Service.query.filter_by(church_id=church_id).order_by(Service.date.asc()).all()
    return jsonify([e.to_dict() for e in events])


@bp.route("/events", methods=["POST"])
@role_required(ROLE_ADMIN, ROLE_LEADER)
@church_scoped
def create_event():
    data = request.json or {}
    church_id = request.current_member["church_id"]

    if not data.get("name") or not data.get("date"):
        return jsonify({"error": "name and date are required"}), 400

    try:
        event_date = _parse_date(data["date"])
    except ValueError:
        return jsonify({"error": "date must be in YYYY-MM-DD format"}), 400

    event = Service(
        church_id=church_id,
        name=data["name"],
        date=event_date,
        type=data.get("category", "event"),
        venue=data.get("venue"),
        start_time=data.get("start_time"),
        end_time=data.get("end_time"),
        description=data.get("description"),
        visible_to=data.get("visible_to", "everyone"),
    )
    db.session.add(event)
    db.session.commit()
    return jsonify(event.to_dict()), 201


@bp.route("/events/<int:event_id>", methods=["DELETE"])
@role_required(ROLE_ADMIN, ROLE_LEADER)
@church_scoped
def delete_event(event_id):
    """
    Soft-cancel, not a hard delete. A hard DELETE here would silently
    destroy any AttendanceRecord rows already tied to event_id (they
    have no ON DELETE CASCADE configured, so orphaning them on delete
    was the previous, unintentional behavior) -- if attendance was
    ever taken for this event and it's cancelled afterward, that
    history has to survive. Flipping is_cancelled=True keeps the row,
    its attendance history, and its RSVP history intact, and the
    frontend hides the action buttons on any event where this is True.
    """
    event = Service.query.get_or_404(event_id)
    if event.church_id != request.current_member["church_id"]:
        return jsonify({"error": "Forbidden"}), 403
    event.is_cancelled = True
    db.session.commit()
    return jsonify(event.to_dict())

@bp.route("/services/<int:service_id>/rsvp-no-shows", methods=["GET"])
@role_required(ROLE_ADMIN, ROLE_LEADER)
def rsvp_no_shows(service_id):
    """
    Members who RSVP'd yes but weren't marked present -- stronger drift
    signal than a plain absence.

    Church-scoping fix: this route previously had no @church_scoped and
    no manual ownership check. church_scoped wouldn't have helped anyway
    -- it compares a church_id param against the token, and this route
    takes service_id, not church_id, so the decorator has nothing to
    compare. The explicit check below is what actually closes the gap:
    without it, a leader at Church A could pass any service_id and pull
    Church B's RSVP no-show data. Same pattern as delete_event.
    """
    service = Service.query.get_or_404(service_id)
    if service.church_id != request.current_member["church_id"]:
        return jsonify({"error": "Forbidden — cross-church access denied"}), 403
    return jsonify(get_rsvp_no_shows(service_id))


# ---------- FEATURE 5: ENGAGEMENT SUMMARY ----------

@bp.route("/members/<int:member_id>/engagement", methods=["GET"])
@role_required(ROLE_ADMIN, ROLE_LEADER)
@church_scoped
def member_engagement(member_id):
    """Multi-service attendance rate, not just Sunday -- prevents false escalation on differently-engaged members."""
    lookback = request.args.get("lookback", default=8, type=int)
    return jsonify(get_engagement_summary(member_id, lookback))


@bp.route("/admin/follow-up/all", methods=["GET"])
@role_required(ROLE_ADMIN)
@church_scoped
def admin_all_pending():
    """Every open assignment church-wide, including stale/stacked ones, for the admin to see who's falling behind."""
    church_id = request.current_member["church_id"]
    assignments = (
        FollowUpAssignment.query
        .join(Member, FollowUpAssignment.assigned_to == Member.id)
        .filter(Member.church_id == church_id, FollowUpAssignment.status == "pending")
        .order_by(FollowUpAssignment.created_at.asc())
        .all()
    )
    return jsonify([a.to_dict() for a in assignments])


# ---------- GENERAL FOLLOW-UP TEAM ----------

@bp.route("/admin/follow-up-team", methods=["GET"])
@role_required(ROLE_ADMIN)
@church_scoped
def list_follow_up_team():
    church_id = request.current_member["church_id"]
    team = FollowUpTeamMember.query.filter_by(church_id=church_id).all()
    return jsonify([t.to_dict() for t in team])


@bp.route("/admin/follow-up-team", methods=["POST"])
@role_required(ROLE_ADMIN)
@church_scoped
def add_follow_up_team_member():
    """
    Adds a member to the church's general follow-up team. Unassigned
    absences round-robin across everyone active on this list, instead
    of all landing on a single admin.
    """
    data = request.json or {}
    church_id = request.current_member["church_id"]
    member_id = data.get("member_id")
    if not member_id:
        return jsonify({"error": "member_id is required"}), 400

    target = Member.query.get_or_404(member_id)
    if target.church_id != church_id:
        return jsonify({"error": "member does not belong to this church"}), 403

    existing = FollowUpTeamMember.query.filter_by(church_id=church_id, member_id=member_id).first()
    if existing:
        existing.active = True
        db.session.commit()
        return jsonify(existing.to_dict())

    team_member = FollowUpTeamMember(church_id=church_id, member_id=member_id, active=True)
    db.session.add(team_member)
    db.session.commit()
    return jsonify(team_member.to_dict()), 201


@bp.route("/admin/follow-up-team/<int:team_member_id>", methods=["DELETE"])
@role_required(ROLE_ADMIN)
@church_scoped
def remove_follow_up_team_member(team_member_id):
    """Soft-remove: sets inactive rather than deleting, so past
    assignment history tied to this row stays intact."""
    team_member = FollowUpTeamMember.query.get_or_404(team_member_id)
    team_member.active = False
    db.session.commit()
    return jsonify(team_member.to_dict())


# ---------- CHURCH ESCALATION CONFIG ----------

@bp.route("/admin/church/escalation-settings", methods=["GET"])
@role_required(ROLE_ADMIN)
@church_scoped
def get_escalation_settings():
    church_id = request.current_member["church_id"]
    church = Church.query.get_or_404(church_id)
    return jsonify({
        "follow_up_threshold": church.follow_up_threshold,
        "leader_escalation_days": church.leader_escalation_days,
        "senior_leadership_id": church.senior_leadership_id,
        "senior_leadership_name": church.members and next(
            (m.full_name for m in church.members if m.id == church.senior_leadership_id), None
        ),
    })


@bp.route("/admin/church/escalation-settings", methods=["POST"])
@role_required(ROLE_ADMIN)
@church_scoped
def update_escalation_settings():
    """
    Configures the optional senior-leadership tier above admin.
    Leaving senior_leadership_id unset (null) keeps a flat
    admin-is-top structure — nothing changes for churches that
    don't need this tier.
    """
    data = request.json or {}
    church_id = request.current_member["church_id"]
    church = Church.query.get_or_404(church_id)

    if "follow_up_threshold" in data:
        church.follow_up_threshold = data["follow_up_threshold"]
    if "leader_escalation_days" in data:
        church.leader_escalation_days = data["leader_escalation_days"]
    if "senior_leadership_id" in data:
        senior_id = data["senior_leadership_id"]
        if senior_id is not None:
            senior = Member.query.get_or_404(senior_id)
            if senior.church_id != church_id:
                return jsonify({"error": "senior_leadership_id must belong to this church"}), 403
        church.senior_leadership_id = senior_id

    db.session.commit()
    return jsonify({
        "follow_up_threshold": church.follow_up_threshold,
        "leader_escalation_days": church.leader_escalation_days,
        "senior_leadership_id": church.senior_leadership_id,
    })


# ---------- NOTIFICATION LOG ----------

@bp.route("/my/notifications", methods=["GET"])
@login_required
def my_notifications():
    """
    A user's own notification history — currently all entries are
    status='logged_only' since no SMS/email provider is wired in
    yet. This endpoint exists now so the frontend can already build
    against it; the only future change is NotificationLog.status
    moving from 'logged_only' to 'sent'/'failed' once a provider
    is connected in _log_notification().
    """
    user_id = request.current_member["member_id"]
    logs = (
        NotificationLog.query
        .filter_by(recipient_member_id=user_id)
        .order_by(NotificationLog.created_at.desc())
        .limit(50)
        .all()
    )
    return jsonify([n.to_dict() for n in logs])
"""
Cell meeting compliance: leaders commit to a recurring day/time when
their cell is created or edited by an admin. Each week, the leader
uploads proof (image or video) that the meeting happened. An admin or
the church's senior leadership reviews and confirms it -- confirmation
is the only thing that turns the status green; uploading proof alone
is not enough, mirroring how AttendanceRecord vs FollowUpAssignment
already separate "the thing happened" from "someone verified it."

Missed weeks (no proof submitted at all by the time the next scheduled
meeting rolls around) increment a consecutive-miss counter on the
CellGroup itself, the same pattern Member.consecutive_absences already
uses for individual attendance.
"""
from datetime import datetime, date, timedelta
from app.database import db
from app.models import (
    CellGroup, CellMeetingSchedule, CellMeetingProof, Member, Church,
)


def set_cell_schedule(cell_id, day_of_week, meeting_time, created_by_id):
    """
    Admin sets or updates the recurring meeting slot for a cell.
    One schedule per cell -- calling this again just updates the
    existing row rather than creating a second one, since a cell
    only meets on one recurring slot at a time in this design.
    """
    existing = CellMeetingSchedule.query.filter_by(cell_id=cell_id).first()
    if existing:
        existing.day_of_week = day_of_week
        existing.meeting_time = meeting_time
        db.session.commit()
        return existing

    schedule = CellMeetingSchedule(
        cell_id=cell_id,
        day_of_week=day_of_week,
        meeting_time=meeting_time,
        created_by=created_by_id,
    )
    db.session.add(schedule)
    db.session.commit()
    return schedule


def submit_meeting_proof(cell_id, submitted_by_id, proof_url, proof_type, meeting_date=None):
    """
    Cell leader uploads proof for a specific week. proof_url points
    at wherever the file actually lives (Supabase Storage, S3, etc)
    -- this function never touches the file itself, only the record
    of it, same separation routes.py already keeps between auth and
    business logic.
    """
    if proof_type not in ("image", "video"):
        raise ValueError("proof_type must be 'image' or 'video'")

    proof = CellMeetingProof(
        cell_id=cell_id,
        submitted_by=submitted_by_id,
        meeting_date=meeting_date or date.today(),
        proof_url=proof_url,
        proof_type=proof_type,
        status="pending",
    )
    db.session.add(proof)
    db.session.commit()
    return proof


def confirm_meeting_proof(proof_id, confirmed_by_id):
    """
    Admin or senior leadership marks a submitted proof as confirmed.
    This is the action that turns the UI toggle green -- uploading
    proof alone leaves status='pending', not confirmed. Resets the
    cell's consecutive_missed_weeks to 0, the same way a present
    attendance record would reset Member.consecutive_absences.
    """
    proof = CellMeetingProof.query.get(proof_id)
    if not proof:
        raise ValueError("Proof not found")

    proof.status = "confirmed"
    proof.confirmed_by = confirmed_by_id
    proof.confirmed_at = datetime.utcnow()

    cell = CellGroup.query.get(proof.cell_id)
    if cell:
        cell.consecutive_missed_weeks = 0

    db.session.commit()
    return proof


def reject_meeting_proof(proof_id, confirmed_by_id):
    """Admin can also reject proof that doesn't actually show the
    meeting happened -- status stays visibly distinct from pending
    so the leader knows to resubmit, rather than silently staying grey."""
    proof = CellMeetingProof.query.get(proof_id)
    if not proof:
        raise ValueError("Proof not found")

    proof.status = "rejected"
    proof.confirmed_by = confirmed_by_id
    proof.confirmed_at = datetime.utcnow()
    db.session.commit()
    return proof


def get_pending_proofs_for_review(church_id):
    """Everything an admin/senior leadership needs to review right
    now, across every cell in the church -- the queue behind the
    green-toggle screen."""
    return (
        db.session.query(CellMeetingProof)
        .join(CellGroup, CellMeetingProof.cell_id == CellGroup.id)
        .filter(CellGroup.church_id == church_id, CellMeetingProof.status == "pending")
        .order_by(CellMeetingProof.meeting_date.desc())
        .all()
    )


def get_cell_compliance_history(cell_id, limit=12):
    """Proof history for one cell, most recent first -- what a
    facilitator or admin sees when drilling into a single cell's
    track record rather than the church-wide review queue."""
    return (
        CellMeetingProof.query.filter_by(cell_id=cell_id)
        .order_by(CellMeetingProof.meeting_date.desc())
        .limit(limit)
        .all()
    )


def check_and_flag_missed_weeks(church_id):
    """
    Run periodically (e.g. once a day via a scheduled job, or on
    dashboard load same as get_leader_accountability_overview already
    does for leader attendance). For every cell whose scheduled
    meeting day has passed this week with no proof submitted at all,
    increments consecutive_missed_weeks. This is the mechanism that
    surfaces silence, not just rejected/pending proof -- a cell that
    never uploads anything is the case that most needs flagging.
    """
    today = date.today()
    cells_with_schedules = (
        db.session.query(CellGroup, CellMeetingSchedule)
        .join(CellMeetingSchedule, CellGroup.id == CellMeetingSchedule.cell_id)
        .filter(CellGroup.church_id == church_id)
        .all()
    )

    flagged = []
    for cell, schedule in cells_with_schedules:
        days_since_scheduled_day = (today.weekday() - schedule.day_of_week) % 7
        this_weeks_meeting_date = today - timedelta(days=days_since_scheduled_day)

        # Has this week's meeting date already passed, and is there
        # truly no proof at all (pending, confirmed, or rejected) for it?
        if this_weeks_meeting_date <= today:
            proof_exists = CellMeetingProof.query.filter_by(
                cell_id=cell.id, meeting_date=this_weeks_meeting_date
            ).first()
            if not proof_exists:
                cell.consecutive_missed_weeks = (cell.consecutive_missed_weeks or 0) + 1
                flagged.append(cell)

    db.session.commit()
    return flagged

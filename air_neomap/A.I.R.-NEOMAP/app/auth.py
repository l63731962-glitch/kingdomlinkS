from functools import wraps
from datetime import datetime, timedelta
import jwt
from flask import request, jsonify, current_app
from app.models import Member

ROLE_ADMIN = "admin"
ROLE_LEADER = "leader"
ROLE_ADULT = "adult"
ROLE_CHILD = "child"


def generate_token(member_id, church_id, role):
    payload = {
        "member_id": member_id,
        "church_id": church_id,
        "role": role,
        "exp": datetime.utcnow() + timedelta(days=14),
    }
    return jwt.encode(payload, current_app.config["SECRET_KEY"], algorithm="HS256")


def _decode_token():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.split(" ", 1)[1]
    try:
        return jwt.decode(token, current_app.config["SECRET_KEY"], algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None


def login_required(f):
    """Attaches request.current_member (dict from token) or 401s."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        payload = _decode_token()
        if not payload:
            return jsonify({"error": "Unauthorized"}), 401
        request.current_member = payload
        return f(*args, **kwargs)
    return wrapper


def role_required(*allowed_roles):
    """
    Server-side role gate. This is the ONLY place role enforcement
    should live — never trust a hidden button or a client-side
    check. Every sensitive route (register member, take attendance,
    view another leader's queue) must be wrapped in this.
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            payload = _decode_token()
            if not payload:
                return jsonify({"error": "Unauthorized"}), 401
            if payload.get("role") not in allowed_roles:
                return jsonify({"error": "Forbidden — insufficient role"}), 403
            request.current_member = payload
            return f(*args, **kwargs)
        return wrapper
    return decorator


def church_scoped(f):
    """
    Ensures any church_id in the route/query/body matches the
    caller's own church_id from the token. Prevents a leader at
    Church A from ever pulling data scoped to Church B by editing
    a URL or payload.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        payload = getattr(request, "current_member", None) or _decode_token()
        if not payload:
            return jsonify({"error": "Unauthorized"}), 401

        # request.json raises UnsupportedMediaType on a GET with no
        # JSON body/content-type -- get_json(silent=True) returns
        # None instead of raising, which is what "or {}" below was
        # actually trying to guard against.
        body = request.get_json(silent=True) or {}
        requested_church_id = (
            kwargs.get("church_id")
            or request.args.get("church_id")
            or body.get("church_id")
        )
        if requested_church_id and int(requested_church_id) != payload["church_id"]:
            return jsonify({"error": "Forbidden — cross-church access denied"}), 403

        return f(*args, **kwargs)
    return wrapper
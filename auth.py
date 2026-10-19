import base64
import hashlib
import hmac
import json
import os
import time

TOKEN_SECONDS = 8 * 3600  # login 8 ghante chalta hai

SECRET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".auth_secret")


def _load_secret():
    """Token par sign karne ki chaabi."""
    env = os.environ.get("AUTH_SECRET")
    if env:
        return env.encode()
    try:
        if not os.path.exists(SECRET_FILE):
            with open(SECRET_FILE, "wb") as f:
                f.write(base64.urlsafe_b64encode(os.urandom(32)))
        with open(SECRET_FILE, "rb") as f:
            return f.read().strip()
    except OSError:
        return b"demo-fallback-secret-change-me"


SECRET = _load_secret()


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000).hex()


# Demo accounts. Password seedha store nahi hota, sirf hash banta hai.
_DEMO = [
    ("manager", "manager123", "Manager", "Sara (Manager)"),
    ("ali", "ali123", "Technician", "Ali (Technician)"),
    ("auditor", "auditor123", "Auditor", "Auditor"),
]

USERS = {}
for _u, _p, _role, _name in _DEMO:
    _salt = os.urandom(16)
    USERS[_u] = {
        "username": _u,
        "salt": _salt,
        "hash": _hash(_p, _salt),
        "role": _role,
        "name": _name,
    }


def _public(u):
    return {"username": u["username"], "name": u["name"], "role": u["role"]}


def verify(username, password):
    """Sahi ho to user dict, warna None."""
    u = USERS.get((username or "").strip().lower())
    if u is None:
        _hash("dummy", b"0" * 16)  # timing se username ka andaza na lage
        return None
    if hmac.compare_digest(_hash(password or "", u["salt"]), u["hash"]):
        return _public(u)
    return None


def make_token(username):
    payload = base64.urlsafe_b64encode(
        json.dumps({"u": username, "exp": int(time.time()) + TOKEN_SECONDS}).encode()
    ).decode()
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + sig


def user_from_header(header):
    """'Authorization: Bearer <token>' se user nikalta hai. Galat ya expired ho to None."""
    if not header or not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    try:
        payload, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    good = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except Exception:
        return None
    if data.get("exp", 0) < time.time():
        return None
    u = USERS.get(data.get("u"))
    return _public(u) if u else None
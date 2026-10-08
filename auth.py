"""
Login for the Competitor Table app (several users, each with a username + password).

Two kinds of users:
  1. Render -> Environment USERS (fixed ADMINS, cannot be changed on the page - so nobody is ever locked out):
       USERS       = ashref:Password1;ahsana:Password2
                     (username:password, users separated by ;  -  usernames are not case-sensitive)
  2. Users created on the page Settings -> User management (table app_users in french_fragrance_db).
     Passwords are saved hashed (never readable). Role = admin or user.
       admin -> everything + User management
       user  -> everything except User management
  SECRET_KEY  = any long random text (keeps people logged in after a deploy; without it everyone must log in again)

What is protected: every page and button (/competitors, Update price, OK / Not OK, Update all ...).
Not protected:
  /           health check for Render
  /login, /logout
  "run" URLs opened with the correct ?secret=...  (so your bookmarks / scripts keep working)

Plugged into app.py:
  from auth import auth_bp, init_auth
  app.register_blueprint(auth_bp)
  init_auth(app)
"""

import os
import re
import hmac
import time
import secrets
from datetime import timedelta
from urllib.parse import urlparse, quote

import psycopg2
import psycopg2.extras
from flask import Blueprint, request, session, redirect, jsonify, render_template_string, g
from werkzeug.security import generate_password_hash, check_password_hash

auth_bp = Blueprint("auth", __name__)

OPEN_PATHS = {"/", "/login", "/logout", "/favicon.ico"}     # pages anyone can open


def auth_log(*args):
    print(*args, flush=True)


def load_users():
    """USERS env var 'name:pass;name2:pass2' -> {"name": "pass"} (usernames in lower case)."""
    users = {}
    for part in (os.environ.get("USERS") or "").replace("\n", ";").split(";"):
        if ":" in part:
            name, pw = part.split(":", 1)
            if name.strip() and pw:
                users[name.strip().lower()] = pw
    return users


# ---------------------------------------------------------------- users saved in the database (User management)
USERS_CREATE = """
CREATE TABLE IF NOT EXISTS app_users (
    username       TEXT PRIMARY KEY,            -- lower case, e.g. 'rayhaan'
    password_hash  TEXT NOT NULL,               -- hashed password (the real password is never saved)
    role           TEXT NOT NULL DEFAULT 'user',-- 'admin' or 'user'
    active         BOOLEAN NOT NULL DEFAULT TRUE,  -- FALSE = disabled, cannot log in
    created_by     TEXT,
    created_at     TIMESTAMP DEFAULT NOW(),
    last_login     TIMESTAMP
);
"""
ROLES = ("admin", "user")
NAME_RE = re.compile(r"^[a-z0-9._-]{2,30}$")      # allowed usernames
MIN_PASSWORD = 6


def users_db():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    conn = psycopg2.connect(url, sslmode="require")
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(USERS_CREATE)
    return conn, cur


def db_user(username):
    """One user from app_users (dict) or None."""
    try:
        conn, cur = users_db()
        try:
            cur.execute("SELECT * FROM app_users WHERE username = %s", (username,))
            row = cur.fetchone()
            conn.commit()
            return row
        finally:
            cur.close()
            conn.close()
    except Exception as ex:
        auth_log(f"[USERS] Could not read user '{username}' from the database: {ex}")
        return None


def check_login(username, password):
    """Role ('admin' / 'user') when the username + password are right, else None.
    Render USERS are checked first (always admin), then the database users (only active ones)."""
    username = (username or "").strip().lower()
    stored = load_users().get(username)
    if stored is not None:
        ok = hmac.compare_digest(stored.encode(), (password or "").encode())
        auth_log(f"[AUTH] '{username}' is a Render USERS admin -> password {'OK' if ok else 'wrong'}")
        return "admin" if ok else None
    row = db_user(username)
    if not row:
        auth_log(f"[AUTH] '{username}' not found (not in USERS, not in app_users)")
        return None
    if not row["active"]:
        auth_log(f"[AUTH] '{username}' is disabled -> login refused")
        return None
    if not check_password_hash(row["password_hash"], password or ""):
        return None
    try:
        conn, cur = users_db()
        cur.execute("UPDATE app_users SET last_login = NOW() WHERE username = %s", (username,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as ex:
        auth_log(f"[USERS] last_login not saved: {ex}")
    return row["role"]


def current_role(username):
    """Role of a logged-in user right now (so a disabled / removed user is logged out at once).
    None = no longer allowed."""
    if username in load_users():
        return "admin"
    row = db_user(username)
    return row["role"] if row and row["active"] else None


def is_admin():
    return g.get("role") == "admin"


def list_users():
    """All users for the User management page: Render USERS first (fixed), then the database users."""
    out = [{"username": u, "role": "admin", "active": True, "fixed": True, "created_by": "Render USERS",
            "created_at": None, "last_login": None} for u in sorted(load_users())]
    try:
        conn, cur = users_db()
        try:
            cur.execute("SELECT * FROM app_users ORDER BY username")
            out += [{**r, "fixed": False} for r in cur.fetchall()]
            conn.commit()
        finally:
            cur.close()
            conn.close()
    except Exception as ex:
        auth_log(f"[USERS] Could not list users: {ex}")
    auth_log(f"[USERS] {len(out)} users ({len(load_users())} from Render USERS)")
    return out


def init_auth(app):
    """Turn on the login check for every request."""
    app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    if not os.environ.get("SECRET_KEY"):
        auth_log("[AUTH] SECRET_KEY not set -> everyone has to log in again after each deploy")
    if not load_users():
        auth_log("[AUTH] WARNING: USERS is not set -> only users from User management can log in. "
                 "Keep at least one admin in Render -> Environment USERS")
    app.permanent_session_lifetime = timedelta(days=30)      # stay logged in for 30 days
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get("RENDER") is not None   # https on Render

    @app.before_request
    def require_login():
        g.user = session.get("user")
        g.role = None
        if g.user:
            g.role = current_role(g.user)            # checked every time: disabled/removed users are logged out
            if g.role is None:
                auth_log(f"[AUTH] '{g.user}' is disabled or removed -> logged out")
                session.clear()
                g.user = None
            elif session.get("role") != g.role:
                session["role"] = g.role
        if request.path in OPEN_PATHS:
            return None
        # run / status URLs opened with the right ?secret= still work without logging in
        run_secret = os.environ.get("RUN_SECRET", "")
        if run_secret and hmac.compare_digest(request.args.get("secret", ""), run_secret):
            return None
        if g.user:
            if request.method == "POST":
                auth_log(f"[AUTH] {g.user} -> {request.method} {request.path}")   # who changed what
            return None
        if request.method != "GET" or request.is_json:
            return jsonify({"ok": False, "error": "Please log in again (session expired)"}), 401
        return redirect("/login?next=" + request.full_path.rstrip("?"))


LOGIN_HTML = """
<!doctype html><html><head><title>Log in - Competitors</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#1c1b19;--card:#252420;--line:#3a3833;--text:#f2efe9;--muted:#a9a397;--red:#e0806c}
 *{box-sizing:border-box}
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);
      color:var(--text);font-family:Roboto,-apple-system,Segoe UI,Arial,sans-serif;padding:20px}
 form{width:100%;max-width:380px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:28px}
 h1{font-size:24px;margin:0 0 6px} p{color:var(--muted);margin:0 0 20px;font-size:14px}
 label{display:block;font-size:14px;color:var(--muted);margin:14px 0 6px}
 input{width:100%;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:10px;
       padding:12px 14px;font-size:16px}
 button{width:100%;margin-top:22px;background:#f2d675;color:#1c1b19;border:0;border-radius:10px;padding:13px;
        font-size:16px;font-weight:700;cursor:pointer}
 .pw{position:relative}
 .pw input{padding-right:48px}
 .eye{position:absolute;right:6px;top:50%;transform:translateY(-50%);width:38px;height:38px;margin:0;padding:0;
      background:none;border:0;border-radius:8px;font-size:20px;cursor:pointer;color:var(--muted)}
 .eye:hover{background:var(--line)}
 .err{color:var(--red);font-size:14px;margin-top:14px}
</style></head><body>
<form method="post">
  <h1>Competitor table</h1>
  <p>Log in to continue</p>
  <input type="hidden" name="next" value="{{ next }}">
  <label for="u">Username</label>
  <input id="u" name="username" autocomplete="username" autofocus required>
  <label for="p">Password</label>
  <div class="pw">
    <input id="p" name="password" type="password" autocomplete="current-password" required>
    {# eye button: show / hide the password #}
    <button type="button" class="eye" id="eye" onclick="togglePw()" aria-label="Show password" title="Show password">&#128065;</button>
  </div>
  <button type="submit">Log in</button>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
</form>
<script>
  function togglePw() {
    var p = document.getElementById("p"), e = document.getElementById("eye");
    var show = p.type === "password";
    p.type = show ? "text" : "password";
    e.innerHTML = show ? "&#128584;" : "&#128065;";              // see-no-evil monkey = hide, eye = show
    e.title = show ? "Hide password" : "Show password";
    e.setAttribute("aria-label", e.title);
    p.focus();
  }
</script>
</body></html>
"""


def safe_next(url):
    """Only go back to a page of THIS site after login (never to another website)."""
    p = urlparse(url or "")
    return url if url and not p.scheme and not p.netloc and url.startswith("/") else "/competitors"


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    nxt = safe_next(request.values.get("next"))
    error = None
    if request.method == "POST":
        user = (request.form.get("username") or "").strip().lower()
        role = check_login(user, request.form.get("password"))
        if role:
            session.clear()
            session.permanent = True
            session["user"] = user
            session["role"] = role
            auth_log(f"[AUTH] Login OK: {user} ({role})")
            return redirect(nxt)
        time.sleep(1)                                         # slows down password guessing
        auth_log(f"[AUTH] Login FAILED for '{user}' from {request.headers.get('X-Forwarded-For', request.remote_addr)}")
        error = "Wrong username or password."
    return render_template_string(LOGIN_HTML, next=nxt, error=error)


@auth_bp.route("/logout")
def logout():
    auth_log(f"[AUTH] Logout: {session.get('user')}")
    session.clear()
    return redirect("/login")


# ---------------------------------------------------------------- User management actions (admins only)
# The page itself is Settings -> User management (/settings/users in competitors_page.py).
# Every action goes back to that page with ?msg=... or ?error=...
def back(msg=None, error=None):
    q = "msg=" + quote(msg) if msg else "error=" + quote(error or "")
    return redirect("/settings/users?" + q)


def admin_only():
    if not is_admin():
        auth_log(f"[USERS] '{g.user}' tried to manage users but is not an admin")
        return back(error="Only admins can manage users.")
    return None


def editable(name):
    """Error text when this user cannot be changed on the page, else None."""
    if name in load_users():
        return f"'{name}' is set in Render (USERS) - change it there."
    if not db_user(name):
        return f"User '{name}' not found."
    return None


@auth_bp.route("/settings/users/add", methods=["POST"])
def users_add():
    if (r := admin_only()):
        return r
    name = (request.form.get("username") or "").strip().lower()
    pw = request.form.get("password") or ""
    role = request.form.get("role") if request.form.get("role") in ROLES else "user"
    auth_log(f"[USERS] {g.user} -> add '{name}' ({role})")
    if not NAME_RE.match(name):
        return back(error="Username: 2-30 characters, only letters, numbers, . _ -")
    if len(pw) < MIN_PASSWORD:
        return back(error=f"Password must have at least {MIN_PASSWORD} characters.")
    if name in load_users() or db_user(name):
        return back(error=f"User '{name}' already exists.")
    conn, cur = users_db()
    cur.execute("INSERT INTO app_users (username, password_hash, role, created_by) VALUES (%s, %s, %s, %s)",
                (name, generate_password_hash(pw), role, g.user))
    conn.commit()
    cur.close()
    conn.close()
    auth_log(f"[USERS] Added '{name}' as {role}")
    return back(msg=f"User '{name}' added ({role}).")


@auth_bp.route("/settings/users/<name>/password", methods=["POST"])
def users_password(name):
    if (r := admin_only()):
        return r
    if (err := editable(name)):
        return back(error=err)
    pw = request.form.get("password") or ""
    if len(pw) < MIN_PASSWORD:
        return back(error=f"Password must have at least {MIN_PASSWORD} characters.")
    conn, cur = users_db()
    cur.execute("UPDATE app_users SET password_hash = %s WHERE username = %s", (generate_password_hash(pw), name))
    conn.commit()
    cur.close()
    conn.close()
    auth_log(f"[USERS] {g.user} changed the password of '{name}'")
    return back(msg=f"Password of '{name}' changed.")


@auth_bp.route("/settings/users/<name>/role", methods=["POST"])
def users_role(name):
    if (r := admin_only()):
        return r
    if (err := editable(name)):
        return back(error=err)
    role = request.form.get("role")
    if role not in ROLES:
        return back(error="Unknown role.")
    if name == g.user and role != "admin":
        return back(error="You cannot remove your own admin role.")
    conn, cur = users_db()
    cur.execute("UPDATE app_users SET role = %s WHERE username = %s", (role, name))
    conn.commit()
    cur.close()
    conn.close()
    auth_log(f"[USERS] {g.user} set '{name}' role -> {role}")
    return back(msg=f"'{name}' is now {role}.")


@auth_bp.route("/settings/users/<name>/active", methods=["POST"])
def users_active(name):
    if (r := admin_only()):
        return r
    if (err := editable(name)):
        return back(error=err)
    active = request.form.get("active") == "1"
    if name == g.user and not active:
        return back(error="You cannot disable yourself.")
    conn, cur = users_db()
    cur.execute("UPDATE app_users SET active = %s WHERE username = %s", (active, name))
    conn.commit()
    cur.close()
    conn.close()
    auth_log(f"[USERS] {g.user} {'enabled' if active else 'disabled'} '{name}'")
    return back(msg=f"'{name}' {'enabled' if active else 'disabled (cannot log in)'}.")


@auth_bp.route("/settings/users/<name>/delete", methods=["POST"])
def users_delete(name):
    if (r := admin_only()):
        return r
    if (err := editable(name)):
        return back(error=err)
    if name == g.user:
        return back(error="You cannot remove yourself.")
    conn, cur = users_db()
    cur.execute("DELETE FROM app_users WHERE username = %s", (name,))
    conn.commit()
    cur.close()
    conn.close()
    auth_log(f"[USERS] {g.user} removed '{name}'")
    return back(msg=f"User '{name}' removed.")
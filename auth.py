"""
Login for the Competitor Table app (several users, each with a username + password).

All users live ONLY in the database (table app_users in french_fragrance_db), managed on the page
Settings -> User management. Passwords are saved hashed (never readable). Role = admin or user.
    admin -> everything + User management
    user  -> everything except User management
There is always at least one active admin: the last admin cannot be removed, disabled or made a user.

One-time move from Render: if Render -> Environment still has USERS (name:pass;name2:pass2) and the
app_users table is EMPTY, those users are copied into the database as admins (passwords hashed) at start-up.
After the first deploy, delete USERS from Render - it is not used any more.

EMERGENCY reset (when every admin is locked out / forgot the password):
  Render -> Environment add  RESET_ADMIN = ashref:NewPassword123   -> deploy
  At start-up that user gets the new password, becomes an ACTIVE ADMIN and is unlocked (see the logs).
  Then DELETE RESET_ADMIN from Render (while it is set, every deploy resets that password again).

Wrong passwords: after 5 wrong tries the username is locked for 15 minutes
(an admin can unlock it at once by setting a new password, or with Enable).
  SECRET_KEY  = any long random text (keeps people logged in after a deploy; without it everyone must log in again)

What is protected: every page and button (/competitors, Update price, OK / Not OK, Update all ...).
Not protected:
  /           health check for Render
  /login, /logout
  ONLY the run / status / test links (RUN_PATH below) opened with the correct ?secret=...
  (the secret no longer opens the competitor table, Update price, Settings ...)

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
from urllib.parse import urlparse, quote, urlencode

import psycopg2
import psycopg2.extras
from flask import Blueprint, request, session, redirect, jsonify, render_template_string, g
from werkzeug.security import generate_password_hash, check_password_hash

auth_bp = Blueprint("auth", __name__)

OPEN_PATHS = {"/", "/login", "/logout", "/favicon.ico"}     # pages anyone can open


def auth_log(*args):
    print(*args, flush=True)


def env_users():
    """OLD Render USERS env var 'name:pass;name2:pass2' -> {"name": "pass"}.
    Only used ONCE to copy those users into the database (import_env_users). Not used for login."""
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
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS failed_logins INT NOT NULL DEFAULT 0;   -- wrong passwords in a row
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS locked_until  TIMESTAMP;                -- locked until this time
"""
ROLES = ("admin", "user")
NAME_RE = re.compile(r"^[a-z0-9._-]{2,30}$")      # allowed usernames
# MAIN ADMIN (owner): always an active admin. Other admins cannot change its password, make it a user,
# disable it or remove it - only the main admin can change its own password. Change the name here if needed.
MAIN_ADMIN = "ashref"
MIN_PASSWORD = 8                                  # for new / changed passwords (old ones keep working)
MAX_TRIES = 5                                     # wrong passwords in a row ...
LOCK_MINUTES = 15                                 # ... -> username locked for this long

# Links that may be opened with ?secret=... (or header X-Run-Secret) WITHOUT logging in:
#   /samawa/run, /ounass/status, /vperfumes/test, /essenzi/load ...  and  /competitor/match-xx, load-fi,
#   load-our-links, update-least. Everything else (pages, Update price, Settings ...) needs a login.
RUN_PATH = re.compile(r"^/([a-z-]+/(run|status|test|load)"
                      r"|competitor/(load-fi|load-our-links|update-least|match-[a-z]+))$")


_table_ready = False          # set to True after the table was created / updated once


def users_db():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    conn = psycopg2.connect(url, sslmode="require")
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    global _table_ready
    if not _table_ready:
        # create / update the table ONCE (ALTER TABLE locks the table, so it must not run on every connection)
        cur.execute(USERS_CREATE)
        conn.commit()
        _table_ready = True
        auth_log("[USERS] Table app_users ready")
    return conn, cur


def db_user(username):
    """One user from app_users (dict) or None."""
    try:
        conn, cur = users_db()
        try:
            cur.execute("SELECT *, GREATEST(0, CEIL(EXTRACT(EPOCH FROM (locked_until - LOCALTIMESTAMP)) / 60))::int AS lock_left FROM app_users WHERE username = %s", (username,))
            row = cur.fetchone()
            conn.commit()
            return row
        finally:
            cur.close()
            conn.close()
    except Exception as ex:
        auth_log(f"[USERS] Could not read user '{username}' from the database: {ex}")
        return None


def import_env_users():
    """ONE-TIME move: copy Render USERS into app_users (as admins, passwords hashed) when the table is empty.
    After this has run once, USERS can be deleted from Render."""
    old = env_users()
    try:
        conn, cur = users_db()
        try:
            cur.execute("SELECT COUNT(*) AS n FROM app_users")
            n = cur.fetchone()["n"]
            if n:
                auth_log(f"[USERS] {n} users in the database"
                         + (" - USERS in Render is NOT used any more, you can delete it" if old else ""))
            elif old:
                for name, pw in old.items():
                    cur.execute("INSERT INTO app_users (username, password_hash, role, created_by) "
                                "VALUES (%s, %s, 'admin', 'moved from Render USERS') ON CONFLICT DO NOTHING",
                                (name, generate_password_hash(pw)))
                auth_log(f"[USERS] Moved {len(old)} users from Render USERS into the database as admins: "
                         f"{', '.join(old)}. Now delete USERS from Render -> Environment.")
            else:
                auth_log("[USERS] WARNING: no users in the database and no USERS in Render -> nobody can log in. "
                         "Add USERS=name:password in Render once, deploy, then remove it.")
            conn.commit()
        finally:
            cur.close()
            conn.close()
    except Exception as ex:
        auth_log(f"[USERS] Could not check / import users: {ex}")


def reset_admin_from_env():
    """EMERGENCY: Render env RESET_ADMIN=name:newpassword -> that user gets the new password,
    becomes an active admin and is unlocked. Remove RESET_ADMIN from Render afterwards."""
    raw = (os.environ.get("RESET_ADMIN") or "").strip()
    if not raw:
        return
    if ":" not in raw:
        auth_log("[RESET-ADMIN] Ignored: RESET_ADMIN must look like name:newpassword")
        return
    name, pw = raw.split(":", 1)
    name = name.strip().lower()
    if not NAME_RE.match(name) or len(pw) < MIN_PASSWORD:
        auth_log(f"[RESET-ADMIN] Ignored: username must be 2-30 letters/numbers/._- and password "
                 f"at least {MIN_PASSWORD} characters")
        return
    try:
        conn, cur = users_db()
        cur.execute("""INSERT INTO app_users (username, password_hash, role, active, created_by)
                       VALUES (%s, %s, 'admin', TRUE, 'RESET_ADMIN')
                       ON CONFLICT (username) DO UPDATE SET password_hash = EXCLUDED.password_hash,
                           role = 'admin', active = TRUE, failed_logins = 0, locked_until = NULL""",
                    (name, generate_password_hash(pw)))
        conn.commit()
        cur.close()
        conn.close()
        auth_log(f"[RESET-ADMIN] '{name}' now has the new password and is an active admin. "
                 f"DELETE RESET_ADMIN from Render -> Environment now.")
    except Exception as ex:
        auth_log(f"[RESET-ADMIN] Failed: {ex}")


def active_admins(cur):
    """How many active admins there are (used so the LAST admin can never be removed)."""
    cur.execute("SELECT COUNT(*) AS n FROM app_users WHERE role = 'admin' AND active")
    return cur.fetchone()["n"]


def lock_minutes_left(row):
    """Minutes until a locked username can try again (0 = not locked). Worked out in the database
    (lock_left), so the server clock / time zone does not matter."""
    return (row or {}).get("lock_left") or 0


def check_login(username, password):
    """(role, error). role = 'admin' / 'user' when the username + password are right (database users only).
    5 wrong passwords in a row -> the username is locked for 15 minutes."""
    username = (username or "").strip().lower()
    row = db_user(username)
    if not row:
        auth_log(f"[AUTH] '{username}' not found in app_users")
        return None, None
    if not row["active"]:
        auth_log(f"[AUTH] '{username}' is disabled -> login refused")
        return None, None
    left = lock_minutes_left(row)
    if left:
        auth_log(f"[LOCK] '{username}' is locked for {left} more minute(s) -> login refused")
        return None, f"Too many wrong passwords. Try again in {left} minute{'s' if left != 1 else ''}."
    conn, cur = users_db()
    try:
        if not check_password_hash(row["password_hash"], password or ""):
            tries = (row["failed_logins"] or 0) + 1
            if tries >= MAX_TRIES:                         # lock now, start counting again afterwards
                cur.execute("UPDATE app_users SET failed_logins = 0, locked_until = NOW() + %s * INTERVAL '1 minute' "
                            "WHERE username = %s", (LOCK_MINUTES, username))
                auth_log(f"[LOCK] '{username}': {tries} wrong passwords -> locked for {LOCK_MINUTES} minutes")
                return None, f"Too many wrong passwords. Try again in {LOCK_MINUTES} minutes."
            cur.execute("UPDATE app_users SET failed_logins = %s WHERE username = %s", (tries, username))
            auth_log(f"[LOCK] '{username}': wrong password {tries}/{MAX_TRIES}")
            return None, None
        cur.execute("UPDATE app_users SET last_login = NOW(), failed_logins = 0, locked_until = NULL "
                    "WHERE username = %s", (username,))
        return row["role"], None
    finally:
        conn.commit()
        cur.close()
        conn.close()


def current_role(username):
    """Role of a logged-in user right now (so a disabled / removed user is logged out at once).
    None = no longer allowed."""
    row = db_user(username)
    return row["role"] if row and row["active"] else None


def is_admin():
    return g.get("role") == "admin"


def list_users():
    """All users for the User management page (from the database)."""
    out = []
    try:
        conn, cur = users_db()
        try:
            cur.execute("SELECT *, GREATEST(0, CEIL(EXTRACT(EPOCH FROM (locked_until - LOCALTIMESTAMP)) / 60))::int AS lock_left FROM app_users ORDER BY username")
            out += [{**r, "fixed": False, "locked": lock_minutes_left(r), "main": r["username"] == MAIN_ADMIN}
                    for r in cur.fetchall()]
            conn.commit()
        finally:
            cur.close()
            conn.close()
    except Exception as ex:
        auth_log(f"[USERS] Could not list users: {ex}")
    auth_log(f"[USERS] {len(out)} users in the database")
    return out


def init_auth(app):
    """Turn on the login check for every request."""
    app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    if not os.environ.get("SECRET_KEY"):
        auth_log("[AUTH] SECRET_KEY not set -> everyone has to log in again after each deploy")
    import_env_users()                                        # one-time move of old Render USERS
    auth_log(f"[USERS] Main admin: '{MAIN_ADMIN}' (protected - other admins cannot change it)")
    reset_admin_from_env()                                    # emergency reset (only when RESET_ADMIN is set)
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
        # ONLY run / status / test links work with the right secret (?secret=... or header X-Run-Secret)
        run_secret = os.environ.get("RUN_SECRET", "")
        given = request.args.get("secret") or request.headers.get("X-Run-Secret") or ""
        if run_secret and given and hmac.compare_digest(given, run_secret):
            if RUN_PATH.match(request.path):
                auth_log(f"[AUTH] Run link with secret: {request.method} {request.path}")
                return None
            auth_log(f"[AUTH] Secret used on {request.path} -> not a run link, login needed")
        if g.user:
            if request.method == "POST":
                auth_log(f"[AUTH] {g.user} -> {request.method} {request.path}")   # who changed what
            return None
        if request.method != "GET" or request.is_json:
            return jsonify({"ok": False, "error": "Please log in again (session expired)"}), 401
        # back to this page after login (the secret is dropped so it never shows in the login link)
        args = [(k, v) for k, v in request.args.items(multi=True) if k != "secret"]
        nxt = request.path + ("?" + urlencode(args) if args else "")
        return redirect("/login?next=" + quote(nxt, safe="/"))


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
        role, lock_error = check_login(user, request.form.get("password"))
        if role:
            session.clear()
            session.permanent = True
            session["user"] = user
            session["role"] = role
            auth_log(f"[AUTH] Login OK: {user} ({role})")
            return redirect(nxt)
        time.sleep(1)                                         # slows down password guessing
        auth_log(f"[AUTH] Login FAILED for '{user}' from {request.headers.get('X-Forwarded-For', request.remote_addr)}")
        error = lock_error or "Wrong username or password."
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


def editable(name, action="change"):
    """Error text when this user cannot be changed on the page, else None.
    The main admin can only change its own password - nothing else, by nobody."""
    if name == MAIN_ADMIN and not (action == "password" and g.user == MAIN_ADMIN):
        auth_log(f"[USERS] Refused: {g.user} tried to {action} the main admin '{MAIN_ADMIN}'")
        return f"'{name}' is the main admin and cannot be changed by other users."
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
    if db_user(name):
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
    if (err := editable(name, "password")):
        return back(error=err)
    pw = request.form.get("password") or ""
    if len(pw) < MIN_PASSWORD:
        return back(error=f"Password must have at least {MIN_PASSWORD} characters.")
    conn, cur = users_db()
    cur.execute("UPDATE app_users SET password_hash = %s, failed_logins = 0, locked_until = NULL "
                "WHERE username = %s", (generate_password_hash(pw), name))         # new password also unlocks
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
    if role != "admin" and db_user(name)["role"] == "admin" and active_admins(cur) <= 1:
        cur.close(); conn.close()
        auth_log(f"[USERS] Refused: '{name}' is the last admin")
        return back(error=f"'{name}' is the last admin - make another admin first.")
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
    u = db_user(name)
    if not active and u["role"] == "admin" and u["active"] and active_admins(cur) <= 1:
        cur.close(); conn.close()
        auth_log(f"[USERS] Refused: '{name}' is the last admin")
        return back(error=f"'{name}' is the last admin - make another admin first.")
    cur.execute("UPDATE app_users SET active = %s, failed_logins = 0, locked_until = NULL "
                "WHERE username = %s", (active, name))                             # Enable also unlocks
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
    u = db_user(name)
    if u["role"] == "admin" and u["active"] and active_admins(cur) <= 1:
        cur.close(); conn.close()
        auth_log(f"[USERS] Refused: '{name}' is the last admin")
        return back(error=f"'{name}' is the last admin - make another admin first.")
    cur.execute("DELETE FROM app_users WHERE username = %s", (name,))
    conn.commit()
    cur.close()
    conn.close()
    auth_log(f"[USERS] {g.user} removed '{name}'")
    return back(msg=f"User '{name}' removed.")
"""
Login for the Competitor Table app (several users, each with a username + password).

Users are set in Render -> Environment (no database needed):
  USERS       = ashref:Password1;ahsana:Password2;rayhaan:Password3
                (username:password, users separated by ;  -  usernames are not case-sensitive)
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
import hmac
import time
import secrets
from datetime import timedelta
from urllib.parse import urlparse

from flask import Blueprint, request, session, redirect, jsonify, render_template_string, g

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


def check_login(username, password):
    """True when the username exists and the password is right (compared safely)."""
    stored = load_users().get((username or "").strip().lower())
    return stored is not None and hmac.compare_digest(stored.encode(), (password or "").encode())


def init_auth(app):
    """Turn on the login check for every request."""
    app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    if not os.environ.get("SECRET_KEY"):
        auth_log("[AUTH] SECRET_KEY not set -> everyone has to log in again after each deploy")
    if not load_users():
        auth_log("[AUTH] WARNING: USERS is not set -> nobody can log in. Add USERS in Render -> Environment")
    app.permanent_session_lifetime = timedelta(days=30)      # stay logged in for 30 days
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.environ.get("RENDER") is not None   # https on Render

    @app.before_request
    def require_login():
        g.user = session.get("user")
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
        if check_login(user, request.form.get("password")):
            session.clear()
            session.permanent = True
            session["user"] = user
            auth_log(f"[AUTH] Login OK: {user}")
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
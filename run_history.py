"""
Run history for the Item Master buttons  (like Airtable -> Automations -> Run history)
=====================================================================================
Every click on "Create product description" or "Create in website" is saved as ONE run with its steps:
  Trigger                          who clicked, which product
  Step 1, 2, 3 ...                 name, Success / Failed, seconds, INPUT (what went in), OUTPUT (what came back)
Page: /item-master/history                 all runs, newest first (Airtable record id is saved, not shown)
      /item-master/history?fi=rec...       runs of one product   ("Run history" link on each card)
      /item-master/history?run=12          one run opened        ("View run" link after a click)
Table: button_runs (one row per click, steps in JSON). Read it with:
  python sql.py "SELECT id, action, status, started_by, started_at, error FROM button_runs ORDER BY id DESC LIMIT 20"

Used in description_gen.py / shopify_create.py like this:
  run = run_history.Run("description", fid, user)
  with run.step("Read product from Airtable", input={...}) as st:
      ...                                   # an error here marks the step (and the run) Failed
      st["output"] = {...}
  run.finish("success")

Where things are (change them here):
  SECTION 1  settings      -> ACTIONS (names on the page), KEEP_RUNS, PER_PAGE
  SECTION 2  table         -> RUNS_CREATE (button_runs)
  SECTION 3  Run           -> start / step / finish (saved after every step, so a running run is visible too)
  SECTION 4  page HTML     -> HIST_HTML
  SECTION 5  route         -> /item-master/history
  SECTION 6  hooks         -> init(): database connection from app.py
"""

import json
import time
import contextlib
from datetime import datetime

import psycopg2.extras
from flask import Blueprint, request, render_template_string, g

import competitors_page                     # menu bar (render_nav, NAV_CSS) + menu counts

history_bp = Blueprint("run_history", __name__)


def log(*a):
    """Print to the Render log (lines start with [RUN-HISTORY])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
ACTIONS = {"description": "Create product description", "website": "Create in website"}
KEEP_RUNS = 5000                       # older runs are deleted (keeps the table small)
PER_PAGE = 50
MAX_TEXT = 6000                        # long values (descriptions, API answers) are cut to this many characters


# ============================================================ SECTION 2: table
RUNS_CREATE = """
CREATE TABLE IF NOT EXISTS button_runs (
    id            SERIAL PRIMARY KEY,
    action        TEXT NOT NULL,               -- 'description' / 'website'
    fi_record_id  TEXT,                        -- Airtable record id of the product
    product       TEXT,                        -- product name / SKU (filled once Airtable was read)
    started_by    TEXT,                        -- user who clicked
    started_at    TIMESTAMP DEFAULT NOW(),
    finished_at   TIMESTAMP,
    status        TEXT,                        -- running / success / failed / stopped
    error         TEXT,                        -- why it failed / stopped
    steps         JSONB                        -- [{name, status, seconds, input, output, error}, ...]
);
CREATE INDEX IF NOT EXISTS button_runs_fi ON button_runs (fi_record_id);
"""
_hooks = {}
_table_ready = False


def _conn():
    """Database connection; creates button_runs once after each start."""
    global _table_ready
    conn = _hooks["connect"]()
    if not _table_ready:
        conn.cursor().execute(RUNS_CREATE)
        conn.commit()
        _table_ready = True
    return conn


def _cut(v):
    """Make a value safe for JSON and not too long (long texts are cut)."""
    if isinstance(v, str):
        return v if len(v) <= MAX_TEXT else v[:MAX_TEXT] + f" ... ({len(v)} characters, cut)"
    if isinstance(v, dict):
        return {str(k): _cut(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_cut(x) for x in v]
    if v is None or isinstance(v, (int, float, bool)):
        return v
    return str(v)


# ============================================================ SECTION 3: Run (one click)
class Run:
    """One button click. Never breaks the button: if saving the history fails, it is only logged."""

    def __init__(self, action, fid, user):
        self.id, self.action, self.fid, self.user = None, action, fid, user
        self.steps, self.product, self.t0 = [], None, time.time()
        self.steps.append({"name": f"Trigger: '{ACTIONS.get(action, action)}' clicked", "status": "success",
                           "seconds": 0, "input": {"user": user, "airtable_record_id": fid}, "output": None})
        try:
            conn = _conn()
            try:
                cur = conn.cursor()
                cur.execute("INSERT INTO button_runs (action, fi_record_id, started_by, status, steps) "
                            "VALUES (%s, %s, %s, 'running', %s) RETURNING id",
                            (action, fid, user, json.dumps(self.steps)))
                self.id = cur.fetchone()[0]
                cur.execute("DELETE FROM button_runs WHERE id <= %s", (self.id - KEEP_RUNS,))
                conn.commit()
            finally:
                conn.close()
            log(f"[RUN-HISTORY] Run {self.id} started: {action} {fid} by {user}")
        except Exception as ex:
            log(f"[RUN-HISTORY] Could not save run start ({str(ex)[:150]}) - the button still works")

    def _save(self, status="running", error=None):
        if self.id is None:
            return
        try:
            conn = _conn()
            try:
                conn.cursor().execute(
                    "UPDATE button_runs SET status = %s, error = %s, product = COALESCE(%s, product), steps = %s, "
                    "finished_at = CASE WHEN %s = 'running' THEN NULL ELSE NOW() END WHERE id = %s",
                    (status, error, self.product, json.dumps(_cut(self.steps)), status, self.id))
                conn.commit()
            finally:
                conn.close()
        except Exception as ex:
            log(f"[RUN-HISTORY] Could not save run {self.id} ({str(ex)[:150]})")

    @contextlib.contextmanager
    def step(self, name, input=None):
        """One step. Set st['output'] inside the block. An error marks the step Failed and goes on up."""
        st = {"name": name, "status": "running", "input": input, "output": None}
        self.steps.append(st)
        t = time.time()
        try:
            yield st
            st["status"] = "success"
        except Exception as ex:
            st["status"], st["error"] = "failed", str(ex)[:500]
            raise
        finally:
            st["seconds"] = round(time.time() - t, 2)
            log(f"[RUN-HISTORY] Run {self.id} step '{name}': {st['status']} ({st['seconds']}s)")
            self._save()

    def note(self, name, output=None, status="success"):
        """A step without work of its own (e.g. 'already on the website -> asked the admin')."""
        self.steps.append({"name": name, "status": status, "seconds": 0, "input": None, "output": output})
        self._save()

    def finish(self, status, error=None):
        """status: success / failed / stopped."""
        self._save(status, error and str(error)[:500])
        log(f"[RUN-HISTORY] Run {self.id} {status} after {round(time.time() - self.t0, 1)}s"
            + (f": {error}" if error else ""))


# ============================================================ SECTION 4: page HTML
HIST_HTML = """<!doctype html><html><head><title>Run history - Item Master</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#1c1b19;--card:#252420;--line:#3a3833;--text:#f2efe9;--muted:#a9a397;--gold:#d8b76e;--grey:#8b877f;
       --green:#8cc58c;--red:#e0806c;--orange:#e0a36c}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--text);font-family:Roboto,-apple-system,Segoe UI,Arial,sans-serif}
 .page{max-width:920px;margin:0 auto;padding:22px 18px 40px}
 h1{font-size:26px;margin:4px 0 18px}
 .topbar{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
 .who{color:var(--muted);font-size:14px} .who b{color:var(--text)} .who a{color:var(--gold)}
 {{ nav_css|safe }}
 .info{color:var(--muted);font-size:14px;margin:14px 2px} .info a{color:var(--gold)}
 details.run{background:var(--card);border:1px solid var(--line);border-radius:14px;margin-bottom:10px}
 details.run > summary{list-style:none;cursor:pointer;padding:12px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
 details.run > summary::-webkit-details-marker{display:none}
 details.run[open] > summary{border-bottom:1px solid var(--line)}
 .st{font-weight:600;font-size:14px;white-space:nowrap}
 .st.success{color:var(--green)} .st.failed{color:var(--red)} .st.running{color:var(--gold)} .st.stopped{color:var(--orange)}
 .what{font-weight:600} .prod{color:var(--muted);font-size:14px;flex:1 1 200px;min-width:0}
 .when{color:var(--grey);font-size:13px;font-family:Consolas,monospace;white-space:nowrap;margin-left:auto}
 .steps{padding:8px 16px 14px}
 .runerr{color:var(--red);font-size:14px;margin:4px 0 10px}
 details.step{border:1px solid var(--line);border-radius:10px;margin-top:8px}
 details.step > summary{list-style:none;cursor:pointer;padding:9px 12px;display:flex;gap:10px;align-items:center}
 details.step > summary::-webkit-details-marker{display:none}
 .n{color:var(--grey);font-size:13px;width:22px}
 .sname{flex:1}
 .secs{color:var(--grey);font-size:12px}
 .box{padding:4px 12px 12px}
 .lbl{color:var(--muted);font-size:12px;margin:8px 0 4px;text-transform:uppercase;letter-spacing:.04em}
 pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;margin:0;font-size:12.5px;
     white-space:pre-wrap;overflow-wrap:anywhere;max-height:360px;overflow:auto}
 .serr{color:var(--red);font-size:13px}
 .empty{background:var(--card);border:1px dashed var(--line);border-radius:16px;padding:28px 20px;text-align:center;color:var(--muted)}
 .pager{display:flex;justify-content:space-between;align-items:center;margin-top:16px;color:var(--muted)}
 .pbtn{background:#f2d675;color:#1c1b19;text-decoration:none;border-radius:12px;padding:10px 16px;font-weight:600}
 .pbtn.off{visibility:hidden}
</style></head><body><div class="page">
<div class="topbar"><h1>Run history</h1>
  {% if g.user %}<span class="who">Logged in as <b>{{ g.user }}</b> · <a href="/logout">Log out</a></span>{% endif %}</div>
{{ nav_html|safe }}
<div class="info">{{ total }} runs{% if fi %} for this product · <a href="/item-master/history">show all products</a>{% endif %}
  · newest first · click a run to see its steps · <a href="/item-master">&larr; Item Master</a></div>

{% set icons = {'success': '&#10003; Ran successfully', 'failed': '&#10007; Failed', 'running': '&#8635; Running',
                'stopped': '&#9208; Stopped'} %}
{% for r in runs %}
<details class="run" {% if r.id == open_run or (loop.first and fi) %}open{% endif %} id="run-{{ r.id }}">
  <summary>
    <span class="st {{ r.status }}">{{ icons.get(r.status, r.status)|safe }}</span>
    <span class="what">{{ actions.get(r.action, r.action) }}</span>
    <span class="prod">{{ r.product or '(no product name in Airtable)' }}</span>
    <span class="when">{{ r.started_at.strftime('%d %b %Y, %H:%M:%S') }} UTC · {{ r.started_by }}{% if r.secs is not none %} · {{ r.secs }}s{% endif %}</span>
  </summary>
  <div class="steps">
    {% if r.error %}<div class="runerr">{{ r.error }}</div>{% endif %}
    {% for s in r.steps or [] %}
    <details class="step" {% if s.status == 'failed' %}open{% endif %}>
      <summary><span class="n">{{ loop.index0 or '' }}</span><span class="sname">{{ s.name }}</span>
        <span class="secs">{{ s.seconds }}s</span><span class="st {{ s.status }}">{{ icons.get(s.status, s.status).split(' ')[0]|safe }} {{ s.status|capitalize }}</span></summary>
      <div class="box">
        {% if s.error %}<div class="serr">{{ s.error }}</div>{% endif %}
        {% if s.input is not none %}<div class="lbl">Input</div><pre>{{ s.input|tojson(indent=2) }}</pre>{% endif %}
        {% if s.output is not none %}<div class="lbl">Output</div><pre>{{ s.output|tojson(indent=2) }}</pre>{% endif %}
      </div>
    </details>
    {% endfor %}
  </div>
</details>
{% endfor %}
{% if not runs %}<div class="empty">No runs yet - click "Create product description" or "Create in website" on the Item Master.</div>{% endif %}
{% if pages > 1 %}
<div class="pager">
  <a class="pbtn {{ '' if page > 1 else 'off' }}" href="?fi={{ fi }}&page={{ page - 1 }}">&larr; Newer</a>
  <span>Page {{ page }} of {{ pages }}</span>
  <a class="pbtn {{ '' if page < pages else 'off' }}" href="?fi={{ fi }}&page={{ page + 1 }}">Older &rarr;</a>
</div>
{% endif %}
</div></body></html>"""


HIDDEN_KEYS = {"record_id", "airtable_record_id", "fi_record_id"}   # Airtable record id: saved, NOT shown on the page


def hide_ids(v):
    """Copy of a step value without the Airtable record id (it stays in the database)."""
    if isinstance(v, dict):
        return {k: hide_ids(x) for k, x in v.items() if k not in HIDDEN_KEYS}
    if isinstance(v, list):
        return [hide_ids(x) for x in v]
    return v


# ============================================================ SECTION 5: route
@history_bp.route("/item-master/history")
def history_page():
    """Runs of the Item Master buttons, newest first (all, one product ?fi=, or one opened ?run=)."""
    fi = request.args.get("fi", "").strip()
    try:
        open_run = int(request.args.get("run", "0"))
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        open_run, page = 0, 1
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        counts = competitors_page.tab_counts(cur)
        where, params = ("WHERE fi_record_id = %s", [fi]) if fi else ("", [])
        if open_run and not fi:                       # one run opened: show the page it is on
            cur.execute("SELECT COUNT(*) AS n FROM button_runs WHERE id > %s", (open_run,))
            page = cur.fetchone()["n"] // PER_PAGE + 1
        cur.execute(f"SELECT COUNT(*) AS n FROM button_runs {where}", params)
        total = cur.fetchone()["n"]
        cur.execute(f"SELECT * FROM button_runs {where} ORDER BY id DESC LIMIT %s OFFSET %s",
                    params + [PER_PAGE, (page - 1) * PER_PAGE])
        runs = cur.fetchall()
    finally:
        conn.close()
    for r in runs:
        r["steps"] = hide_ids(r["steps"] or [])
        r["secs"] = round((r["finished_at"] - r["started_at"]).total_seconds(), 1) if r["finished_at"] else None
    pages = max(1, -(-total // PER_PAGE))
    log(f"[RUN-HISTORY] Page shown to {g.get('user')}: {total} runs" + (f" for {fi}" if fi else "")
        + (f", run {open_run} opened" if open_run else ""))
    return render_template_string(HIST_HTML, nav_html=competitors_page.render_nav("item", "im-history", counts),
                                  nav_css=competitors_page.NAV_CSS, runs=runs, total=total, fi=fi, open_run=open_run,
                                  page=page, pages=pages, actions=ACTIONS, now=datetime.utcnow())


# ============================================================ SECTION 6: hooks (called once from app.py)
def init(connect):
    """connect() -> new Postgres connection (app.pg_conn)."""
    _hooks["connect"] = connect
    log("[RUN-HISTORY] Linked to app.py: Item Master button runs -> table button_runs, page /item-master/history")

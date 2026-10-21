"""
"Create a new item"  (menu Item Master -> Create a new item -> /item-master/new)
================================================================================
EMPTY FOR NOW: the menu option and the page exist, the content (form) is added later.

Where things are (change them here):
  SECTION 1  page HTML     -> NEW_HTML (put the form here later)
  SECTION 2  route         -> GET /item-master/new
  SECTION 3  hooks         -> init(connect)
"""

import psycopg2.extras
from flask import Blueprint, render_template_string, g

import competitors_page                     # menu bar (render_nav, NAV_CSS) + menu counts

new_item_bp = Blueprint("item_create", __name__)
_hooks = {}                                 # filled by init() (SECTION 3)


def log(*a):
    """Print to the Render log (lines start with [NEW-ITEM])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: page HTML
NEW_HTML = """<!doctype html><html><head><title>Create a new item - Item Master</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#1c1b19;--card:#252420;--line:#3a3833;--text:#f2efe9;--muted:#a9a397;--gold:#d8b76e}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--text);font-family:Roboto,-apple-system,Segoe UI,Arial,sans-serif}
 .page{max-width:920px;margin:0 auto;padding:22px 18px 40px}
 h1{font-size:26px;margin:4px 0 18px}
 .topbar{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
 .who{color:var(--muted);font-size:14px} .who b{color:var(--text)} .who a{color:var(--gold)}
 {{ nav_css|safe }}
 .empty{background:var(--card);border:1px dashed var(--line);border-radius:16px;padding:28px 20px;text-align:center;
        color:var(--muted);margin-top:16px}
 .empty b{color:var(--text);font-size:18px}
</style></head><body><div class="page">
<div class="topbar"><h1>Create a new item</h1>
  {% if g.user %}<span class="who">Logged in as <b>{{ g.user }}</b> · <a href="/logout">Log out</a></span>{% endif %}</div>
{{ nav_html|safe }}
<div class="empty"><b>Create a new item</b><br>Coming soon - this page will get its content later.</div>
</div></body></html>"""


# ============================================================ SECTION 2: route
@new_item_bp.route("/item-master/new")
def new_item_page():
    """Empty page for now (only the menu option exists)."""
    counts = {}
    try:
        conn = _hooks["connect"]()
        try:
            counts = competitors_page.tab_counts(conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor))
        finally:
            conn.close()
    except Exception as ex:                  # menu numbers are optional - the page still opens
        log(f"[NEW-ITEM] Menu counts not loaded ({str(ex)[:120]})")
    log(f"[NEW-ITEM] Page opened by {g.get('user')} (empty for now)")
    return render_template_string(NEW_HTML, nav_html=competitors_page.render_nav("item", "im-new", counts),
                                  nav_css=competitors_page.NAV_CSS)


# ============================================================ SECTION 3: hooks (called once from app.py)
def init(connect):
    """connect() -> new Postgres connection (app.pg_conn)."""
    _hooks["connect"] = connect
    log("[NEW-ITEM] Linked to app.py: menu Item Master -> Create a new item (/item-master/new, empty for now)")

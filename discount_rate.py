"""
Auto discount rate  (Settings -> Auto discount rate,  page /settings/discount)
==============================================================================
Separate module for ONE setting: how many % below the cheapest IN-STOCK competitor the suggested price is set.

    Suggested price = cheapest in-stock competitor price - X%        (X = this rate, e.g. 3)
    Example: cheapest competitor AED 1620, X = 3  ->  1620 - 48.60 = 1571.40  ->  AED 1571

Where things are (change them here):
  SECTION 1  settings          -> MIN_RATE / MAX_RATE (allowed values), SETTING_KEY (name in the database)
  SECTION 2  database          -> table app_settings (one row: key 'discount_rate', value '3')
  SECTION 3  read the rate     -> get_discount_rate(cur)  - used by app.py for every suggested price
  SECTION 4  page box (HTML)   -> DISCOUNT_HTML + render_box(cur)  - shown inside the Settings page
  SECTION 5  save (route)      -> POST /settings/discount  - checks the number, saves it, recalculates prices
  SECTION 6  recalc hook       -> ON_DISCOUNT_CHANGE - app.py plugs in "recalculate all suggested prices"

Used by:
  competitors_page.py  -> Settings page shows render_box(cur) when /settings/discount is opened
  app.py               -> registers discount_bp, reads get_discount_rate(cur), sets ON_DISCOUNT_CHANGE

Env var: FF_DATABASE_URL = Postgres URL ending in /french_fragrance_db (same as the other pages)
"""

import os

import psycopg2
import psycopg2.extras
from flask import Blueprint, request, render_template_string, g, redirect

discount_bp = Blueprint("discount_rate", __name__)


# ============================================================ SECTION 1: settings (change limits here)
SETTING_KEY = "discount_rate"      # row name in table app_settings
MIN_RATE = 0                       # lowest allowed rate in %  (0 = suggested price = cheapest competitor)
MAX_RATE = 50                      # highest allowed rate in %
PAGE_URL = "/settings/discount"    # where the page is (the Settings menu links here)


def log(*a):
    """Print to the Render log (every line of this module starts with [DISCOUNT])."""
    print(*a, flush=True)


# ============================================================ SECTION 2: database (table app_settings)
# Same table as the other settings (daily update, brands per day ...). Created the first time if missing.
SETTINGS_CREATE = """
CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,             -- e.g. 'discount_rate'
    value       TEXT,                         -- e.g. '3'  (percent)
    updated_by  TEXT,                         -- user who saved it
    updated_at  TIMESTAMP DEFAULT NOW()
);
"""


def db_conn():
    """Connection to french_fragrance_db (FF_DATABASE_URL on Render)."""
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    log("[DISCOUNT] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ============================================================ SECTION 3: read the rate (used by app.py)
def get_discount_rate(cur):
    """Auto discount rate in percent (e.g. 2.5), or None when the admin never saved one.
    None -> app.py does NOT calculate suggested prices (nothing is guessed).
    Works with any cursor (app.py uses plain tuples, the pages use dict rows)."""
    log(f"[DISCOUNT] get_discount_rate(): reading '{SETTING_KEY}' from app_settings")
    cur.execute(SETTINGS_CREATE)
    cur.execute("SELECT value FROM app_settings WHERE key = %s", (SETTING_KEY,))
    row = cur.fetchone()
    value = (row["value"] if isinstance(row, dict) else row[0]) if row else None
    try:
        rate = float(value) if value not in (None, "") else None
    except ValueError:
        log(f"[DISCOUNT] Saved value {value!r} is not a number -> treated as NOT SET")
        rate = None
    log(f"[DISCOUNT] Using Auto discount rate: {rate if rate is not None else 'NOT SET'}")
    return rate


def rate_details(cur):
    """The saved row {value, updated_by, updated_at} (for 'Last changed by ... on ...'), or None."""
    cur.execute(SETTINGS_CREATE)
    cur.execute("SELECT value, updated_by, updated_at FROM app_settings WHERE key = %s", (SETTING_KEY,))
    row = cur.fetchone()
    if row is not None and not isinstance(row, dict):            # plain cursor -> make it a dict
        row = {"value": row[0], "updated_by": row[1], "updated_at": row[2]}
    log(f"[DISCOUNT] Saved row: {dict(row) if row else 'not saved yet'}")
    return row


# ============================================================ SECTION 4: page box (HTML shown in Settings)
# Only the box: the page around it (menu bar, title, colours) comes from competitors_page.py SETTINGS_HTML.
# The CSS classes (dform, drow, pct, hint, ok, err, note, back) are defined there too.
DISCOUNT_HTML = """
<a class="back" href="/settings">&larr; All settings</a>
{# ---- Auto discount rate: admin writes the % (e.g. 2 or 5). Saved in table app_settings, key 'discount_rate'. ---- #}
<form class="dform" method="post" action="{{ page_url }}"
      onsubmit="console.log('[DISCOUNT] Save clicked: ' + this.rate.value + '%')">
  {# -- input: the rate in % -- #}
  <label for="rate">Discount below the cheapest in-stock competitor</label>
  <div class="drow">
    <input id="rate" name="rate" type="number" min="{{ min_rate }}" max="{{ max_rate }}" step="0.1" required
           value="{{ '%g' % rate if rate is not none else '' }}" placeholder="e.g. 5">
    <span class="pct">%</span>
    <button type="submit">Save</button>
  </div>
  {# -- example with the saved rate -- #}
  {% if rate is not none %}<p class="hint">Example: cheapest competitor AED 100 &rarr; with {{ '%g' % rate }}% the suggested price is
     AED {{ '%g' % (100 - rate) }}.</p>{% endif %}
  {# -- messages after Save -- #}
  {% if saved %}<p class="ok">Saved: {{ '%g' % rate }}%.</p>{% endif %}
  {% if error %}<p class="err">{{ error }}</p>{% endif %}
  {% if info %}<p class="hint">Last changed by <b>{{ info.updated_by or '-' }}</b>{% if info.updated_at %} on {{ info.updated_at.strftime('%d %b %Y, %H:%M') }}{% endif %}.</p>{% endif %}
  {% if rate is none %}<p class="err">No rate saved yet: suggested prices are not being calculated. Write a rate and Save.</p>{% endif %}
  <script>console.log("[DISCOUNT] Page: saved rate = {{ rate if rate is not none else 'NOT SET' }}{% if saved %}, just saved{% endif %}{% if recalc %}, recalc = {{ recalc }}{% endif %}");</script>
  {# -- recalculation status (after Save) -- #}
  {% if recalc == '1' %}<p class="ok">All suggested prices are being recalculated with the new rate (about 1 minute).
     Automatic brands are then updated in Airtable.</p>{% elif recalc == 'busy' %}<p class="hint">Another run is in progress -
     it will use the new rate when it reaches the price step.</p>{% endif %}
  {# -- explanation -- #}
  <p class="note">This rate is used for every suggested price: on the competitor table, for Update price / Update all,
     and for the automatic brands. Saving recalculates all suggested prices.</p>
</form>
"""


def render_box(cur):
    """HTML of the discount box for the Settings page (competitors_page.py puts it inside SETTINGS_HTML)."""
    info = rate_details(cur)
    try:
        rate = float(info["value"]) if info and info["value"] not in (None, "") else None
    except ValueError:
        rate = None
    log(f"[DISCOUNT] Page opened: saved rate = {rate if rate is not None else 'NOT SET - suggested prices are not calculated'}")
    return render_template_string(DISCOUNT_HTML, rate=rate, info=info, page_url=PAGE_URL,
                                  min_rate=MIN_RATE, max_rate=MAX_RATE,
                                  saved=request.args.get("saved") == "1", error=request.args.get("error"),
                                  recalc=request.args.get("recalc"))


# ============================================================ SECTION 5: save (POST /settings/discount)
@discount_bp.route(PAGE_URL, methods=["POST"])
def discount_save():
    """Save the Auto discount rate written by the admin (MIN_RATE - MAX_RATE %), then recalculate all prices."""
    raw = (request.form.get("rate") or "").strip().replace("%", "")
    user = g.get("user") or "unknown"
    log(f"[DISCOUNT] {user} wants to save discount rate {raw!r}")
    # 5a. check the number
    try:
        rate = round(float(raw), 2)
    except ValueError:
        log("[DISCOUNT] Not a number -> not saved")
        return redirect(f"{PAGE_URL}?error=Please+write+a+number,+e.g.+5")
    if not MIN_RATE <= rate <= MAX_RATE:
        log(f"[DISCOUNT] {rate} is outside {MIN_RATE}-{MAX_RATE} -> not saved")
        return redirect(f"{PAGE_URL}?error=The+rate+must+be+between+{MIN_RATE}+and+{MAX_RATE}")
    # 5b. save it (insert the first time, update after that)
    conn = db_conn()
    cur = conn.cursor()
    try:
        cur.execute(SETTINGS_CREATE)
        cur.execute("""INSERT INTO app_settings (key, value, updated_by) VALUES (%s, %s, %s)
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                                                       updated_at = NOW()""", (SETTING_KEY, str(rate), user))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    log(f"[DISCOUNT] Saved discount rate {rate}% by {user}")
    # 5c. recalculate every suggested price now (function given by app.py, SECTION 6)
    if not ON_DISCOUNT_CHANGE:
        log("[DISCOUNT] No recalculation hook set (app.py not loaded?) -> prices update at the next run")
    started = ON_DISCOUNT_CHANGE() if ON_DISCOUNT_CHANGE else False
    log(f"[DISCOUNT] Recalculation {'started now' if started else 'not started now (another run busy)'}")
    return redirect(f"{PAGE_URL}?saved=1&recalc=" + ("1" if started else "busy"))


# ============================================================ SECTION 6: recalculation hook (set by app.py)
# app.py sets this to its "recalculate all suggested prices" function:
#     discount_rate.ON_DISCOUNT_CHANGE = recalc_after_discount_change
# It must return True when the recalculation started, False when another run is busy.
ON_DISCOUNT_CHANGE = None
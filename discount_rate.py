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
  SECTION 7  BRAND discount    -> table brand_discount: chosen brands get their OWN % (e.g. Afnan 5%), all other
                                  brands keep the common rate above (2026-11-06, user: "if afnan brand need 5% and
                                  other all 2%"). brand_rates(cur), BRAND_HTML, POST /settings/discount/brands
                                  (+ /remove). Used by app.py SUGGEST_SQL, price_triggers.recalc_product and
                                  db_triggers TRIGGER B (they read the table themselves: key = LOWER(TRIM(brand))).

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
    cur.execute(BRAND_CREATE)                                    # brand rates (SECTION 7) read by SUGGEST_SQL too
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
    html = render_template_string(DISCOUNT_HTML, rate=rate, info=info, page_url=PAGE_URL,
                                  min_rate=MIN_RATE, max_rate=MAX_RATE,
                                  saved=request.args.get("saved") == "1", error=request.args.get("error"),
                                  recalc=request.args.get("recalc"))
    return html + render_brand_box(cur, rate)                     # SECTION 7: brands with their own %


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


# ============================================================ SECTION 7: BRAND discount (own % for chosen brands)
# Table brand_discount: one row per brand that has its OWN rate. Brands not in it use the common rate (SECTION 3).
# Key = LOWER(TRIM(brand)) so "Afnan", "AFNAN " are the same brand. The SQL in app.py / price_triggers.py /
# db_triggers.py reads this table directly (COALESCE(brand rate, common rate)).
BRAND_CREATE = """
CREATE TABLE IF NOT EXISTS brand_discount (
    brand_key   TEXT PRIMARY KEY,             -- LOWER(TRIM(brand)), e.g. 'afnan'
    brand       TEXT,                         -- name as shown, e.g. 'Afnan'
    rate        NUMERIC(5,2) NOT NULL,        -- percent below the cheapest in-stock competitor, e.g. 5
    updated_by  TEXT,
    updated_at  TIMESTAMP DEFAULT NOW()
);
"""
BRAND_URL = PAGE_URL + "/brands"


def brand_rates(cur):
    """[{brand_key, brand, rate, updated_by, updated_at}] sorted by rate, brand."""
    cur.execute(BRAND_CREATE)
    cur.execute("SELECT brand_key, brand, rate, updated_by, updated_at FROM brand_discount ORDER BY rate, brand")
    rows = cur.fetchall()
    cols = ("brand_key", "brand", "rate", "updated_by", "updated_at")
    return [dict(r) if isinstance(r, dict) else dict(zip(cols, r)) for r in rows]


BRAND_HTML = """
{# ---- Brand discount: chosen brands get their own % (brand_discount table). Others keep the common rate. ---- #}
<form class="dform" method="post" action="{{ brand_url }}" id="bdform"
      onsubmit="if (event.submitter && event.submitter.name === 'brand_key') return true; var n=this.querySelectorAll('input[name=brand]:checked').length; console.log('[DISCOUNT-BRAND] Save: ' + this.rate.value + '% for ' + n + ' brands'); if(!n){alert('Tick at least one brand'); return false;}">
  <label for="brate">Brand discount (own % for chosen brands)</label>
  <div class="drow">
    <input id="brate" name="rate" type="number" min="{{ min_rate }}" max="{{ max_rate }}" step="0.1" required placeholder="e.g. 5">
    <span class="pct">%</span>
    <button type="submit">Save for ticked brands</button>
  </div>
  <p class="hint">All other brands use the common rate{% if rate is not none %} ({{ '%g' % rate }}%){% endif %}.
     Example: Afnan 5% &rarr; cheapest competitor AED 100 &rarr; suggested AED 95.</p>
  {% if bsaved %}<p class="ok">{{ bsaved }}</p>{% endif %}
  {% if berror %}<p class="err">{{ berror }}</p>{% endif %}
  {# -- brands that already have their own % -- #}
  {% if rows %}
  <table class="bdtable">
    <tr><th>Brand</th><th>Own %</th><th>Changed by</th><th></th></tr>
    {% for r in rows %}
    <tr><td><b>{{ r.brand }}</b></td><td>{{ '%g' % r.rate }}%</td>
        <td class="muted">{{ r.updated_by or '-' }}{% if r.updated_at %}, {{ r.updated_at.strftime('%d %b %Y') }}{% endif %}</td>
        <td><button type="submit" class="bdremove" formaction="{{ brand_url }}/remove" formnovalidate
                    name="brand_key" value="{{ r.brand_key }}"
                    onclick="return confirm('Remove the own % of {{ r.brand }}? It will use the common rate again.')">Remove</button></td></tr>
    {% endfor %}
  </table>
  {% else %}<p class="hint">No brand has its own % yet.</p>{% endif %}
  {# -- pick brands (search + tick) -- #}
  <input type="search" class="bdsearch" placeholder="Search brands..." oninput="bdFilter(this.value)">
  <div class="bdlist">
    {% for b in brands %}
    <label class="bdbrand" data-b="{{ b.brand|lower }}"><input type="checkbox" name="brand" value="{{ b.brand }}">
      {{ b.brand }} <span class="muted">({{ b.n }}){% if b.key in own %} &middot; now {{ '%g' % own[b.key] }}%{% endif %}</span></label>
    {% endfor %}
  </div>
  <p class="note">Saving or removing recalculates all suggested prices (automatic brands follow).</p>
</form>
<style>
 .bdtable{width:100%;border-collapse:collapse;margin:10px 0 14px}
 .bdtable th,.bdtable td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line)}
 .bdtable .muted,.bdbrand .muted{color:var(--muted);font-size:13px}
 .bdremove{background:none !important;border:1px solid var(--line) !important;color:var(--muted) !important;
           padding:4px 10px !important;border-radius:8px;font-size:13px !important;cursor:pointer}
 .bdsearch{width:100%;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:10px;
           padding:10px 12px;font-size:15px;margin:6px 0 10px;box-sizing:border-box}
 .bdlist{max-height:320px;overflow:auto;border:1px solid var(--line);border-radius:10px;padding:6px 10px}
 .bdbrand{display:block;font-weight:400 !important;margin:0 !important;padding:6px 0;cursor:pointer}
 .bdbrand[hidden]{display:none}
</style>
<script>
  function bdFilter(q) {                     // brand search box: show only brands containing the typed text
    q = (q || "").toLowerCase().trim();
    document.querySelectorAll(".bdbrand").forEach(function (l) { l.hidden = q && l.dataset.b.indexOf(q) < 0; });
  }
  console.log("[DISCOUNT-BRAND] {{ rows|length }} brands with their own %");
</script>
"""


def render_brand_box(cur, common_rate):
    """HTML of the Brand discount box (under the common rate box)."""
    rows = brand_rates(cur)
    # one line per brand whatever the capitals ("armaf" + "Armaf" = one brand, same key as the price SQL)
    cur.execute("SELECT MAX(TRIM(brand)) AS brand, COUNT(*) AS n FROM competitor_table "
                "WHERE brand IS NOT NULL AND TRIM(brand) <> '' GROUP BY LOWER(TRIM(brand)) ORDER BY LOWER(TRIM(brand))")
    brands = []
    for r in cur.fetchall():
        b, n = (r["brand"], r["n"]) if isinstance(r, dict) else r
        brands.append({"brand": b, "n": n, "key": b.lower().strip()})
    own = {r["brand_key"]: float(r["rate"]) for r in rows}
    log(f"[DISCOUNT-BRAND] Page: {len(rows)} brands with own %, {len(brands)} brands to pick from")
    return render_template_string(BRAND_HTML, rows=rows, brands=brands, own=own, rate=common_rate,
                                  brand_url=BRAND_URL, min_rate=MIN_RATE, max_rate=MAX_RATE,
                                  bsaved=request.args.get("bsaved"), berror=request.args.get("berror"))


def _brand_done(msg=None, error=None):
    """Recalculate all suggested prices, then back to the page with a message."""
    from urllib.parse import quote
    if error:
        return redirect(f"{PAGE_URL}?berror={quote(error)}")
    started = ON_DISCOUNT_CHANGE() if ON_DISCOUNT_CHANGE else False
    log(f"[DISCOUNT-BRAND] Recalculation {'started now' if started else 'not started now (another run busy)'}")
    msg += (" All suggested prices are being recalculated (about 1 minute)." if started
            else " Another run is busy - it will use the new % when it reaches the price step.")
    return redirect(f"{PAGE_URL}?bsaved={quote(msg)}")


@discount_bp.route(BRAND_URL, methods=["POST"])
def brand_discount_save():
    """Save one % for every ticked brand (insert or change)."""
    raw = (request.form.get("rate") or "").strip().replace("%", "")
    picked = [b.strip() for b in request.form.getlist("brand") if b.strip()]
    user = g.get("user") or "unknown"
    log(f"[DISCOUNT-BRAND] {user} wants {raw!r}% for {picked}")
    try:
        rate = round(float(raw), 2)
    except ValueError:
        return _brand_done(error="Please write a number, e.g. 5")
    if not MIN_RATE <= rate <= MAX_RATE:
        return _brand_done(error=f"The rate must be between {MIN_RATE} and {MAX_RATE}")
    if not picked:
        return _brand_done(error="Tick at least one brand")
    conn = db_conn()
    cur = conn.cursor()
    try:
        cur.execute(BRAND_CREATE)
        for b in picked:
            cur.execute("""INSERT INTO brand_discount (brand_key, brand, rate, updated_by) VALUES (%s, %s, %s, %s)
                           ON CONFLICT (brand_key) DO UPDATE SET brand = EXCLUDED.brand, rate = EXCLUDED.rate,
                               updated_by = EXCLUDED.updated_by, updated_at = NOW()""",
                        (b.lower(), b, rate, user))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    log(f"[DISCOUNT-BRAND] Saved {rate}% for {len(picked)} brands by {user}: {picked}")
    return _brand_done(f"Saved: {', '.join(picked)} = {rate:g}%.")


@discount_bp.route(BRAND_URL + "/remove", methods=["POST"])
def brand_discount_remove():
    """Remove a brand's own % -> it uses the common rate again."""
    key = (request.form.get("brand_key") or "").strip()
    user = g.get("user") or "unknown"
    conn = db_conn()
    cur = conn.cursor()
    try:
        cur.execute(BRAND_CREATE)
        cur.execute("DELETE FROM brand_discount WHERE brand_key = %s RETURNING brand", (key,))
        row = cur.fetchone()
        conn.commit()
    finally:
        cur.close()
        conn.close()
    if not row:
        return _brand_done(error="That brand had no own % any more")
    log(f"[DISCOUNT-BRAND] {user} removed the own % of {row[0]} -> common rate again")
    return _brand_done(f"Removed: {row[0]} uses the common rate again.")

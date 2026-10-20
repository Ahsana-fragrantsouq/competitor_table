"""
Item Master  (menu: Competitor table | Competitors | Settings | Item Master  ->  /item-master)
=============================================================================================
A full copy of every Airtable French Inventories product in the database table item_master.

What is saved (one row per Airtable record, key = fi_record_id = the Airtable record id):
  sku, brand, product_type, perfume_name, product_name, type, category, size, barcode,
  top_notes, middle_notes, base_notes, shopify_description, item_id,
  uae_price, asia_price, america_price, kuwait_price, qatar_price, uae_comparison_price, stock_qty
  + airtable_fields  = EVERY field of the record exactly as Airtable gave it (JSON) -> nothing is ever lost
The page HIDES the prices, stock qty and UAE comparison price; they are only in the database:
  python sql.py "SELECT sku, uae_price, asia_price, america_price, kuwait_price, qatar_price,
                 uae_comparison_price, stock_qty FROM item_master WHERE sku = 'AMF1085'"

When it is loaded from Airtable:
  - automatically once a day (background thread, AUTO_EVERY_HOURS; Render env ITEM_MASTER_AUTO=off switches it off)
  - admin button "Load from Airtable now" on the page
  - /item-master/load?secret=XXX   (progress: /item-master/status)
Products deleted in Airtable are deleted from item_master at the next full load.

Airtable field names: FIELDS below lists, per column, the Airtable names that are tried (upper / lower case and
spaces do not matter). Fields NOT found are listed on the page and in the log [ITEM-MASTER] -> add the real
name to that column's list.

Where things are (change them here):
  SECTION 1  settings      -> FIELDS (column, type, Airtable names, shown on page?), PER_PAGE, AUTO_EVERY_HOURS
  SECTION 2  table         -> IM_CREATE (item_master)
  SECTION 3  Airtable -> values (text / number / brand link)
  SECTION 4  load          -> load_all(): Airtable -> item_master, summary saved in app_settings 'item_master'
  SECTION 5  daily thread  -> auto_loader()
  SECTION 6  page HTML     -> IM_HTML
  SECTION 7  routes        -> /item-master, /item-master/reload (admin), /item-master/load, /item-master/status
  SECTION 8  hooks         -> init(): Airtable reader + database connection from app.py
"""

import os
import re
import json
import time
import html
import math
import threading
from datetime import datetime, timedelta

import psycopg2.extras
from psycopg2.extras import execute_values
from flask import Blueprint, request, jsonify, render_template_string, redirect, g

import competitors_page                     # menu bar (render_nav, NAV_CSS) + menu counts
from auth import is_admin

item_master_bp = Blueprint("item_master", __name__)


def log(*a):
    """Print to the Render log (lines start with [ITEM-MASTER])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
# (column in item_master, SQL type, Airtable field names tried in this order, shown on the page?)
# Prices, stock qty and UAE comparison price are saved but NOT shown (shown=False).
FIELDS = [
    ("sku",                  "TEXT",          ["SKU"],                                                 True),
    ("brand",                "TEXT",          ["Brand"],                                               True),
    ("product_type",         "TEXT",          ["Product Type"],                                        True),
    ("perfume_name",         "TEXT",          ["Perfume Name"],                                        True),
    ("uae_price",            "NUMERIC(12,2)", ["UAE Price"],                                           False),
    ("asia_price",           "NUMERIC(12,2)", ["Asia Price"],                                          False),
    ("america_price",        "NUMERIC(12,2)", ["America Price", "USA Price", "US Price"],              False),
    ("kuwait_price",         "NUMERIC(12,2)", ["Kuwait Price"],                                        False),
    ("qatar_price",          "NUMERIC(12,2)", ["Qatar Price"],                                         False),
    ("uae_comparison_price", "NUMERIC(12,2)", ["UAE Comparison Price", "UAE Compare Price",
                                               "UAE Compare At Price", "UAE Compare at price"],        False),
    ("stock_qty",            "NUMERIC(12,2)", ["Qty given in shopify", "Shopify Qty"],                False),
    ("type",                 "TEXT",          ["Type"],                                                True),
    ("category",             "TEXT",          ["Category"],                                            True),
    ("size",                 "TEXT",          ["Size"],                                                True),
    ("barcode",              "TEXT",          ["Barcode"],                                             True),
    ("top_notes",            "TEXT",          ["Top Notes", "Top Note"],                               True),
    ("middle_notes",         "TEXT",          ["Middle Notes", "Middle Note", "Heart Notes"],          True),
    ("base_notes",           "TEXT",          ["Base Notes", "Base Note"],                             True),
    ("shopify_description",  "TEXT",          ["Shopify Description", "Description"],                  True),
    ("product_name",         "TEXT",          ["Product Name"],                                        True),
    ("item_id",              "TEXT",          ["Item ID"],                                             False),
]
NUMBER_COLS = {c for c, t, _, _ in FIELDS if t.startswith("NUMERIC")}
SHOWN = [c for c, _, _, shown in FIELDS if shown]              # page columns (+ Airtable record id at the end)
LABELS = {"sku": "SKU", "brand": "Brand", "product_type": "Product type", "perfume_name": "Perfume name",
          "type": "Type", "category": "Category", "size": "Size", "barcode": "Barcode", "top_notes": "Top notes",
          "middle_notes": "Middle notes", "base_notes": "Base notes", "shopify_description": "Shopify description",
          "product_name": "Product name"}
SEARCH_COLS = ["sku", "brand", "perfume_name", "product_name", "barcode", "item_id", "fi_record_id"]

PER_PAGE = 50
AUTO_EVERY_HOURS = 24                       # automatic load from Airtable when the last one is this old
AUTO_ON = os.environ.get("ITEM_MASTER_AUTO", "on").lower() != "off"
SETTING_KEY = "item_master"                 # app_settings row with the last load summary


# ============================================================ SECTION 2: table (created / new columns added automatically)
IM_CREATE = ("""
CREATE TABLE IF NOT EXISTS item_master (
    fi_record_id     TEXT PRIMARY KEY,          -- Airtable French Inventories record id (rec...)
    airtable_fields  JSONB,                     -- every Airtable field of this record, as Airtable gave it
    loaded_at        TIMESTAMP DEFAULT NOW()    -- when this row was last copied from Airtable
);
""" + "".join(f"ALTER TABLE item_master ADD COLUMN IF NOT EXISTS {c} {t};\n" for c, t, _, _ in FIELDS)
    + "CREATE INDEX IF NOT EXISTS item_master_sku ON item_master (sku);\n")

SETTINGS_CREATE = """
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY, value TEXT, updated_by TEXT, updated_at TIMESTAMP DEFAULT NOW()
);
"""


# ============================================================ SECTION 3: Airtable value -> text / number
def name_key(s):
    """'UAE Comparison Price' / 'uae_comparison price' -> 'uaecomparisonprice' (for finding field names)."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def is_record_id(v):
    """Airtable linked-record id, e.g. 'recAbC123...' (17 characters)."""
    return isinstance(v, str) and len(v) == 17 and v.startswith("rec")


def to_text(v):
    """Any Airtable value -> text. Lists (multi-select, lookups) are joined with ', '."""
    if v is None:
        return None
    if isinstance(v, list):
        parts = [to_text(x) for x in v]
        v = ", ".join(p for p in parts if p)
    elif isinstance(v, dict):                   # AI field / button / attachment -> its value, name or url
        v = v.get("value") or v.get("label") or v.get("name") or v.get("url") or json.dumps(v)
    v = str(v).strip()
    return v or None


def to_number(v):
    """Airtable number / currency / lookup / text like 'AED 1,250' -> number (None when not a number)."""
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, bool) or v in (None, ""):
        return None
    try:
        n = float(re.sub(r"[^0-9.\-]", "", v) if isinstance(v, str) else v)
    except (TypeError, ValueError):
        return None
    if math.isnan(n) or abs(n) >= 1e10:         # does not fit NUMERIC(12,2) -> typing mistake in Airtable
        return None
    return round(n, 2)


def pick_fields(seen_names):
    """Column -> real Airtable field name, using the names seen in the downloaded records.
    Returns (found {column: airtable name}, missing [column, ...])."""
    by_key = {name_key(n): n for n in seen_names}
    found, missing = {}, []
    for col, _, names, _ in FIELDS:
        real = next((by_key[name_key(n)] for n in names if name_key(n) in by_key), None)
        if real:
            found[col] = real
        else:
            missing.append(col)
    return found, missing


# ============================================================ SECTION 4: load (Airtable -> item_master)
_load_lock = threading.Lock()
_hooks = {}                                  # filled by init() from app.py (SECTION 8)


def save_summary(summary):
    """Last load result -> app_settings 'item_master' (shown on the page, survives a restart)."""
    conn = _hooks["connect"]()
    try:
        cur = conn.cursor()
        cur.execute(SETTINGS_CREATE)
        cur.execute("""INSERT INTO app_settings (key, value, updated_by, updated_at) VALUES (%s, %s, %s, NOW())
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                       updated_at = NOW()""", (SETTING_KEY, json.dumps(summary), summary.get("by")))
        conn.commit()
    finally:
        conn.close()


def read_summary(cur):
    """Last load result from app_settings, or {} when it never ran."""
    cur.execute(SETTINGS_CREATE)
    cur.execute("SELECT value FROM app_settings WHERE key = %s", (SETTING_KEY,))
    row = cur.fetchone()
    try:
        return json.loads((row["value"] if isinstance(row, dict) else row[0]) or "{}") if row else {}
    except ValueError:
        return {}


def load_all(by="auto"):
    """Copy EVERY French Inventories record (all fields) from Airtable into item_master.
    1. brand record id -> brand name (Brand is a linked field)
    2. all records, all fields (Airtable only sends fields that are not empty)
    3. find the real field name for each column (FIELDS), list the ones not found
    4. save 500 at a time (new = insert, existing = update); records gone from Airtable are deleted
    Returns the summary (also saved in app_settings 'item_master'). Only one load at a time."""
    if not _load_lock.acquire(blocking=False):
        log("[ITEM-MASTER] A load is already running -> not started again")
        return {"error": "already running"}
    started = time.time()
    summary = {"by": by, "started": datetime.utcnow().isoformat(timespec="seconds") + "Z", "running": True}
    try:
        h = _hooks
        log(f"[ITEM-MASTER] Load from Airtable started (by {by})")
        brand_names = {}
        for r in h["read_airtable"](h["brands_table"], {"fields[]": [h["brands_name_field"]]}):
            name = to_text(r.get("fields", {}).get(h["brands_name_field"]))
            if name:
                brand_names[r["id"]] = name
        log(f"[ITEM-MASTER] {len(brand_names)} brand names read")

        recs = h["read_airtable"](h["fi_table"], {})          # no fields[] -> every field
        log(f"[ITEM-MASTER] {len(recs)} French Inventories records downloaded")
        if not recs:
            raise RuntimeError("Airtable returned 0 records - nothing changed")

        seen = set()
        for rec in recs:
            seen.update(rec.get("fields", {}).keys())
        found, missing = pick_fields(seen)
        log(f"[ITEM-MASTER] Fields used: " + ", ".join(f"{c}<-'{n}'" for c, n in found.items()))
        if missing:
            log(f"[ITEM-MASTER] NOT FOUND in Airtable: {', '.join(missing)} -> add the real field name in "
                f"item_master.py FIELDS. Airtable field names seen: {', '.join(sorted(seen))}")

        cols = [c for c, _, _, _ in FIELDS]
        rows, linked_warned = [], set()
        for rec in recs:
            f = rec.get("fields", {})
            vals = []
            for col in cols:
                v = f.get(found[col]) if col in found else None
                if col == "brand" and isinstance(v, list) and v and is_record_id(v[0]):
                    v = [brand_names.get(b, b) for b in v]            # linked brand -> brand name
                elif isinstance(v, list) and v and is_record_id(v[0]) and col not in linked_warned:
                    linked_warned.add(col)
                    log(f"[ITEM-MASTER] '{found[col]}' is a linked field -> record ids saved, not names")
                vals.append(to_number(v) if col in NUMBER_COLS else to_text(v))
            rows.append((rec["id"], json.dumps(f), *vals))

        conn = h["connect"]()
        try:
            cur = conn.cursor()
            cur.execute(IM_CREATE)
            conn.commit()
            sql = (f"INSERT INTO item_master (fi_record_id, airtable_fields, {', '.join(cols)}, loaded_at) VALUES %s "
                   f"ON CONFLICT (fi_record_id) DO UPDATE SET airtable_fields = EXCLUDED.airtable_fields, "
                   + ", ".join(f"{c} = EXCLUDED.{c}" for c in cols) + ", loaded_at = NOW()")
            template = "(" + ", ".join(["%s"] * (len(cols) + 2)) + ", NOW())"
            for i in range(0, len(rows), 500):
                execute_values(cur, sql, rows[i:i + 500], template=template)
                conn.commit()
                log(f"[ITEM-MASTER] Saved {min(i + 500, len(rows))}/{len(rows)}")
            # products deleted in Airtable -> deleted here too (only after a full download)
            cur.execute("DELETE FROM item_master WHERE NOT (fi_record_id = ANY(%s))", ([r[0] for r in rows],))
            deleted = cur.rowcount
            conn.commit()
        finally:
            conn.close()

        summary.update(products=len(rows), deleted=deleted, missing=missing, found=found,
                       seconds=round(time.time() - started, 1))
        log(f"[ITEM-MASTER] Done: {len(rows)} products saved, {deleted} deleted (gone from Airtable), "
            f"{summary['seconds']}s")
    except Exception as ex:
        summary["error"] = str(ex)[:300]
        log(f"[ITEM-MASTER] ERROR: {summary['error']}")
    finally:
        summary["running"] = False
        summary["finished"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        try:
            save_summary(summary)
        except Exception as ex:
            log(f"[ITEM-MASTER] Could not save the summary: {ex}")
        _load_lock.release()
    return summary


def start_load(by):
    """Run load_all() in the background. False when a load is already running."""
    if _load_lock.locked():
        return False
    threading.Thread(target=load_all, args=(by,), daemon=True, name="item-master-load").start()
    return True


# ============================================================ SECTION 5: daily thread (automatic load)
def auto_loader(check_every=1800):
    """Every 30 min: when the last finished load is AUTO_EVERY_HOURS old (or never ran) -> load_all()."""
    time.sleep(120)                                    # let the website start first
    log(f"[ITEM-MASTER] Automatic load on: every {AUTO_EVERY_HOURS} hours")
    while True:
        try:
            conn = _hooks["connect"]()
            try:
                last = read_summary(conn.cursor()).get("finished")
            finally:
                conn.close()
            age = (datetime.utcnow() - datetime.fromisoformat(last.rstrip("Z"))) if last else None
            if age is None or age >= timedelta(hours=AUTO_EVERY_HOURS):
                log(f"[ITEM-MASTER] Last load {last or 'never'} -> loading now")
                load_all("auto")
        except Exception as ex:                        # never stops the website; tries again later
            log(f"[ITEM-MASTER] Automatic load check failed: {str(ex)[:200]}")
        time.sleep(check_every)


# ============================================================ SECTION 6: page HTML
IM_HTML = """<!doctype html><html><head><title>Item Master - Competitors</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
{# Same look as the Competitor table cards (competitors_page.py SECTION 6a) #}
<style>
 :root{--bg:#1c1b19;--card:#252420;--line:#3a3833;--text:#f2efe9;--muted:#a9a397;--gold:#d8b76e;--grey:#8b877f}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--text);font-family:Roboto,-apple-system,Segoe UI,Arial,sans-serif}
 .page{max-width:920px;margin:0 auto;padding:22px 18px 40px}
 h1{font-size:26px;margin:4px 0 18px}
 .topbar{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
 .who{color:var(--muted);font-size:14px} .who b{color:var(--text)} .who a{color:var(--gold)}
 {{ nav_css|safe }}
 .bar{display:flex;gap:8px;margin:14px 0 6px;flex-wrap:wrap}
 .bar input{flex:1;min-width:0;background:var(--card);color:var(--text);border:1px solid var(--line);
            border-radius:12px;padding:13px 14px;font-size:16px}
 .bar button,.load button{background:#f2d675;color:#1c1b19;border:0;border-radius:12px;padding:0 18px;
            font-size:16px;font-weight:600;cursor:pointer;min-height:48px}
 .load button{min-height:0;padding:9px 16px;font-size:14px}
 .info{color:var(--muted);font-size:14px;margin:6px 2px 14px;display:flex;justify-content:space-between;
       align-items:center;gap:12px;flex-wrap:wrap}
 .info b{color:var(--text)} .warn{color:#e0a36c} .err{color:#e0806c} .ok{color:#8cc58c}
 /* ---- product card ---- */
 .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px;margin-bottom:12px}
 .title{font-size:18px;font-weight:600;margin-bottom:6px;line-height:1.35}
 .code{color:var(--muted);font-size:14px;font-family:Consolas,monospace}
 .meta{color:var(--muted);font-size:14px;margin-top:4px}
 .meta b{color:var(--text);font-weight:600}
 /* gold box: product type | type | category | size | perfume name (like the least-price box) */
 .facts{display:flex;flex-wrap:wrap;gap:10px 28px;margin:12px 0 2px;padding:10px 14px;border:1px solid #f2d675;border-radius:12px}
 .plabel{color:var(--muted);font-size:13px}
 .pval{font-size:17px;font-weight:600;margin-top:2px}
 .pval.gold{color:#f2d675}
 /* one section per line: notes, description (like one competitor shop) */
 .shop{border-top:1px solid var(--line);margin-top:12px;padding-top:10px}
 .notes{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
 .notes .pval{font-size:15px;font-weight:500;line-height:1.4}
 @media (max-width:640px){ .notes{grid-template-columns:1fr} }
 .desc{font-size:14px;line-height:1.5;margin-top:4px;overflow-wrap:anywhere}
 .desc[hidden]{display:none}
 .more{background:none;border:1px solid var(--line);color:var(--muted);border-radius:8px;padding:6px 12px;
       font-size:13px;cursor:pointer;margin-top:8px}
 .more:hover{color:var(--text);border-color:var(--muted)}
 .none{color:var(--grey)}
 .date{color:var(--grey);font-size:13px;margin-top:10px}
 .empty{background:var(--card);border:1px dashed var(--line);border-radius:16px;padding:28px 20px;text-align:center;color:var(--muted)}
 .pager{display:flex;justify-content:space-between;align-items:center;margin-top:16px;color:var(--muted)}
 .pbtn{background:#f2d675;color:#1c1b19;text-decoration:none;border-radius:12px;padding:12px 18px;font-weight:600}
 .pbtn.off{visibility:hidden}
</style></head><body><div class="page">
<div class="topbar"><h1>Item Master</h1>
  {% if g.user %}<span class="who">Logged in as <b>{{ g.user }}</b> · <a href="/logout">Log out</a></span>{% endif %}</div>
{{ nav_html|safe }}

<form class="bar" method="get" action="/item-master">
  <input name="q" value="{{ q }}" placeholder="Filter by SKU, brand, perfume name, product name, barcode">
  <button type="submit">Go</button>
</form>

<div class="info">
  <span><b>{{ total }}</b> products{% if q %} matching "{{ q }}"{% endif %}.
    {% if last.finished %}Last loaded from Airtable {{ last.finished|replace('T', ' ')|replace('Z', ' UTC') }}
      ({{ last.by }}){% elif not last.running %}Never loaded from Airtable yet.{% endif %}
    {% if running %}<span class="ok">Loading from Airtable now ... reload this page in a minute.</span>{% endif %}
    {% if last.error %}<span class="err">Last load failed: {{ last.error }}</span>{% endif %}
    {% if msg %}<span class="ok">{{ msg }}</span>{% endif %}</span>
  {% if admin %}
  <form class="load" method="post" action="/item-master/reload"><button type="submit">Load from Airtable now</button></form>
  {% endif %}
</div>
{% if last.missing %}<div class="info warn">Not found in Airtable (left empty): {{ last.missing|join(', ') }}
  - tell Claude the real Airtable field names.</div>{% endif %}

{# one card per product. Prices, stock qty and UAE comparison price are NOT on the page (database only) #}
{% for r in rows %}
<div class="card">
  <div class="title">{{ r.product_name or r.perfume_name or r.sku or r.fi_record_id }}</div>
  {% if r.item_id %}<div class="code">{{ r.item_id }}</div>{% endif %}
  <div class="meta">{% if r.brand %}Brand <b>{{ r.brand }}</b>{% endif %}{% if r.brand and r.sku %} · {% endif %}{% if r.sku %}SKU <b>{{ r.sku }}</b>{% endif %}{% if r.barcode and (r.brand or r.sku) %} · {% endif %}{% if r.barcode %}Barcode <b>{{ r.barcode }}</b>{% endif %}</div>

  <div class="facts">
    <div><div class="plabel">Perfume name</div><div class="pval gold">{{ r.perfume_name or '-' }}</div></div>
    <div><div class="plabel">Product type</div><div class="pval">{{ r.product_type or '-' }}</div></div>
    <div><div class="plabel">Type</div><div class="pval">{{ r.type or '-' }}</div></div>
    <div><div class="plabel">Category</div><div class="pval">{{ r.category or '-' }}</div></div>
    <div><div class="plabel">Size</div><div class="pval">{{ r.size or '-' }}</div></div>
  </div>

  <div class="shop notes">
    <div><div class="plabel">Top notes</div><div class="pval">{{ r.top_notes or '' }}{% if not r.top_notes %}<span class="none">-</span>{% endif %}</div></div>
    <div><div class="plabel">Middle notes</div><div class="pval">{{ r.middle_notes or '' }}{% if not r.middle_notes %}<span class="none">-</span>{% endif %}</div></div>
    <div><div class="plabel">Base notes</div><div class="pval">{{ r.base_notes or '' }}{% if not r.base_notes %}<span class="none">-</span>{% endif %}</div></div>
  </div>

  <div class="shop">
    <div class="plabel">Shopify description</div>
    {% if r.desc_text %}
      {% if r.desc_text|length > 220 %}
      <div class="desc" id="ds-{{ loop.index }}">{{ r.desc_text[:220] }} ...</div>
      <div class="desc" id="df-{{ loop.index }}" hidden>{{ r.desc_text }}</div>
      <button type="button" class="more" onclick="toggleDesc({{ loop.index }}, this)">Show more</button>
      {% else %}
      <div class="desc">{{ r.desc_text }}</div>
      {% endif %}
    {% else %}<div class="desc none">No description</div>{% endif %}
  </div>

  <div class="date">Airtable record {{ r.fi_record_id }}{% if r.loaded_at %} · Loaded {{ r.loaded_at.strftime('%d %b, %H:%M') }}{% endif %}</div>
</div>
{% endfor %}
{% if not rows %}<div class="empty">No products{% if not total and not q %} - load from Airtable first{% endif %}.</div>{% endif %}

{% if pages > 1 %}
<div class="pager">
  <a class="pbtn {{ '' if page > 1 else 'off' }}" href="?q={{ q|urlencode }}&page={{ page - 1 }}">&larr; Previous</a>
  <span>Page {{ page }} of {{ pages }}</span>
  <a class="pbtn {{ '' if page < pages else 'off' }}" href="?q={{ q|urlencode }}&page={{ page + 1 }}">Next &rarr;</a>
</div>
{% endif %}
<script>
  // "Show more" / "Show less" on a long Shopify description
  function toggleDesc(i, btn) {
    var full = document.getElementById("df-" + i), short = document.getElementById("ds-" + i);
    full.hidden = !full.hidden; short.hidden = !full.hidden;
    btn.textContent = full.hidden ? "Show more" : "Show less";
    console.log("[ITEM-MASTER] Description " + i + (full.hidden ? " closed" : " opened"));
  }
</script>
</div></body></html>"""


def plain_text(s):
    """Shopify description HTML -> plain text for the page (the database keeps the original)."""
    if not s:
        return ""
    s = re.sub(r"<\s*(br|/p|/li|/div)[^>]*>", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


# ============================================================ SECTION 7: routes
_table_ready = False


def ensure_table(conn):
    """Create item_master / add new columns ONCE after each start (not on every page view: ALTER TABLE locks it)."""
    global _table_ready
    if not _table_ready:
        conn.cursor().execute(IM_CREATE)
        conn.commit()
        _table_ready = True
        log("[ITEM-MASTER] Table item_master ready")


@item_master_bp.route("/item-master")
def item_master_page():
    """The Item Master page: search + 50 per page. Prices / stock / comparison price are not selected at all."""
    q = request.args.get("q", "").strip()
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    rows, total, last, counts = [], 0, {}, {}
    conn = _hooks["connect"]()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        ensure_table(conn)
        counts = competitors_page.tab_counts(cur)          # numbers in the Competitors menu
        where, params = "", []
        if q:
            where = "WHERE " + " OR ".join(f"{c} ILIKE %s" for c in SEARCH_COLS)
            params = [f"%{q}%"] * len(SEARCH_COLS)
        cur.execute(f"SELECT COUNT(*) AS n FROM item_master {where}", params)
        total = cur.fetchone()["n"]
        cur.execute(f"SELECT fi_record_id, item_id, loaded_at, {', '.join(SHOWN)} FROM item_master {where} "
                    f"ORDER BY LOWER(brand), LOWER(product_name) LIMIT %s OFFSET %s",
                    params + [PER_PAGE, (page - 1) * PER_PAGE])
        rows = cur.fetchall()
        for r in rows:
            r["desc_text"] = plain_text(r.get("shopify_description"))
        last = read_summary(cur)
    finally:
        conn.close()
    pages = max(1, math.ceil(total / PER_PAGE))
    log(f"[ITEM-MASTER] Page shown to {g.get('user')}: search {q!r}, page {page}/{pages}, {total} products")
    return render_template_string(
        IM_HTML, nav_html=competitors_page.render_nav("item", None, counts), nav_css=competitors_page.NAV_CSS,
        rows=rows, total=total, q=q, page=page, pages=pages, shown=SHOWN, labels=LABELS, last=last,
        running=_load_lock.locked(), admin=is_admin(), msg=request.args.get("msg"))


@item_master_bp.post("/item-master/reload")
def item_master_reload():
    """Admin button 'Load from Airtable now' -> background load, back to the page."""
    if not is_admin():
        log(f"[ITEM-MASTER] Load refused: {g.get('user')} is not an admin")
        return jsonify({"error": "admins only"}), 403
    started = start_load(g.get("user") or "admin")
    log(f"[ITEM-MASTER] Load button by {g.get('user')}: {'started' if started else 'already running'}")
    msg = "Loading from Airtable started." if started else "A load is already running."
    return redirect("/item-master?msg=" + msg.replace(" ", "+"))


@item_master_bp.get("/item-master/load")
def item_master_load_url():
    """/item-master/load?secret=XXX -> background load (same as the admin button)."""
    secret = _hooks.get("run_secret")
    if not g.get("user") and (not secret or request.args.get("secret") != secret):
        return jsonify({"error": "unauthorized"}), 401
    started = start_load("url")
    return jsonify({"started": started, "check_status": "/item-master/status"}), 202 if started else 409


@item_master_bp.get("/item-master/status")
def item_master_status():
    """Last / current load result."""
    conn = _hooks["connect"]()
    try:
        last = read_summary(conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor))
    finally:
        conn.close()
    return jsonify({"running": _load_lock.locked(), "last": last})


# ============================================================ SECTION 8: hooks (called once from app.py)
def init(connect, read_airtable, fi_table, brands_table, brands_name_field, run_secret=""):
    """connect()                     -> new Postgres connection (app.pg_conn)
    read_airtable(table, params)  -> list of Airtable records (app.at_list, handles paging + 429)"""
    _hooks.update(connect=connect, read_airtable=read_airtable, fi_table=fi_table, brands_table=brands_table,
                  brands_name_field=brands_name_field, run_secret=run_secret)
    log(f"[ITEM-MASTER] Linked to app.py (table {fi_table} -> item_master)")
    if AUTO_ON:
        threading.Thread(target=auto_loader, daemon=True, name="item-master-auto").start()
    else:
        log("[ITEM-MASTER] Automatic load switched off (ITEM_MASTER_AUTO=off)")

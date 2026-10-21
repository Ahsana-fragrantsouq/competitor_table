"""
Item Master  (menu: Competitor table | Competitors | Settings | Item Master  ->  /item-master)
=============================================================================================
A full copy of every Airtable French Inventories product in the database table item_master.

What is saved (one row per Airtable record, key = fi_record_id = the Airtable record id):
  sku, brand, product_type, perfume_name, product_name, type, category, size, barcode,
  top_notes, middle_notes, base_notes, shopify_description,
  uae_price, asia_price, america_price, kuwait_price, qatar_price, uae_comparison_price, stock_qty
  shopify_id, image_urls (one link per line), gift_items (one per line), desc_created_at
  -> everything the Item Master buttons need is in OUR database, they never read Airtable
     (so Airtable can be removed later; "Load from Airtable" is then only an import)
  + airtable_fields  = EVERY field of the record exactly as Airtable gave it (JSON) -> nothing is ever lost
                       (except DROP_FIELDS, e.g. "Item ID": never saved anywhere)
The page HIDES the prices, stock qty, UAE comparison price, Shopify description and Airtable record id;
they are only in the database:
  python sql.py "SELECT sku, uae_price, asia_price, america_price, kuwait_price, qatar_price,
                 uae_comparison_price, stock_qty FROM item_master WHERE sku = 'AMF1085'"

When it is loaded from Airtable:
  (NOT automatic: only when someone starts it)
  - admin button "Load from Airtable now" on the page
  - /item-master/load?secret=XXX   (progress: /item-master/status)
Products deleted in Airtable are deleted from item_master at the next full load.
What our website created is never overwritten by a load: shopify_id (Create in website) and a description made by
Create product description (desc_created_at set) - see WEBSITE_COLS_SQL.

Airtable field names: FIELDS below lists, per column, the Airtable names that are tried (upper / lower case and
spaces do not matter). Fields NOT found are listed on the page and in the log [ITEM-MASTER] -> add the real
name to that column's list.

Where things are (change them here):
  SECTION 1  settings      -> FIELDS (column, type, Airtable names, shown on page?), PER_PAGE
  SECTION 2  table         -> IM_CREATE (item_master)
  SECTION 3  Airtable -> values (text / number / brand link)
  SECTION 4  load          -> load_all(): Airtable -> item_master, summary saved in app_settings 'item_master'
  SECTION 5  (removed: there is no automatic daily load)
  SECTION 6  page HTML     -> IM_HTML
  SECTION 7  routes        -> /item-master, /item-master/reload (admin), /item-master/load, /item-master/status
  SECTION 8  hooks         -> init(): Airtable reader + database connection from app.py
"""

import re
import json
import time
import math
import threading
from datetime import datetime

import psycopg2.extras
from psycopg2.extras import execute_values
from flask import Blueprint, request, jsonify, render_template_string, redirect, g

import competitors_page                     # menu bar (render_nav, NAV_CSS) + menu counts
import description_gen                      # 'Create product description' button (description_gen.py)
import shopify_create                       # 'Create in website' button (shopify_create.py)
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
    ("shopify_description",  "TEXT",          ["Shopify Description", "Description"],                  False),
    ("product_name",         "TEXT",          ["Product Name"],                                        True),
    # needed by the Item Master buttons, so they never have to read Airtable (later Airtable can be removed):
    ("shopify_id",           "TEXT",          ["ShopifyID"],                                           False),
    ("image_urls",           "TEXT",          ["Image URLs"],                                          False),
    ("gift_items",           "TEXT",          ["Items in Gift Set"],                                   False),
]
# Linked Airtable fields: record ids are turned into real values while loading (SECTION 4), one per line:
#   image_urls <- "Image URLs" table, field "Name" (= the image link)   gift_items <- the linked products' Product Name
LINKED_COLS = {"image_urls": ("Image URLs", "Name"), "gift_items": (None, "Product Name")}   # None = French Inventories
# Columns OUR WEBSITE writes. "Load from Airtable" never overwrites them once our website has set them:
#   shopify_id           set by 'Create in website'        -> Airtable value only used while ours is empty
#   shopify_description  set by 'Create product description' (desc_created_at filled) -> Airtable value ignored
WEBSITE_COLS_SQL = {
    "shopify_id": "COALESCE(NULLIF(item_master.shopify_id, ''), EXCLUDED.shopify_id)",
    "shopify_description": "CASE WHEN item_master.desc_created_at IS NOT NULL THEN item_master.shopify_description "
                           "ELSE EXCLUDED.shopify_description END",
}
# Airtable fields NOT saved at all (not as a column, not in airtable_fields). Old saved values are removed at start.
DROP_FIELDS = ["Item ID"]
NUMBER_COLS = {c for c, t, _, _ in FIELDS if t.startswith("NUMERIC")}
SHOWN = [c for c, _, _, shown in FIELDS if shown]              # page columns (Airtable record id is not shown)
LABELS = {"sku": "SKU", "brand": "Brand", "product_type": "Product type", "perfume_name": "Perfume name",
          "type": "Type", "category": "Category", "size": "Size", "barcode": "Barcode", "top_notes": "Top notes",
          "middle_notes": "Middle notes", "base_notes": "Base notes", "shopify_description": "Shopify description",
          "product_name": "Product name"}
SEARCH_COLS = ["sku", "brand", "perfume_name", "product_name", "barcode", "fi_record_id"]

PER_PAGE = 50
SETTING_KEY = "item_master"                 # app_settings row with the last load summary


# ============================================================ SECTION 2: table (created / new columns added automatically)
IM_CREATE = ("""
CREATE TABLE IF NOT EXISTS item_master (
    fi_record_id     TEXT PRIMARY KEY,          -- Airtable French Inventories record id (rec...)
    airtable_fields  JSONB,                     -- every Airtable field of this record, as Airtable gave it
    loaded_at        TIMESTAMP DEFAULT NOW()    -- when this row was last copied from Airtable
);
""" + "".join(f"ALTER TABLE item_master ADD COLUMN IF NOT EXISTS {c} {t};\n" for c, t, _, _ in FIELDS)
    + "CREATE INDEX IF NOT EXISTS item_master_sku ON item_master (sku);\n"
    # fields that are not kept any more (DROP_FIELDS): old column + old values inside airtable_fields removed
    + "ALTER TABLE item_master DROP COLUMN IF EXISTS item_id;\n"
    # when 'Create product description' (description_gen.py) wrote shopify_description -> Load keeps it
    + "ALTER TABLE item_master ADD COLUMN IF NOT EXISTS desc_created_at TIMESTAMP;\n"
    + "".join(f"UPDATE item_master SET airtable_fields = airtable_fields - '{n}' WHERE airtable_fields ? '{n}';\n"
              for n in DROP_FIELDS))

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

        # linked fields -> real values: {column: {record id: value}}
        linked = {}
        for col, (table, field) in LINKED_COLS.items():
            if col not in found:
                continue
            if table is None:                                  # links to other French Inventories products
                linked[col] = {r["id"]: to_text(r.get("fields", {}).get(field)) for r in recs}
            else:
                try:
                    linked[col] = {r["id"]: to_text(r.get("fields", {}).get(field))
                                   for r in h["read_airtable"](table, {"fields[]": [field]})}
                except Exception as ex:                        # table missing / no access -> ids are kept
                    log(f"[ITEM-MASTER] Could not read Airtable table '{table}' ({str(ex)[:120]}) -> {col} keeps record ids")
                    linked[col] = {}
            log(f"[ITEM-MASTER] {col}: {len(linked[col])} linked records read to turn ids into values")
        log("[ITEM-MASTER] Fields used: " + ", ".join(f"{c}<-'{n}'" for c, n in found.items()))
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
                elif col in LINKED_COLS:                                # image links / gift items: one per line
                    items = v if isinstance(v, list) else ([] if v in (None, "") else [v])
                    items = [linked.get(col, {}).get(x) or x if is_record_id(x) else to_text(x) for x in items]
                    vals.append("\n".join(i for i in items if i) or None)
                    continue
                elif isinstance(v, list) and v and is_record_id(v[0]) and col not in linked_warned:
                    linked_warned.add(col)
                    log(f"[ITEM-MASTER] '{found[col]}' is a linked field -> record ids saved, not names")
                vals.append(to_number(v) if col in NUMBER_COLS else to_text(v))
            raw = {k: v for k, v in f.items() if k not in DROP_FIELDS}       # "Item ID" is never saved
            rows.append((rec["id"], json.dumps(raw), *vals))

        conn = h["connect"]()
        try:
            cur = conn.cursor()
            cur.execute(IM_CREATE)
            conn.commit()
            sql = (f"INSERT INTO item_master (fi_record_id, airtable_fields, {', '.join(cols)}, loaded_at) VALUES %s "
                   f"ON CONFLICT (fi_record_id) DO UPDATE SET airtable_fields = EXCLUDED.airtable_fields, "
                   + ", ".join(f"{c} = {WEBSITE_COLS_SQL.get(c, 'EXCLUDED.' + c)}" for c in cols) + ", loaded_at = NOW()")
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


# ============================================================ SECTION 5: no automatic load
# Loading happens ONLY from the admin button or /item-master/load?secret=... (no daily background load).


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
 /* title left, our fragrantsouq.com link right (link from competitor_table.our_link) */
 .titlerow{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
 .titlerow .title{flex:1 1 auto;min-width:0}               /* title takes all the free space on the left */
 .ourlink{text-align:right;font-size:15px;font-weight:500;flex:0 0 auto;white-space:nowrap}
 .ourlink a{color:var(--gold);text-underline-offset:3px}
 .ourlink .nolink{color:var(--muted);font-size:14px}
 .ourlink .out{color:#e0806c;font-size:13px;margin-bottom:3px}
 @media (max-width:640px){ .titlerow{flex-direction:column} .ourlink{max-width:100%;text-align:left} }
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
@@DESC_CSS@@
@@WEB_CSS@@
 /* button row: Create in website first (left), Create product description (right), messages on their own line */
 .btnrow{display:flex;flex-wrap:wrap;justify-content:flex-end;align-items:center;gap:8px;margin-top:8px}
 .btnrow .desc-btn{margin:0}
 .btnrow .web-btn{order:1}
 .btnrow .desc-btn:not(.web-btn){order:2}
 .btnrow .desc-msg{order:3;flex-basis:100%;text-align:right;margin-top:0}
 .btnrow .desc-msg:empty{display:none}
 @media (max-width:640px){ .btnrow{justify-content:flex-start} .btnrow .desc-msg{text-align:left} }
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
  {# Run history is in the menu: Item Master -> Run history #}
  <form class="load" method="post" action="/item-master/reload"><button type="submit">Load from Airtable now</button></form>
  {% endif %}
</div>
{% if last.missing %}<div class="info warn">Not found in Airtable (left empty): {{ last.missing|join(', ') }}
  - tell Claude the real Airtable field names.</div>{% endif %}

{# one card per product. Prices, stock qty and UAE comparison price are NOT on the page (database only) #}
{% for r in rows %}
<div class="card">
  <div class="titlerow">
    <div class="title">{{ r.product_name or r.perfume_name or r.sku or r.fi_record_id }}</div>
    {# our product page on fragrantsouq.com - same link as on the Competitor table card #}
    <div class="ourlink">
      {% if r.our_link and r.our_stock == false %}<div class="out">Out of stock</div>{% endif %}
      {% if r.our_link %}<a href="{{ r.our_link }}" target="_blank" title="{{ r.our_title or r.our_link }}">View on fragrantsouq.com &#8599;</a>
      {% else %}<span class="nolink">Not on fragrantsouq.com</span>{% endif %}
      {# admins: the 2 buttons on ONE line - [Create in website] [Create product description], messages below #}
      <div class="btnrow">
      {# Create product description (description_gen.py DESC_HTML) #}
      @@DESC_HTML@@
      {# Create in website - only ON when there is a description (shopify_create.py WEB_HTML) #}
      @@WEB_HTML@@
      </div>
    </div>
  </div>
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

  {# Airtable record id (fi_record_id) is NOT shown - it is only in the database #}
  {% if r.loaded_at %}<div class="date">Loaded {{ r.loaded_at.strftime('%d %b, %H:%M') }}</div>{% endif %}
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
@@DESC_JS@@
@@WEB_JS@@
</script>
</div></body></html>"""
# card parts from description_gen.py (button, its look and its script)
IM_HTML = (IM_HTML.replace("@@DESC_HTML@@", description_gen.DESC_HTML).replace("@@DESC_CSS@@", description_gen.DESC_CSS)
           .replace("@@DESC_JS@@", description_gen.DESC_JS)
           .replace("@@WEB_HTML@@", shopify_create.WEB_HTML).replace("@@WEB_CSS@@", shopify_create.WEB_CSS)
           .replace("@@WEB_JS@@", shopify_create.WEB_JS))


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
            where = "WHERE " + " OR ".join(f"im.{c} ILIKE %s" for c in SEARCH_COLS)
            params = [f"%{q}%"] * len(SEARCH_COLS)
        cur.execute(f"SELECT COUNT(*) AS n FROM item_master im {where}", params)
        total = cur.fetchone()["n"]
        # our fragrantsouq.com link comes from competitor_table (same product = same Airtable record id)
        cur.execute("SELECT to_regclass('competitor_table') IS NOT NULL AS ok")
        if cur.fetchone()["ok"]:
            link_cols = "ct.our_link, ct.our_title, ct.our_stock"
            link_join = "LEFT JOIN competitor_table ct ON ct.fi_record_id = im.fi_record_id"
        else:                                              # competitor table not created yet -> no links
            link_cols, link_join = "NULL AS our_link, NULL AS our_title, NULL AS our_stock", ""
        cur.execute(f"SELECT im.fi_record_id, im.loaded_at, {', '.join('im.' + c for c in SHOWN)}, "
                    f"(COALESCE(im.shopify_description, '') <> '') AS has_desc, "     # button asks before replacing
                    f"{link_cols} FROM item_master im {link_join} {where} "
                    f"ORDER BY LOWER(im.brand), LOWER(im.product_name) LIMIT %s OFFSET %s",
                    params + [PER_PAGE, (page - 1) * PER_PAGE])
        rows = cur.fetchall()
        last = read_summary(cur)
    finally:
        conn.close()
    pages = max(1, math.ceil(total / PER_PAGE))
    log(f"[ITEM-MASTER] Page shown to {g.get('user')}: search {q!r}, page {page}/{pages}, {total} products")
    return render_template_string(
        IM_HTML, nav_html=competitors_page.render_nav("item", "im-list", counts), nav_css=competitors_page.NAV_CSS,
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
    log(f"[ITEM-MASTER] Linked to app.py (table {fi_table} -> item_master) - loads only from the button or "
        f"/item-master/load?secret=...")

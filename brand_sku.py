"""
Brand SKU prefix + next SKU number  (DATABASE ONLY - not shown on any page)
======================================================================================
OUR copy (table brand_sku) of the Airtable "brands" table fields "SKU Prefix" and "Next sku number",
so new SKUs can later be made from our database instead of Airtable.
  brand_sku: brand (as written in Airtable) · sku_prefix (e.g. AAK) · next_sku_number (e.g. 1005)
Filled from Airtable ONCE (done). The Automate page button was removed - our table is the master now; it is
changed by Create a new item (Next SKU number + 1). Re-copy from Airtable only if ever needed: /brand-sku/load?secret=XXX
Every load copies ALL brands again - safe to repeat. Next SKU number: the HIGHER of ours / Airtable's is kept
(Create a new item raises ours when it makes a SKU - an older Airtable number never brings it back down).
Read it with:  python sql.py "SELECT * FROM brand_sku ORDER BY brand"

Where things are (change them here):
  SECTION 1  settings      -> Airtable field names (F_PREFIX, F_NEXT)
  SECTION 2  table         -> BRAND_SKU_CREATE
  SECTION 3  load + read   -> load_from_airtable(), get_map() (brand -> prefix / next; not used by a page any more)
  SECTION 4  routes        -> GET /brand-sku/load?secret= (no page button)
  SECTION 5  hooks         -> init(): database + Airtable reader from app.py
"""

import re
import time

from flask import Blueprint, request, jsonify, g


brand_sku_bp = Blueprint("brand_sku", __name__)


def log(*a):
    """Print to the Render log (lines start with [BRAND-SKU])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
F_PREFIX = "SKU Prefix"              # Airtable "brands" table field names
F_NEXT = "Next sku number"


# ============================================================ SECTION 2: table
BRAND_SKU_CREATE = """
CREATE TABLE IF NOT EXISTS brand_sku (
    brand            TEXT PRIMARY KEY,      -- brand name as written in Airtable "brands"
    sku_prefix       TEXT,                  -- e.g. 'AAK'
    next_sku_number  INTEGER,               -- e.g. 1005 -> the next new SKU is AAK1005
    airtable_id      TEXT,                  -- Airtable brands record id (rec...)
    updated_at       TIMESTAMP DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS brand_sku_lower ON brand_sku (LOWER(brand));
"""
_hooks = {}


# ============================================================ SECTION 3: load + read
def _number(v):
    """Airtable number / lookup / text '1,005' -> int, or None."""
    if isinstance(v, list):
        v = v[0] if v else None
    try:
        return int(float(re.sub(r"[^0-9.\-]", "", str(v)))) if v not in (None, "") else None
    except ValueError:
        return None


def _text(v):
    if isinstance(v, list):
        v = v[0] if v else None
    v = str(v).strip() if v is not None else ""
    return v or None


def load_from_airtable():
    """Copy SKU Prefix + Next sku number of every Airtable brand into brand_sku. Returns a summary dict."""
    started = time.time()
    name_f = _hooks["brands_name_field"]
    recs = _hooks["read_airtable"](_hooks["brands_table"], {"fields[]": [name_f, F_PREFIX, F_NEXT]})
    rows, seen = [], set()
    for r in recs:
        f = r.get("fields", {})
        brand = _text(f.get(name_f))
        if not brand or brand.lower() in seen:          # empty / same brand twice (any case) -> skip
            continue
        seen.add(brand.lower())
        rows.append((brand, _text(f.get(F_PREFIX)), _number(f.get(F_NEXT)), r["id"]))
    conn = _hooks["connect"]()
    try:
        cur = conn.cursor()
        cur.execute(BRAND_SKU_CREATE)
        for brand, prefix, nxt, rid in rows:
            cur.execute("""INSERT INTO brand_sku (brand, sku_prefix, next_sku_number, airtable_id, updated_at)
                           VALUES (%s, %s, %s, %s, NOW())
                           ON CONFLICT ((LOWER(brand))) DO UPDATE SET brand = EXCLUDED.brand,
                               sku_prefix = EXCLUDED.sku_prefix,
                               -- keep the HIGHER number: our website already raised it when it made SKUs
                               next_sku_number = GREATEST(brand_sku.next_sku_number, EXCLUDED.next_sku_number),
                               airtable_id = EXCLUDED.airtable_id, updated_at = NOW()""", (brand, prefix, nxt, rid))
        conn.commit()
    finally:
        conn.close()
    summary = {"brands": len(rows), "with_prefix": sum(1 for r in rows if r[1]),
               "with_next_number": sum(1 for r in rows if r[2] is not None), "seconds": round(time.time() - started, 1)}
    log(f"[BRAND-SKU] Loaded from Airtable '{_hooks['brands_table']}': {summary}")
    return summary


def _vals(r):
    return list(r.values()) if isinstance(r, dict) else list(r)


def get_map(cur):
    """{brand lower case: (sku_prefix, next_sku_number, airtable_name)} for the Automate page ({} = never loaded).
    1. by NAME (brand_sku.brand = competitor_table.brand, any case)          -> airtable_name None
    2. name not found (brand renamed in Airtable after the last load-fi, e.g. "Al Majed for Oud" -> "Al Majed Oud"):
       by the Airtable BRAND RECORD ID, which never changes: competitor_table product -> item_master.airtable_fields
       "Brand" (record id) -> brand_sku.airtable_id                       -> airtable_name = the current Airtable name"""
    cur.execute("SELECT to_regclass('brand_sku') IS NOT NULL AS a, to_regclass('item_master') IS NOT NULL AS b, "
                "to_regclass('competitor_table') IS NOT NULL AS c")       # named: a dict cursor needs names
    has_sku, has_im, has_ct = _vals(cur.fetchone())
    if not has_sku:
        return {}
    cur.execute("SELECT LOWER(brand) AS k, sku_prefix, next_sku_number FROM brand_sku")
    out = {v[0]: (v[1], v[2], None) for v in map(_vals, cur.fetchall())}
    if has_im and has_ct:
        cur.execute("""SELECT DISTINCT ON (LOWER(ct.brand)) LOWER(ct.brand) AS k, bs.sku_prefix, bs.next_sku_number,
                              bs.brand AS at_name
                       FROM competitor_table ct
                       JOIN item_master im ON im.fi_record_id = ct.fi_record_id
                       JOIN brand_sku bs ON bs.airtable_id = im.airtable_fields->'Brand'->>0
                       WHERE COALESCE(ct.brand, '') <> ''
                       ORDER BY LOWER(ct.brand), bs.brand""")
        renamed = 0
        for key, prefix, nxt, at_name in map(_vals, cur.fetchall()):
            if key not in out:
                out[key] = (prefix, nxt, at_name)
                renamed += 1
        if renamed:
            log(f"[BRAND-SKU] {renamed} brand(s) found by Airtable record id (name changed in Airtable since load-fi)")
    return out


# ============================================================ SECTION 4: routes
@brand_sku_bp.get("/brand-sku/load")
def brand_sku_load_url():
    """/brand-sku/load?secret=XXX -> same load (for scripts)."""
    secret = _hooks.get("run_secret")
    if not g.get("user") and (not secret or request.args.get("secret") != secret):
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(load_from_airtable())


# ============================================================ SECTION 5: hooks (called once from app.py)
def init(connect, read_airtable, brands_table, brands_name_field, run_secret=""):
    """connect() -> Postgres connection; read_airtable(table, params) -> Airtable records (app.at_list)."""
    _hooks.update(connect=connect, read_airtable=read_airtable, brands_table=brands_table,
                  brands_name_field=brands_name_field, run_secret=run_secret)
    log(f"[BRAND-SKU] Linked to app.py: Airtable '{brands_table}' ({brands_name_field}, {F_PREFIX}, {F_NEXT}) "
        f"-> table brand_sku")

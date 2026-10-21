"""
Brand SKU prefix + next SKU number  (shown on Settings -> Automate, next to each brand)
======================================================================================
OUR copy (table brand_sku) of the Airtable "brands" table fields "SKU Prefix" and "Next sku number",
so new SKUs can later be made from our database instead of Airtable.
  brand_sku: brand (as written in Airtable) · sku_prefix (e.g. AAK) · next_sku_number (e.g. 1005)
Filled by "Load SKU prefixes from Airtable" on the Automate page (admins) or /brand-sku/load?secret=XXX.
Every load copies ALL brands again (Airtable values replace ours) - safe to repeat.
Read it with:  python sql.py "SELECT * FROM brand_sku ORDER BY brand"

Where things are (change them here):
  SECTION 1  settings      -> Airtable field names (F_PREFIX, F_NEXT)
  SECTION 2  table         -> BRAND_SKU_CREATE
  SECTION 3  load + read   -> load_from_airtable(), get_map() (used by competitors_page.py Automate page)
  SECTION 4  routes        -> POST /settings/brand-sku/load (admin button), GET /brand-sku/load?secret=
  SECTION 5  hooks         -> init(): database + Airtable reader from app.py
"""

import re
import time

from flask import Blueprint, request, jsonify, redirect, g

from auth import is_admin

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
                               sku_prefix = EXCLUDED.sku_prefix, next_sku_number = EXCLUDED.next_sku_number,
                               airtable_id = EXCLUDED.airtable_id, updated_at = NOW()""", (brand, prefix, nxt, rid))
        conn.commit()
    finally:
        conn.close()
    summary = {"brands": len(rows), "with_prefix": sum(1 for r in rows if r[1]),
               "with_next_number": sum(1 for r in rows if r[2] is not None), "seconds": round(time.time() - started, 1)}
    log(f"[BRAND-SKU] Loaded from Airtable '{_hooks['brands_table']}': {summary}")
    return summary


def get_map(cur):
    """{brand lower case: (sku_prefix, next_sku_number)} for the Automate page ({} when never loaded)."""
    cur.execute("SELECT to_regclass('brand_sku') IS NOT NULL")
    row = cur.fetchone()
    if not (row[0] if not isinstance(row, dict) else list(row.values())[0]):
        return {}
    cur.execute("SELECT LOWER(brand), sku_prefix, next_sku_number FROM brand_sku")
    out = {}
    for r in cur.fetchall():
        vals = list(r.values()) if isinstance(r, dict) else r
        out[vals[0]] = (vals[1], vals[2])
    return out


# ============================================================ SECTION 4: routes
@brand_sku_bp.post("/settings/brand-sku/load")
def brand_sku_load_button():
    """Automate page button (admins): load from Airtable, back to the page with a message."""
    if not is_admin():
        log(f"[BRAND-SKU] Refused: {g.get('user')} is not an admin")
        return jsonify({"error": "admins only"}), 403
    try:
        s = load_from_airtable()
        msg = f"SKU prefixes loaded from Airtable: {s['brands']} brands ({s['with_prefix']} with a prefix)."
    except Exception as ex:
        log(f"[BRAND-SKU] Load failed: {ex}")
        msg = f"SKU prefixes NOT loaded: {str(ex)[:150]}"
    log(f"[BRAND-SKU] {g.get('user')}: {msg}")
    return redirect("/settings/automate?msg=" + msg.replace(" ", "+").replace("&", "and"))


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

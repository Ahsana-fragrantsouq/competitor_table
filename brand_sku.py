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
NEW brands are made on our website: Item Master -> Brands -> "Create new" (Brand name + SKU prefix, both must be NEW,
any capitals / spaces). Next SKU number starts at FIRST_SKU_NUMBER. Those rows have no airtable_id (created_by = admin).

Where things are (change them here):
  SECTION 1  settings      -> Airtable field names (F_PREFIX, F_NEXT)
  SECTION 2  table         -> BRAND_SKU_CREATE
  SECTION 3  load + read   -> load_from_airtable(), get_map() (brand -> prefix / next; not used by a page any more)
  SECTION 4  routes        -> GET /brand-sku/load?secret= (no page button), POST /brand-sku/create (Create new brand)
  SECTION 5  hooks         -> init(): database + Airtable reader from app.py
"""

import re
import time

from flask import Blueprint, request, jsonify, g

from auth import is_admin


brand_sku_bp = Blueprint("brand_sku", __name__)


def log(*a):
    """Print to the Render log (lines start with [BRAND-SKU])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
F_PREFIX = "SKU Prefix"              # Airtable "brands" table field names
F_NEXT = "Next sku number"
FIRST_SKU_NUMBER = 1001              # Next SKU number of a brand made with "Create new" (first SKU = prefix + 1001)


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
ALTER TABLE brand_sku ADD COLUMN IF NOT EXISTS created_by TEXT;     -- admin who made it on our website (Create new)
ALTER TABLE brand_sku ALTER COLUMN next_sku_number SET DEFAULT 1001;  -- new brand: first SKU = prefix + 1001
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


_table_ready = False


def ensure_table(conn):
    """Create brand_sku / add new columns ONCE after each start (ALTER TABLE on every request would wait on locks)."""
    global _table_ready
    if not _table_ready:
        conn.cursor().execute(BRAND_SKU_CREATE)
        conn.commit()
        _table_ready = True
        log("[BRAND-SKU] Table brand_sku ready")


def _key(s):
    """Compare names without capitals / extra spaces: ' Al  Majed ' == 'al majed'."""
    return " ".join((s or "").split()).lower()


def create_brand(conn, name, prefix, user):
    """New brand in brand_sku. Returns (row dict, None) or (None, reason). Refused when:
    - name or prefix is empty / prefix is not letters + digits
    - the brand name already exists (brand_sku, or products in item_master / competitor_table) - any capitals / spaces
    - the SKU prefix already belongs to another brand, or SKUs like PREFIX1234 are already used in item_master"""
    name = " ".join((name or "").split())
    prefix = (prefix or "").strip().upper()
    if not name or not prefix:
        return None, "Brand name and SKU prefix are both needed"
    if not re.fullmatch(r"[A-Z0-9]{2,10}", prefix):
        return None, "SKU prefix: 2-10 letters / digits only (e.g. AFN)"
    ensure_table(conn)
    cur = conn.cursor()
    # two admins creating at the same moment -> the 2nd waits here until the 1st is saved (no table lock)
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('brand_sku_create'))")
    # 1. brand name must be new (brand_sku + brands already used by products)
    sql = "SELECT brand FROM brand_sku WHERE LOWER(REGEXP_REPLACE(TRIM(brand), '\\s+', ' ', 'g')) = %s"
    for t in ("item_master", "competitor_table"):
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (t,))
        if cur.fetchone()[0]:
            sql += f" UNION SELECT brand FROM {t} WHERE LOWER(REGEXP_REPLACE(TRIM(brand), '\\s+', ' ', 'g')) = %s"
    cur.execute(sql + " LIMIT 1", [_key(name)] * sql.count("%s"))
    row = cur.fetchone()
    if row:
        conn.rollback()
        log(f"[BRAND-SKU] Create refused: brand '{name}' already exists as '{row[0]}'")
        return None, f"Brand '{row[0]}' already exists"
    # 2. SKU prefix must be new
    cur.execute("SELECT brand FROM brand_sku WHERE UPPER(TRIM(sku_prefix)) = %s LIMIT 1", (prefix,))
    row = cur.fetchone()
    if row:
        conn.rollback()
        log(f"[BRAND-SKU] Create refused: prefix {prefix} already belongs to '{row[0]}'")
        return None, f"SKU prefix {prefix} already belongs to brand '{row[0]}'"
    cur.execute("SELECT to_regclass('item_master') IS NOT NULL")
    if cur.fetchone()[0]:
        cur.execute("SELECT sku FROM item_master WHERE UPPER(TRIM(sku)) ~ %s LIMIT 1", (f"^{prefix}[0-9]+$",))
        row = cur.fetchone()
        if row:
            conn.rollback()
            log(f"[BRAND-SKU] Create refused: SKUs with prefix {prefix} already used (e.g. {row[0]})")
            return None, f"SKU prefix {prefix} is already used by products (e.g. {row[0]})"
    cur.execute("INSERT INTO brand_sku (brand, sku_prefix, next_sku_number, created_by, updated_at) "
                "VALUES (%s, %s, %s, %s, NOW())", (name, prefix, FIRST_SKU_NUMBER, user))
    conn.commit()
    log(f"[BRAND-SKU] {user} created brand '{name}' prefix {prefix}, next SKU {prefix}{FIRST_SKU_NUMBER}")
    return {"brand": name, "sku_prefix": prefix, "next_sku_number": FIRST_SKU_NUMBER}, None


@brand_sku_bp.post("/brand-sku/create")
def brand_sku_create():
    """Item Master -> Brands -> Create new (admins). Body: {brand, sku_prefix}."""
    if not is_admin():
        log(f"[BRAND-SKU] Create refused: {g.get('user')} is not an admin")
        return jsonify({"ok": False, "error": "admins only"}), 403
    body = request.get_json(silent=True) or {}
    conn = _hooks["connect"]()
    try:
        row, err = create_brand(conn, body.get("brand"), body.get("sku_prefix"), g.get("user"))
    except Exception as ex:
        conn.rollback()
        log(f"[BRAND-SKU] Create ERROR: {ex}")
        return jsonify({"ok": False, "error": str(ex)[:200]}), 500
    finally:
        conn.close()
    if err:
        return jsonify({"ok": False, "error": err}), 409
    return jsonify({"ok": True, **row})


# ============================================================ SECTION 5: hooks (called once from app.py)
def init(connect, read_airtable, brands_table, brands_name_field, run_secret=""):
    """connect() -> Postgres connection; read_airtable(table, params) -> Airtable records (app.at_list)."""
    _hooks.update(connect=connect, read_airtable=read_airtable, brands_table=brands_table,
                  brands_name_field=brands_name_field, run_secret=run_secret)
    log(f"[BRAND-SKU] Linked to app.py: Airtable '{brands_table}' ({brands_name_field}, {F_PREFIX}, {F_NEXT}) "
        f"-> table brand_sku")

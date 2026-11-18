"""
Samawa Catalog (samawa.ae -> Postgres french_fragrance_db, table samawa_catalog)
Plugged into app.py as a Blueprint.

Routes:
  GET /samawa-catalog/run?secret=XXX   -> download full Samawa catalog into the database (background)
  GET /samawa-catalog/status           -> progress of the download
  GET /samawa-catalog                  -> view the table (search + stock filter + pages)

Env vars:
  FF_DATABASE_URL = Postgres URL ending in /french_fragrance_db (same as ff-catalog page)
  RUN_SECRET      = same secret used for /samawa/run
"""

import os
import re
import math
import time
import threading
import traceback

import requests
import psycopg2
import psycopg2.extras
from psycopg2.extras import execute_values
from flask import Blueprint, request, jsonify, render_template_string

samawa_catalog_bp = Blueprint("samawa_catalog", __name__)

SM_BASE = "https://samawa.ae"
SM_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
SM_MAX_PAGES = 100            # Shopify limit: page 101+ returns 400 (max 25,000 products per list)
SM_RETRY_WAITS = [10, 30, 60, 120, 180]
SM_PER_PAGE_VIEW = 100
# NO ENDLESS WAITING (2026-11-17, live: the download stopped after ~11,750 products and stayed "running" for hours ->
# Run 2 "timeout", every "Run again" refused as busy; the database showed nothing waiting). Every step now has a limit:
SM_PAGE_SECONDS = 120         # one Samawa page must be fully downloaded within this (else: retry, like a network error)
SM_DB_STATEMENT = "600s"      # one database statement (save of a page incl. price triggers) at most 10 min
SM_DB_LOCK = "120s"           # rows busy (refresh / price update) longer than 2 min -> error instead of waiting forever
# COLLECTIONS TIME BUDGET (2026-11-18, live log): the catalog stops at 25,000 products (Shopify limit), then EVERY
# collection is read for the ~1,900 missing ones; Samawa answers "429 busy" again and again -> hours -> Run 2 gave up
# ("timeout") and "Run again" was refused while it kept running. Now: at most this many minutes for the collections,
# then the run ends (missing products keep their last price; nothing marked 'Not on site' because it is incomplete).
SM_COLLECTIONS_MINUTES = 40   # the next run continues with the collection after the last one read (sm_state)

sm_lock = threading.Lock()
sm_state = {"running": False, "last": None}


def sm_log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- database
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS samawa_catalog (
    id           SERIAL PRIMARY KEY,
    variant_id   BIGINT UNIQUE NOT NULL,
    product_id   BIGINT,
    product_url  TEXT,
    gtin         TEXT,
    brand        TEXT,
    name         TEXT,
    price        NUMERIC(10,2),
    stock        TEXT,
    volume       TEXT,
    updated_at   TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sm_gtin ON samawa_catalog (gtin);
"""

UPSERT_SQL = """
INSERT INTO samawa_catalog (variant_id, product_id, product_url, gtin, brand, name, price, stock, volume)
VALUES %s
ON CONFLICT (variant_id) DO UPDATE SET
    product_id  = EXCLUDED.product_id,
    product_url = EXCLUDED.product_url,
    gtin        = EXCLUDED.gtin,
    brand       = EXCLUDED.brand,
    name        = EXCLUDED.name,
    price       = EXCLUDED.price,
    stock       = EXCLUDED.stock,
    volume      = EXCLUDED.volume,
    updated_at  = NOW();
"""


def sm_db_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    sm_log("[SM-DB] Opening connection to french_fragrance_db")
    # keepalives: a connection that silently dropped is noticed in ~1 min (else a query waits on it forever)
    return psycopg2.connect(url, sslmode="require", connect_timeout=30,
                            keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5,
                            options=f"-c statement_timeout={SM_DB_STATEMENT} -c lock_timeout={SM_DB_LOCK}")


# ---------------------------------------------------------------- helpers
def sm_volume(*texts):
    """'Dior Sauvage EDP 100ml' -> '100 ml'"""
    for t in texts:
        m = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", str(t or ""), re.I)
        if m:
            return f"{m.group(1)} ml"
    return None


def sm_price(v):
    try:
        return round(float(v), 2) if v not in (None, "") else None
    except ValueError:
        return None


def sm_rows(products):
    """One row per variant (same product can have 50ml / 100ml variants)."""
    rows = []
    for p in products:
        handle, title = p.get("handle", ""), (p.get("title") or "").strip()
        variants = p.get("variants", [])
        for v in variants:
            vtitle = (v.get("title") or "").strip()
            name = title if vtitle.lower() in ("", "default title") else f"{title} - {vtitle}"
            url = f"{SM_BASE}/products/{handle}"
            if len(variants) > 1:
                url += f"?variant={v.get('id')}"
            sku = (v.get("sku") or "").strip() or None      # Samawa puts the barcode in SKU
            rows.append((
                v.get("id"), p.get("id"), url, sku, (p.get("vendor") or "").strip() or None, name,
                sm_price(v.get("price")),
                "In stock" if v.get("available") else "Out of stock",
                sm_volume(vtitle, title),
            ))
    return rows


def _get_with_deadline(url, params):
    """requests.get with a limit on the WHOLE download. timeout=60 alone is per piece of data: a server that keeps
    sending a few bytes never times out. The download runs in its own thread; not finished after SM_PAGE_SECONDS ->
    TimeoutError (that thread is left to end on its own - it never holds sm_lock or the database)."""
    box = {}

    def fetch():
        try:
            box["r"] = requests.get(url, params=params, headers=SM_HEADERS, timeout=(15, 60))
        except Exception as ex:                     # handed to the caller below
            box["ex"] = ex

    t = threading.Thread(target=fetch, daemon=True, name="sm-page")
    t.start()
    t.join(SM_PAGE_SECONDS)
    if t.is_alive():
        raise TimeoutError(f"page not downloaded within {SM_PAGE_SECONDS}s")
    if "ex" in box:
        raise box["ex"]
    return box["r"]


def sm_get(path, page, label):
    """GET one page from Samawa with retries on 429 / 5xx / network errors. Returns Response or None."""
    for attempt, wait in enumerate([0] + SM_RETRY_WAITS):
        if wait:
            sm_log(f"[SM] {label} page {page}: retry {attempt}/{len(SM_RETRY_WAITS)} in {wait}s")
            time.sleep(wait)
        sm_state["at"] = f"downloading {label} page {page} (since {time.strftime('%H:%M:%S')} UTC)"
        try:
            r = _get_with_deadline(f"{SM_BASE}{path}", {"limit": 250, "page": page})
        except (requests.RequestException, TimeoutError) as ex:
            sm_log(f"[SM] {label} page {page}: network error {ex}")
            continue
        if r.status_code == 429 or r.status_code >= 500:
            sm_log(f"[SM] {label} page {page}: HTTP {r.status_code} (Samawa busy)")
            continue
        return r
    sm_log(f"[SM] {label} page {page}: giving up")
    return None


def sm_pages(path, label, list_key, on_batch, max_pages=SM_MAX_PAGES, deadline=None):
    """Walk pages and hand each batch to on_batch(). Returns (hit_cap, incomplete).
    deadline (time.time() value): past it -> stop before the next page, counted as incomplete."""
    page = 1
    while page <= max_pages:
        if deadline and time.time() > deadline:
            sm_log(f"[SM] {label}: time budget used up at page {page} -> stopped")
            return False, True
        r = sm_get(path, page, label)
        if r is None:
            return False, True
        if r.status_code in (400, 404):
            sm_log(f"[SM] {label}: stopped at page {page} (HTTP {r.status_code})")
            return page > max_pages, False
        r.raise_for_status()
        batch = r.json().get(list_key, [])
        if not batch:
            return False, False
        on_batch(batch)
        sm_log(f"[SM] {label} page {page}: {len(batch)} items")
        if len(batch) < 250:
            return False, False
        page += 1
        time.sleep(1.5)
    sm_log(f"[SM] {label}: reached {max_pages}-page limit")
    return True, False


# ---------------------------------------------------------------- download job
def sm_run():
    started = time.time()
    st = {"products": 0, "variants": 0, "from_collections": 0, "incomplete": False,
          "marked_not_on_site": 0, "seconds": 0, "status": "running"}
    sm_state["last"] = st

    conn = sm_db_conn()
    cur = conn.cursor()
    sm_log("[SM-DB] Creating table samawa_catalog (if not exists)")
    cur.execute(CREATE_SQL)
    cur.execute("SELECT NOW()")
    run_start = cur.fetchone()[0]
    conn.commit()

    seen = set()   # product ids already saved in this run

    def save(products, source):
        new = [p for p in products if p.get("id") not in seen]
        if not new:
            return
        seen.update(p.get("id") for p in new)
        rows = sm_rows(new)
        if rows:
            sm_state["at"] = f"saving {len(rows)} variants in the database (since {time.strftime('%H:%M:%S')} UTC)"
            execute_values(cur, UPSERT_SQL, rows)
            conn.commit()
        st["products"] += len(new)
        st["variants"] += len(rows)
        if source == "collection":
            st["from_collections"] += len(new)
        st["seconds"] = round(time.time() - started, 1)
        sm_log(f"[SM-DB] Saved {len(new)} products / {len(rows)} variants "
               f"(total {st['products']} products, {st['variants']} variants)")

    try:
        # 1) full catalog list
        sm_log("[SM] Downloading /products.json ...")
        hit_cap, incomplete = sm_pages("/products.json", "catalog", "products", lambda b: save(b, "catalog"))
        st["incomplete"] |= incomplete

        # 2) catalog capped at 25,000 -> fill the rest from every collection
        if hit_cap:
            sm_log("[SM] Catalog hit the 25,000 limit -> reading all collections for missing products")
            collections = []
            _, inc = sm_pages("/collections.json", "collections", "collections", collections.extend)
            st["incomplete"] |= inc
            sm_log(f"[SM] {len(collections)} collections to check")
            coll_started = time.time()
            first = sm_state.get("coll_next", 0) % max(1, len(collections))     # continue where the last run stopped
            if first:
                sm_log(f"[SM] Collections: starting at {first + 1} (the last run stopped there)")
            order = collections[first:] + collections[:first]
            sm_state["coll_next"] = 0
            for i, c in enumerate(order, 1):
                if time.time() - coll_started > SM_COLLECTIONS_MINUTES * 60:
                    sm_state["coll_next"] = (first + i - 1) % len(collections)
                    sm_log(f"[SM] Collections: {SM_COLLECTIONS_MINUTES} min used, {i - 1}/{len(collections)} read "
                           f"-> next run continues there (products not reached keep their last price)")
                    st["incomplete"] = True
                    st["collections_read"] = f"{i - 1}/{len(collections)}"
                    break
                sm_log(f"[SM] Collection {i}/{len(collections)}: {c.get('title')}")
                _, inc = sm_pages(f"/collections/{c['handle']}/products.json", c["handle"], "products",
                                  lambda b: save(b, "collection"),
                                  deadline=coll_started + SM_COLLECTIONS_MINUTES * 60)
                st["incomplete"] |= inc

        # 3) products not seen in a COMPLETE run are gone from Samawa
        if st["incomplete"]:
            sm_log("[SM] Download incomplete -> NOT marking missing products")
        else:
            cur.execute("UPDATE samawa_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            st["marked_not_on_site"] = cur.rowcount
            conn.commit()
            sm_log(f"[SM] {cur.rowcount} old rows marked 'Not on site'")

        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        sm_log(f"[SM-DONE] {st}")


def sm_worker():
    try:
        sm_run()
    except Exception as ex:
        sm_log(f"[SM-ERROR] {ex}\n{traceback.format_exc()}")
        sm_state["last"] = {**(sm_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        sm_state["running"] = False
        sm_lock.release()


# ---------------------------------------------------------------- routes
@samawa_catalog_bp.route("/samawa-catalog/run")
def sm_run_route():
    secret = os.environ.get("RUN_SECRET", "")
    if secret and request.args.get("secret") != secret:
        sm_log("[SM-RUN] Unauthorized attempt")
        return jsonify({"error": "unauthorized"}), 401
    if not sm_lock.acquire(blocking=False):
        return jsonify({"error": "Samawa catalog download already running"}), 409
    sm_state["running"] = True
    sm_log("[SM-RUN] Started Samawa catalog download")
    threading.Thread(target=sm_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/samawa-catalog/status"}), 202


@samawa_catalog_bp.route("/samawa-catalog/status")
def sm_status_route():
    return jsonify({"running": sm_state["running"], "last": sm_state["last"],
                    "at": sm_state.get("at")})        # the step it is on now (shows where a slow run waits)


SM_HTML = """
<!doctype html><html><head><title>Samawa Catalog</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font-family:Arial,sans-serif;margin:30px 38px;color:#111}
 h1{font-size:40px;margin:10px 0}
 h2{font-size:30px;margin:24px 0 16px}
 .bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
 .bar input,.bar select{padding:9px;font-size:15px;border:1px solid #ccc;border-radius:4px}
 .bar input[type=text]{width:320px}
  .btn{background:#f2d675;color:#1c1b19;border:0;padding:10px 18px;border-radius:4px;cursor:pointer;font-size:15px;text-decoration:none;font-weight:600}
 .wrap{overflow-x:auto}
 table{border-collapse:collapse;width:100%;font-size:15px}
 th,td{border:1px solid #ddd;padding:8px;text-align:left;vertical-align:middle}
 th{background:#f4f4f4;white-space:nowrap}
 td.name{min-width:260px}
 .in{color:#070} .out{color:#b00} .gone{color:#888}
 .pager{margin:18px 0;display:flex;gap:10px;align-items:center}
</style></head><body>
<h1>Saved Data</h1>
<h2>Samawa Catalog ({{ total }})</h2>

<form class="bar" method="get">
  <input type="text" name="q" value="{{ q }}" placeholder="Search name, brand or GTIN">
  <select name="stock">
    <option value="" {% if not stock %}selected{% endif %}>All stock</option>
    <option value="In stock" {% if stock=='In stock' %}selected{% endif %}>In stock</option>
    <option value="Out of stock" {% if stock=='Out of stock' %}selected{% endif %}>Out of stock</option>
    <option value="Not on site" {% if stock=='Not on site' %}selected{% endif %}>Not on site</option>
  </select>
  <button class="btn" type="submit">Search</button>
  <a class="btn" href="{{ request.path }}">Clear</a>
</form>

<div class="wrap">
<table>
  <tr>
    <th>id</th><th>product_url</th><th>gtin</th><th>brand</th><th>name</th>
    <th>price</th><th>stock</th><th>volume</th><th>updated_at</th>
  </tr>
  {% for r in rows %}
  <tr>
    <td>{{ r.id }}</td>
    <td>{% if r.product_url %}<a href="{{ r.product_url }}" target="_blank">View product</a>{% endif %}</td>
    <td>{{ r.gtin or '' }}</td>
    <td>{{ r.brand or '' }}</td>
    <td class="name">{{ r.name or '' }}</td>
    <td>{% if r.price is not none %}AED {{ '%.2f' % r.price }}{% endif %}</td>
    <td class="{{ 'in' if r.stock=='In stock' else ('gone' if r.stock=='Not on site' else 'out') }}">{{ r.stock or '' }}</td>
    <td>{{ r.volume or '' }}</td>
    <td>{{ r.updated_at }}</td>
  </tr>
  {% else %}
  <tr><td colspan="9">No products yet. Open /samawa-catalog/run?secret=... to download the Samawa catalog, or clear the search.</td></tr>
  {% endfor %}
</table>
</div>

<div class="pager">
  {% if page > 1 %}<a class="btn" href="?q={{ q }}&stock={{ stock }}&page={{ page-1 }}">Previous</a>{% endif %}
  <span>Page {{ page }} of {{ pages }}</span>
  {% if page < pages %}<a class="btn" href="?q={{ q }}&stock={{ stock }}&page={{ page+1 }}">Next</a>{% endif %}
</div>
</body></html>
"""


@samawa_catalog_bp.route("/samawa-catalog")
def sm_page_route():
    q = request.args.get("q", "").strip()
    stock = request.args.get("stock", "").strip()
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1
    sm_log(f"[SM-PAGE] Search q={q!r} stock={stock!r} page={page}")

    where, params = [], []
    if q:
        where.append("(name ILIKE %s OR brand ILIKE %s OR gtin ILIKE %s)")
        params += [f"%{q}%"] * 3
    if stock:
        where.append("stock = %s")
        params.append(stock)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = sm_db_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(CREATE_SQL)          # page works even before the first download
        conn.commit()
        cur.execute(f"SELECT COUNT(*) AS c FROM samawa_catalog {where_sql}", params)
        total = cur.fetchone()["c"]
        pages = max(math.ceil(total / SM_PER_PAGE_VIEW), 1)
        page = min(page, pages)
        cur.execute(
            f"""SELECT id, product_url, gtin, brand, name, price, stock, volume, updated_at
                FROM samawa_catalog {where_sql}
                ORDER BY id LIMIT %s OFFSET %s""",
            params + [SM_PER_PAGE_VIEW, (page - 1) * SM_PER_PAGE_VIEW],
        )
        rows = cur.fetchall()
        sm_log(f"[SM-PAGE] {total} matching rows, showing {len(rows)} on page {page}/{pages}")
    finally:
        cur.close()
        conn.close()

    return render_template_string(SM_HTML, rows=rows, total=total, page=page,
                                  pages=pages, q=q, stock=stock)
"""
Amberiya catalog (amberiya.ae -> Postgres french_fragrance_db, table amberiya_catalog)
Plugged into app.py as a Blueprint.

Amberiya is a Shopify store (like Samawa), so its full product list is public:
    https://www.amberiya.ae/products.json?limit=250&page=1, 2, 3 ...
One row per variant (a product with 50ml + 100ml = 2 rows).
Brand = Shopify "vendor" (e.g. "L'Arc"), name = product title + size, link opens the right size.

Routes (open in the browser):
  GET /amberiya/test?secret=XXX    read page 1 only and SHOW what was found (run this first)
  GET /amberiya/run?secret=XXX     download everything into amberiya_catalog (background, ~1 min)
  GET /amberiya/status             progress

Env vars (already set on Render): FF_DATABASE_URL, RUN_SECRET
"""

import os
import re
import time
import threading
import traceback

import requests
import psycopg2
from psycopg2.extras import execute_values
from flask import Blueprint, request, jsonify

amberiya_bp = Blueprint("amberiya", __name__)

AM_BASE = "https://www.amberiya.ae"
AM_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
              "Accept": "application/json"}
AM_MAX_PAGES = 100              # Shopify limit: max 100 pages x 250 = 25,000 products
AM_RETRY_WAITS = [10, 30, 60, 120]

am_lock = threading.Lock()
am_state = {"running": False, "last": None}


def am_log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- database
AM_CREATE = """
CREATE TABLE IF NOT EXISTS amberiya_catalog (
    id            SERIAL PRIMARY KEY,
    variant_id    BIGINT UNIQUE NOT NULL,   -- Shopify variant id (one row per size)
    product_url   TEXT,                     -- https://www.amberiya.ae/products/...?variant=...
    gtin          TEXT,                     -- barcode, only when their SKU is a real barcode
    sku           TEXT,                     -- their SKU as written in Shopify
    brand         TEXT,                     -- Shopify vendor, e.g. "L'Arc"
    name          TEXT,                     -- "Argentium Eau de Parfum - 100ml"
    product_type  TEXT,                     -- Shopify product type, e.g. "Eau de Parfum"
    price_inc_tax NUMERIC(10,2),            -- price shown on the site (UAE prices include 5% VAT)
    price         NUMERIC(10,2),            -- without VAT (price_inc_tax / 1.05)
    stock         TEXT,                     -- In stock / Out of stock / Not on site
    volume        TEXT,                     -- e.g. "100 ml"
    updated_at    TIMESTAMP DEFAULT NOW()
);
"""

AM_UPSERT = """
INSERT INTO amberiya_catalog (variant_id, product_url, gtin, sku, brand, name, product_type,
                              price_inc_tax, price, stock, volume)
VALUES %s
ON CONFLICT (variant_id) DO UPDATE SET
    product_url = EXCLUDED.product_url, gtin = EXCLUDED.gtin, sku = EXCLUDED.sku, brand = EXCLUDED.brand,
    name = EXCLUDED.name, product_type = EXCLUDED.product_type, price_inc_tax = EXCLUDED.price_inc_tax,
    price = EXCLUDED.price, stock = EXCLUDED.stock, volume = EXCLUDED.volume, updated_at = NOW();
"""


def am_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    am_log("[AM-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ---------------------------------------------------------------- reading products
def am_volume(*texts):
    """'100ml' / 'Argentium EDP 100 ML' -> '100 ml'"""
    for t in texts:
        m = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", str(t or ""), re.I)
        if m:
            return f"{m.group(1)} ml"
    return None


def am_items(products):
    """Shopify products -> one dict per variant."""
    out = []
    for p in products:
        title = (p.get("title") or "").strip()
        variants = p.get("variants", [])
        for v in variants:
            vtitle = (v.get("title") or "").strip()
            name = title if vtitle.lower() in ("", "default title") else f"{title} - {vtitle}"
            url = f"{AM_BASE}/products/{p.get('handle')}"
            if len(variants) > 1:
                url += f"?variant={v.get('id')}"                 # opens the right size
            try:
                price_inc = round(float(v.get("price")), 2)
            except (TypeError, ValueError):
                price_inc = None
            sku = (v.get("sku") or "").strip() or None
            digits = re.sub(r"\D", "", sku or "")
            out.append({
                "variant_id": v.get("id"), "url": url,
                "gtin": digits if sku and digits == sku and len(digits) >= 8 else None,   # pure barcode SKU only
                "sku": sku, "brand": (p.get("vendor") or "").strip() or None, "name": name,
                "product_type": (p.get("product_type") or "").strip() or None,
                "price_inc_tax": price_inc, "price": round(price_inc / 1.05, 2) if price_inc else None,
                "stock": "In stock" if v.get("available") else "Out of stock",
                "volume": am_volume(vtitle, title),
            })
    return out


def am_get(page):
    """One page of products.json (retries when busy). Returns list of products, [] at the end, None on failure."""
    for attempt, wait in enumerate([0] + AM_RETRY_WAITS):
        if wait:
            am_log(f"[AM] page {page}: retry {attempt}/{len(AM_RETRY_WAITS)} in {wait}s")
            time.sleep(wait)
        try:
            r = requests.get(f"{AM_BASE}/products.json", params={"limit": 250, "page": page},
                             headers=AM_HEADERS, timeout=60)
        except requests.RequestException as ex:
            am_log(f"[AM] page {page}: network error {ex}")
            continue
        if r.status_code == 429 or r.status_code >= 500:
            am_log(f"[AM] page {page}: HTTP {r.status_code} (busy)")
            continue
        if r.status_code in (400, 404):
            return []
        if r.status_code != 200:
            raise RuntimeError(f"Amberiya answered HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json().get("products", [])
        except ValueError:
            raise RuntimeError(f"Not a product list (blocked?): {r.text[:200]}")
    return None


# ---------------------------------------------------------------- full download
def am_run():
    started = time.time()
    st = {"status": "running", "pages": 0, "products": 0, "saved": 0, "in_stock": 0, "out_of_stock": 0,
          "incomplete": False}
    am_state["last"] = st
    conn = am_conn()
    cur = conn.cursor()
    try:
        cur.execute(AM_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()
        for page in range(1, AM_MAX_PAGES + 1):
            products = am_get(page)
            if products is None:
                st["incomplete"] = True
                am_log(f"[AM] page {page}: gave up -> stopping (run is incomplete)")
                break
            if not products:
                break
            items = am_items(products)
            rows = [(i["variant_id"], i["url"], i["gtin"], i["sku"], i["brand"], i["name"], i["product_type"],
                     i["price_inc_tax"], i["price"], i["stock"], i["volume"]) for i in items if i["variant_id"]]
            if rows:
                execute_values(cur, AM_UPSERT, rows)
                conn.commit()
            st["pages"] = page
            st["products"] += len(products)
            st["saved"] += len(rows)
            st["in_stock"] += sum(i["stock"] == "In stock" for i in items)
            st["out_of_stock"] += sum(i["stock"] != "In stock" for i in items)
            st["seconds"] = round(time.time() - started, 1)
            am_log(f"[AM] page {page}: {len(products)} products, {len(rows)} variants saved (total {st['saved']})")
            if len(products) < 250:
                break
            time.sleep(1.5)
        if not st["incomplete"] and st["saved"]:             # products gone from the site
            cur.execute("UPDATE amberiya_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            st["marked_not_on_site"] = cur.rowcount
            conn.commit()
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        am_log(f"[AM-DONE] {st}")


def am_worker():
    try:
        am_run()
    except Exception as ex:
        am_log(f"[AM-ERROR] {ex}\n{traceback.format_exc()}")
        am_state["last"] = {**(am_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        am_state["running"] = False
        am_lock.release()


# ---------------------------------------------------------------- routes
def am_secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


@amberiya_bp.route("/amberiya/test")
def am_test_route():
    """Read page 1 only - shows whether Render can read Amberiya and what is read."""
    if not am_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        products = am_get(1)
    except RuntimeError as ex:
        am_log(f"[AM-TEST] {ex}")
        return jsonify({"ok": False, "error": str(ex)})
    items = am_items(products or [])
    result = {"ok": bool(items), "page1_products": len(products or []), "page1_variants": len(items),
              "with_sku": sum(1 for i in items if i["sku"]), "with_barcode": sum(1 for i in items if i["gtin"]),
              "brands_seen": sorted({i["brand"] or "-" for i in items})[:20],
              "product_types": sorted({i["product_type"] or "-" for i in items})[:20],
              "first_3": items[:3]}
    am_log(f"[AM-TEST] {result}")
    return jsonify(result)


@amberiya_bp.route("/amberiya/run")
def am_run_route():
    if not am_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not am_lock.acquire(blocking=False):
        return jsonify({"error": "Amberiya download already running"}), 409
    am_state["running"] = True
    am_log("[AM-RUN] Started")
    threading.Thread(target=am_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/amberiya/status"}), 202


@amberiya_bp.route("/amberiya/status")
def am_status_route():
    return jsonify({"running": am_state["running"], "last": am_state["last"]})
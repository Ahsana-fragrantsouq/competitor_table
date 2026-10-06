"""
Niche Gallerie catalog (nichegallerie.com -> Postgres french_fragrance_db, table nichegallerie_catalog)
Plugged into app.py as a Blueprint.

Niche Gallerie runs on WordPress + WooCommerce. WooCommerce has a public product list (no key needed):
    GET https://nichegallerie.com/wp-json/wc/store/v1/products?per_page=100&page=1, 2, 3 ...
Each product: name, permalink (link), sku, prices (in fils -> divided by 100), is_in_stock.

Same fields as the other catalogs: Product URL, GTIN, Name, Price Inc Tax, Price, Stock, Volume (+ SKU).

Routes (open in the browser):
  GET /nichegallerie/test?secret=XXX    read pages 1 and 2 only and SHOW what was found (run this first)
  GET /nichegallerie/run?secret=XXX     download all products into nichegallerie_catalog (background)
  GET /nichegallerie/status             progress

Env vars (already set on Render): FF_DATABASE_URL, RUN_SECRET
"""

import os
import re
import html as htmllib
import time
import threading
import traceback

import requests
import psycopg2
from psycopg2.extras import execute_values
from flask import Blueprint, request, jsonify

nichegallerie_bp = Blueprint("nichegallerie", __name__)

NG_SITE = "https://nichegallerie.com"
NG_API = NG_SITE + "/wp-json/wc/store/v1/products"    # WooCommerce Store API (public)
NG_PER_PAGE = 100                                     # WooCommerce maximum per request
NG_MAX_PAGES = 300
NG_DELAY = 1.0                                        # seconds between requests (be gentle)
NG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept": "application/json",
}

ng_lock = threading.Lock()
ng_state = {"running": False, "last": None}


def ng_log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- database
NG_CREATE = """
CREATE TABLE IF NOT EXISTS nichegallerie_catalog (
    id            SERIAL PRIMARY KEY,
    product_url   TEXT UNIQUE NOT NULL,     -- https://nichegallerie.com/product/...
    gtin          TEXT,                     -- barcode (only when their SKU is a real barcode)
    sku           TEXT,                     -- their SKU as written on the site
    name          TEXT,                     -- e.g. "Amouage Decision 42 Man Extrait 100ML"
    price_inc_tax NUMERIC(10,2),            -- price shown on the site (UAE prices include 5% VAT)
    price         NUMERIC(10,2),            -- same price without VAT (price_inc_tax / 1.05)
    stock         TEXT,                     -- In stock / Out of stock / Not on site
    volume        TEXT,                     -- e.g. "100 ml" (read from the name)
    updated_at    TIMESTAMP DEFAULT NOW()
);
"""

NG_UPSERT = """
INSERT INTO nichegallerie_catalog (product_url, gtin, sku, name, price_inc_tax, price, stock, volume)
VALUES %s
ON CONFLICT (product_url) DO UPDATE SET
    gtin = EXCLUDED.gtin, sku = EXCLUDED.sku, name = EXCLUDED.name,
    price_inc_tax = EXCLUDED.price_inc_tax, price = EXCLUDED.price,
    stock = EXCLUDED.stock, volume = EXCLUDED.volume, updated_at = NOW();
"""


def ng_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    ng_log("[NG-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ---------------------------------------------------------------- reading the API
def ng_item(p):
    """One WooCommerce product -> our catalog fields."""
    name = htmllib.unescape(re.sub(r"<[^>]+>", "", p.get("name") or "")).strip()
    prices = p.get("prices") or {}
    minor = int(prices.get("currency_minor_unit") or 0)          # 2 -> price "65000" means 650.00
    raw = prices.get("price") or prices.get("sale_price") or prices.get("regular_price")
    try:
        price_inc = round(int(raw) / (10 ** minor), 2) if raw not in (None, "") else None
    except ValueError:
        price_inc = None
    if not price_inc:
        price_inc = None
    sku = (p.get("sku") or "").strip() or None
    digits = re.sub(r"\D", "", sku or "")
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*ml\b", name, re.I)               # "100mL" -> "100 ml"
    return {
        "url": p.get("permalink"),
        "gtin": digits if sku and digits == sku and len(digits) >= 8 else None,   # only a pure barcode SKU
        "sku": sku,
        "name": name,
        "price_inc_tax": price_inc,
        "price": round(price_inc / 1.05, 2) if price_inc else None,
        "stock": "In stock" if p.get("is_in_stock") else "Out of stock",
        "volume": f"{m.group(1).replace(',', '')} ml" if m else None,
    }


def ng_get(page):
    """One page of products (retries when the site is busy). Returns (products, total_pages)."""
    for attempt, wait in enumerate([0, 10, 30, 90]):
        if wait:
            ng_log(f"[NG] page {page}: retry {attempt}/3 in {wait}s")
            time.sleep(wait)
        try:
            r = requests.get(NG_API, params={"per_page": NG_PER_PAGE, "page": page},
                             headers=NG_HEADERS, timeout=60)
        except requests.RequestException as ex:
            ng_log(f"[NG] page {page}: network error {ex}")
            continue
        if r.status_code == 429 or r.status_code >= 500:
            ng_log(f"[NG] page {page}: HTTP {r.status_code} (busy)")
            continue
        if r.status_code == 400 and page > 1:                  # asked past the last page
            return [], page - 1
        if r.status_code != 200:
            raise RuntimeError(f"Niche Gallerie answered HTTP {r.status_code}: {r.text[:300]}")
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"Not a product list (blocked or API switched off): {r.text[:200]}")
        return data if isinstance(data, list) else [], int(r.headers.get("X-WP-TotalPages") or 0)
    return None, 0


# ---------------------------------------------------------------- full download
def ng_run():
    started = time.time()
    st = {"status": "running", "pages": 0, "saved": 0, "in_stock": 0, "out_of_stock": 0, "incomplete": False}
    ng_state["last"] = st
    conn = ng_conn()
    cur = conn.cursor()
    try:
        cur.execute(NG_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()
        for page in range(1, NG_MAX_PAGES + 1):
            items, total_pages = ng_get(page)
            if items is None:
                st["incomplete"] = True
                ng_log(f"[NG] page {page}: gave up -> stopping (run is incomplete)")
                break
            if page == 1:
                st["site_pages"] = total_pages
                ng_log(f"[NG] Site has {total_pages} pages of {NG_PER_PAGE}")
            rows = []
            for p in items:
                it = ng_item(p)
                if not it["url"] or not it["name"]:
                    continue
                rows.append((it["url"], it["gtin"], it["sku"], it["name"], it["price_inc_tax"], it["price"],
                             it["stock"], it["volume"]))
                st["in_stock" if it["stock"] == "In stock" else "out_of_stock"] += 1
            if rows:
                execute_values(cur, NG_UPSERT, rows)
                conn.commit()
            st["pages"] = page
            st["saved"] += len(rows)
            st["seconds"] = round(time.time() - started, 1)
            ng_log(f"[NG] page {page}/{total_pages or '?'}: {len(rows)} saved (total {st['saved']})")
            if not items or (total_pages and page >= total_pages):
                break
            time.sleep(NG_DELAY)
        if not st["incomplete"] and st["saved"] > 0:          # products gone from the site
            cur.execute("UPDATE nichegallerie_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            st["marked_not_on_site"] = cur.rowcount
            conn.commit()
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        ng_log(f"[NG-DONE] {st}")


def ng_worker():
    try:
        ng_run()
    except Exception as ex:
        ng_log(f"[NG-ERROR] {ex}\n{traceback.format_exc()}")
        ng_state["last"] = {**(ng_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        ng_state["running"] = False
        ng_lock.release()


# ---------------------------------------------------------------- routes
def ng_secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


@nichegallerie_bp.route("/nichegallerie/test")
def ng_test_route():
    """Read pages 1 and 2 only - shows whether the product list is reachable and what is read."""
    if not ng_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        a, total = ng_get(1)
        b, _ = ng_get(2)
    except RuntimeError as ex:
        ng_log(f"[NG-TEST] {ex}")
        return jsonify({"ok": False, "error": str(ex)})
    a = [ng_item(p) for p in (a or [])]
    b = [ng_item(p) for p in (b or [])]
    result = {"ok": True, "site_pages": total, "page1_products": len(a), "page2_products": len(b),
              "page2_new_products": len({x["url"] for x in b} - {x["url"] for x in a}),
              "page1_first_3": a[:3], "page2_first_3": b[:3]}
    ng_log(f"[NG-TEST] {result}")
    return jsonify(result)


@nichegallerie_bp.route("/nichegallerie/run")
def ng_run_route():
    if not ng_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not ng_lock.acquire(blocking=False):
        return jsonify({"error": "Niche Gallerie download already running"}), 409
    ng_state["running"] = True
    ng_log("[NG-RUN] Started")
    threading.Thread(target=ng_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/nichegallerie/status"}), 202


@nichegallerie_bp.route("/nichegallerie/status")
def ng_status_route():
    return jsonify({"running": ng_state["running"], "last": ng_state["last"]})
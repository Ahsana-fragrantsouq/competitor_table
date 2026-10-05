"""
Al Injaz Perfumes catalog (alinjazperfumes.com -> Postgres french_fragrance_db, table alinjaz_catalog)
Plugged into app.py as a Blueprint.

Al Injaz runs on WordPress + WooCommerce. WooCommerce has a public product list (no key needed):
    GET https://www.alinjazperfumes.com/wp-json/wc/store/v1/products?per_page=100&page=1, 2, 3 ...
Each product: name, permalink (link), sku, prices (in fils -> divided by 100), is_in_stock.

Same fields as the other catalogs: Product URL, GTIN, Name, Price Inc Tax, Price, Stock, Volume (+ SKU).

Routes (open in the browser):
  GET /alinjaz/test?secret=XXX    read pages 1 and 2 only and SHOW what was found (run this first)
  GET /alinjaz/run?secret=XXX     download all products into alinjaz_catalog (background)
  GET /alinjaz/status             progress

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

alinjaz_bp = Blueprint("alinjaz", __name__)

AJ_SITE = "https://www.alinjazperfumes.com"
AJ_API = AJ_SITE + "/wp-json/wc/store/v1/products"    # WooCommerce Store API (public)
AJ_PER_PAGE = 100                                     # WooCommerce maximum per request
AJ_MAX_PAGES = 300
AJ_DELAY = 1.0                                        # seconds between requests (be gentle)
AJ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept": "application/json",
}

aj_lock = threading.Lock()
aj_state = {"running": False, "last": None}


def aj_log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- database
AJ_CREATE = """
CREATE TABLE IF NOT EXISTS alinjaz_catalog (
    id            SERIAL PRIMARY KEY,
    product_url   TEXT UNIQUE NOT NULL,     -- https://www.alinjazperfumes.com/product/...
    gtin          TEXT,                     -- barcode (only when their SKU is a real barcode)
    sku           TEXT,                     -- their SKU as written on the site
    name          TEXT,                     -- e.g. "Creed Oud Zarian Eau de Parfum 100mL"
    price_inc_tax NUMERIC(10,2),            -- price shown on the site (UAE prices include 5% VAT)
    price         NUMERIC(10,2),            -- same price without VAT (price_inc_tax / 1.05)
    stock         TEXT,                     -- In stock / Out of stock / Not on site
    volume        TEXT,                     -- e.g. "100 ml" (read from the name)
    updated_at    TIMESTAMP DEFAULT NOW()
);
"""

AJ_UPSERT = """
INSERT INTO alinjaz_catalog (product_url, gtin, sku, name, price_inc_tax, price, stock, volume)
VALUES %s
ON CONFLICT (product_url) DO UPDATE SET
    gtin = EXCLUDED.gtin, sku = EXCLUDED.sku, name = EXCLUDED.name,
    price_inc_tax = EXCLUDED.price_inc_tax, price = EXCLUDED.price,
    stock = EXCLUDED.stock, volume = EXCLUDED.volume, updated_at = NOW();
"""


def aj_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    aj_log("[AJ-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ---------------------------------------------------------------- reading the API
def aj_item(p):
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


def aj_get(page):
    """One page of products (retries when the site is busy). Returns (products, total_pages)."""
    for attempt, wait in enumerate([0, 10, 30, 90]):
        if wait:
            aj_log(f"[AJ] page {page}: retry {attempt}/3 in {wait}s")
            time.sleep(wait)
        try:
            r = requests.get(AJ_API, params={"per_page": AJ_PER_PAGE, "page": page},
                             headers=AJ_HEADERS, timeout=60)
        except requests.RequestException as ex:
            aj_log(f"[AJ] page {page}: network error {ex}")
            continue
        if r.status_code == 429 or r.status_code >= 500:
            aj_log(f"[AJ] page {page}: HTTP {r.status_code} (busy)")
            continue
        if r.status_code == 400 and page > 1:                  # asked past the last page
            return [], page - 1
        if r.status_code != 200:
            raise RuntimeError(f"Al Injaz answered HTTP {r.status_code}: {r.text[:300]}")
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"Not a product list (blocked or API switched off): {r.text[:200]}")
        return data if isinstance(data, list) else [], int(r.headers.get("X-WP-TotalPages") or 0)
    return None, 0


# ---------------------------------------------------------------- full download
def aj_run():
    started = time.time()
    st = {"status": "running", "pages": 0, "saved": 0, "in_stock": 0, "out_of_stock": 0, "incomplete": False}
    aj_state["last"] = st
    conn = aj_conn()
    cur = conn.cursor()
    try:
        cur.execute(AJ_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()
        for page in range(1, AJ_MAX_PAGES + 1):
            items, total_pages = aj_get(page)
            if items is None:
                st["incomplete"] = True
                aj_log(f"[AJ] page {page}: gave up -> stopping (run is incomplete)")
                break
            if page == 1:
                st["site_pages"] = total_pages
                aj_log(f"[AJ] Site has {total_pages} pages of {AJ_PER_PAGE}")
            rows = []
            for p in items:
                it = aj_item(p)
                if not it["url"] or not it["name"]:
                    continue
                rows.append((it["url"], it["gtin"], it["sku"], it["name"], it["price_inc_tax"], it["price"],
                             it["stock"], it["volume"]))
                st["in_stock" if it["stock"] == "In stock" else "out_of_stock"] += 1
            if rows:
                execute_values(cur, AJ_UPSERT, rows)
                conn.commit()
            st["pages"] = page
            st["saved"] += len(rows)
            st["seconds"] = round(time.time() - started, 1)
            aj_log(f"[AJ] page {page}/{total_pages or '?'}: {len(rows)} saved (total {st['saved']})")
            if not items or (total_pages and page >= total_pages):
                break
            time.sleep(AJ_DELAY)
        if not st["incomplete"] and st["saved"] > 0:          # products gone from the site
            cur.execute("UPDATE alinjaz_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            st["marked_not_on_site"] = cur.rowcount
            conn.commit()
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        aj_log(f"[AJ-DONE] {st}")


def aj_worker():
    try:
        aj_run()
    except Exception as ex:
        aj_log(f"[AJ-ERROR] {ex}\n{traceback.format_exc()}")
        aj_state["last"] = {**(aj_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        aj_state["running"] = False
        aj_lock.release()


# ---------------------------------------------------------------- routes
def aj_secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


@alinjaz_bp.route("/alinjaz/test")
def aj_test_route():
    """Read pages 1 and 2 only - shows whether the product list is reachable and what is read."""
    if not aj_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        a, total = aj_get(1)
        b, _ = aj_get(2)
    except RuntimeError as ex:
        aj_log(f"[AJ-TEST] {ex}")
        return jsonify({"ok": False, "error": str(ex)})
    a = [aj_item(p) for p in (a or [])]
    b = [aj_item(p) for p in (b or [])]
    result = {"ok": True, "site_pages": total, "page1_products": len(a), "page2_products": len(b),
              "page2_new_products": len({x["url"] for x in b} - {x["url"] for x in a}),
              "page1_first_3": a[:3], "page2_first_3": b[:3]}
    aj_log(f"[AJ-TEST] {result}")
    return jsonify(result)


@alinjaz_bp.route("/alinjaz/run")
def aj_run_route():
    if not aj_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not aj_lock.acquire(blocking=False):
        return jsonify({"error": "Al Injaz download already running"}), 409
    aj_state["running"] = True
    aj_log("[AJ-RUN] Started")
    threading.Thread(target=aj_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/alinjaz/status"}), 202


@alinjaz_bp.route("/alinjaz/status")
def aj_status_route():
    return jsonify({"running": aj_state["running"], "last": aj_state["last"]})
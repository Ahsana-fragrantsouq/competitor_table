"""
V Perfumes catalog (vperfumes.com UAE -> Postgres french_fragrance_db, table vperfumes_catalog)
Plugged into app.py as a Blueprint.

V Perfumes is NOT Shopify, but its website loads products from its own API (the "Load More" button):
    POST https://api.vperfumes.com/api/v1/w/product-lisiting-hybrid
    body {"page": 1, "limit": 24, "category": ["perfumes"], ...}
Each product has: sku (= barcode / GTIN), name, slug (-> link), AED selling price, inStock.
Only the "perfumes" category is read (gift sets are a separate category), and any name with
"Set" / "Gift" is skipped as well -> perfumes only.

Same fields as the Airtable FF Catalog: Product URL, GTIN, Name, Price Inc Tax, Price, Stock, Volume.

Routes (open in the browser):
  GET /vperfumes/test?secret=XXX    read API pages 1 and 2 only and SHOW what was found (run this first)
  GET /vperfumes/run?secret=XXX     download all perfumes into vperfumes_catalog (background)
  GET /vperfumes/status             progress

Env vars (already set on Render):
  FF_DATABASE_URL = Postgres URL ending in /french_fragrance_db
  RUN_SECRET      = same secret as the other /run URLs
  VP_API_HEADERS  = (optional, normally NOT needed) extra/override request headers as JSON
"""

import os
import re
import json
import time
import uuid
import base64
import random
import threading
import traceback

import requests
import psycopg2
from psycopg2.extras import execute_values
from flask import Blueprint, request, jsonify

vperfumes_bp = Blueprint("vperfumes", __name__)

VP_SITE = "https://www.vperfumes.com"
VP_PRODUCT = VP_SITE + "/ae-en/product/"            # + slug = product page link
VP_API = "https://api.vperfumes.com/api/v1/w/product-lisiting-hybrid"   # ("lisiting" = their spelling)
VP_LIMIT = 24                                        # products per request (same as the website)
VP_MAX_PAGES = 1000                                  # safety stop (8,500 / 24 = ~355 pages)
VP_DELAY = 1.0                                       # seconds between requests (be gentle)
VP_SKIP = re.compile(r"\b(set|gift)\b", re.I)        # not perfumes-only -> skipped
def vp_device_token():
    """The website gives every visitor a 'device token' = base64(random id + time in ms), e.g.
    'd0384d42-dcac-4aba-ae94-88d33d605e79' + '1790833281675' -> 'ZDAzODRk...NQ=='. We make one the same way."""
    raw = f"{uuid.uuid4()}{int(time.time() * 1000)}"
    return base64.b64encode(raw.encode()).decode()


# Same headers the V Perfumes website sends (copied from Chrome -> Network -> Request headers)
VP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": VP_SITE,
    "Referer": VP_SITE + "/",
    "devicetoken": vp_device_token(),          # required - without it: 401 "Device token is required"
    "devicetype": "web",
    "lang": "en",
    "store": "AE",                             # UAE store -> AED prices
    "ostype": json.dumps({"type": "desktop", "os": "windows", "source": "browser"}),
    "unbxd-user-id": f"uid-{int(time.time() * 1000)}-{random.randint(10000, 99999)}",   # their search tool's visitor id
}
try:   # extra headers from Render -> Environment, only if the API needs something special
    VP_HEADERS.update(json.loads(os.environ.get("VP_API_HEADERS") or "{}"))
except ValueError:
    print("[VP] VP_API_HEADERS is not valid JSON - ignored", flush=True)


def vp_body(page):
    """Same request body the website sends when you click "Load More" (Perfumes category, UAE)."""
    return {"page": page, "limit": VP_LIMIT, "category": ["perfumes"], "collection": None, "brand": None,
            "note": None, "sort": "", "discount": None, "fragranceFamily": [], "gender": [], "occasion": [],
            "priceFrom": None, "priceTo": None, "rating": None, "search": "", "sellerId": None,
            "uc_param": "", "volume": None}


vp_lock = threading.Lock()                           # only one V Perfumes job at a time
vp_state = {"running": False, "last": None}          # shown at /vperfumes/status


def vp_log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- database
# Same fields as the Airtable "FF Catalog": Product URL, GTIN, Name, Price Inc Tax, Price, Stock, Volume
VP_CREATE = """
CREATE TABLE IF NOT EXISTS vperfumes_catalog (
    id            SERIAL PRIMARY KEY,
    product_url   TEXT UNIQUE NOT NULL,     -- https://www.vperfumes.com/ae-en/product/...
    gtin          TEXT,                     -- barcode (V Perfumes "sku")
    name          TEXT,                     -- e.g. "Burberry EDP For Women 100ML"
    price_inc_tax NUMERIC(10,2),            -- price shown on the site (UAE prices include 5% VAT)
    price         NUMERIC(10,2),            -- same price without VAT (price_inc_tax / 1.05)
    stock         TEXT,                     -- In stock / Out of stock / Not on site
    volume        TEXT,                     -- e.g. "100 ml" (read from the name)
    updated_at    TIMESTAMP DEFAULT NOW()
);
ALTER TABLE vperfumes_catalog ADD COLUMN IF NOT EXISTS gtin TEXT;
ALTER TABLE vperfumes_catalog ADD COLUMN IF NOT EXISTS price_inc_tax NUMERIC(10,2);
ALTER TABLE vperfumes_catalog DROP COLUMN IF EXISTS old_price;      -- not needed (only if an older version made them)
ALTER TABLE vperfumes_catalog DROP COLUMN IF EXISTS discount_pct;
"""

VP_UPSERT = """
INSERT INTO vperfumes_catalog (product_url, gtin, name, price_inc_tax, price, stock, volume)
VALUES %s
ON CONFLICT (product_url) DO UPDATE SET
    gtin          = EXCLUDED.gtin,
    name          = EXCLUDED.name,
    price_inc_tax = EXCLUDED.price_inc_tax,
    price         = EXCLUDED.price,
    stock         = EXCLUDED.stock,
    volume        = EXCLUDED.volume,
    updated_at    = NOW();
"""


def vp_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    vp_log("[VP-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ---------------------------------------------------------------- reading the API
def to_number(v):
    try:
        return round(float(str(v).replace(",", "")), 2) if v not in (None, "") else None
    except ValueError:
        return None


def vp_item(p):
    """One product from the API -> our FF-Catalog-style fields."""
    name = (((p.get("name") or {}).get("text") or {}).get("en") or "").strip()
    aed = (p.get("priceDisplay") or {}).get("aed") or {}
    price_inc = to_number(aed.get("selling")) or to_number(p.get("selling_price"))   # price customers pay (VAT incl.)
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*ml\b", name, re.I)                       # "100ML" -> "100 ml"
    return {
        "url": VP_PRODUCT + (p.get("slug") or ""),
        "gtin": str(p.get("sku") or p.get("_id") or "").strip() or None,          # V Perfumes SKU = barcode
        "name": name,
        "price_inc_tax": price_inc,
        "price": round(price_inc / 1.05, 2) if price_inc else None,               # without 5% VAT
        "stock": "In stock" if p.get("inStock") else "Out of stock",
        "volume": f"{m.group(1).replace(',', '')} ml" if m else None,
    }


def vp_get(page):
    """Ask the API for one page. Returns (products, last_page, total_items)."""
    for attempt, wait in enumerate([0, 10, 30, 90]):
        if wait:
            vp_log(f"[VP] page {page}: retry {attempt}/3 in {wait}s")
            time.sleep(wait)
        try:
            r = requests.post(VP_API, json=vp_body(page), headers=VP_HEADERS, timeout=60)
        except requests.RequestException as ex:
            vp_log(f"[VP] page {page}: network error {ex}")
            continue
        if r.status_code == 429 or r.status_code >= 500:
            vp_log(f"[VP] page {page}: HTTP {r.status_code} (busy)")
            continue
        if r.status_code != 200:
            # show the API's own answer, so we know what it wants (e.g. a missing header)
            raise RuntimeError(f"API answered HTTP {r.status_code}: {r.text[:500]}")
        data = r.json()
        if not data.get("success", True):
            raise RuntimeError(f"API error: {json.dumps(data)[:500]}")
        prods = ((data.get("result") or {}).get("products")) or {}
        return prods.get("product_items") or [], bool(prods.get("last_page")), prods.get("total_items")
    return None, False, None


# ---------------------------------------------------------------- full download
def vp_run():
    started = time.time()
    st = {"status": "running", "pages": 0, "saved": 0, "skipped_sets": 0, "in_stock": 0,
          "out_of_stock": 0, "site_total": None, "incomplete": False}
    vp_state["last"] = st

    conn = vp_conn()
    cur = conn.cursor()
    try:
        cur.execute(VP_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()

        seen = set()
        for page in range(1, VP_MAX_PAGES + 1):
            items, last_page, total = vp_get(page)
            if items is None:
                st["incomplete"] = True
                vp_log(f"[VP] page {page}: gave up -> stopping (run is incomplete)")
                break
            if page == 1:
                st["site_total"] = total
                vp_log(f"[VP] API says {total} perfumes")
            items = [vp_item(p) for p in items]
            items = [it for it in items if it["name"] and it["url"] not in seen]
            if not items:
                vp_log(f"[VP] page {page}: no new products -> end of list")
                break

            rows = []
            for it in items:
                seen.add(it["url"])
                if VP_SKIP.search(it["name"]):
                    st["skipped_sets"] += 1
                    continue
                rows.append((it["url"], it["gtin"], it["name"], it["price_inc_tax"], it["price"],
                             it["stock"], it["volume"]))
                st["in_stock" if it["stock"] == "In stock" else "out_of_stock"] += 1
            if rows:
                execute_values(cur, VP_UPSERT, rows)
                conn.commit()
            st["pages"] = page
            st["saved"] += len(rows)
            st["seconds"] = round(time.time() - started, 1)
            vp_log(f"[VP] page {page}: {len(items)} products, {len(rows)} saved "
                   f"(total {st['saved']}, sets skipped {st['skipped_sets']})")
            if last_page:
                vp_log("[VP] API says last page -> done")
                break
            time.sleep(VP_DELAY)

        # perfumes not seen in a COMPLETE run are no longer on the site
        if not st["incomplete"] and st["saved"] > 0:
            cur.execute("UPDATE vperfumes_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            st["marked_not_on_site"] = cur.rowcount
            conn.commit()
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        vp_log(f"[VP-DONE] {st}")


def vp_worker():
    try:
        vp_run()
    except Exception as ex:
        vp_log(f"[VP-ERROR] {ex}\n{traceback.format_exc()}")
        vp_state["last"] = {**(vp_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        vp_state["running"] = False
        vp_lock.release()


# ---------------------------------------------------------------- routes
def vp_secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


@vperfumes_bp.route("/vperfumes/test")
def vp_test_route():
    """Read API pages 1 and 2 only. Shows whether the API answers, and what is read from each product."""
    if not vp_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        a, last1, total = vp_get(1)
        b, _, _ = vp_get(2)
    except RuntimeError as ex:
        vp_log(f"[VP-TEST] {ex}")
        return jsonify({"ok": False, "error": str(ex)})
    a = [vp_item(p) for p in (a or [])]
    b = [vp_item(p) for p in (b or [])]
    urls_a = {x["url"] for x in a}
    result = {
        "ok": True,
        "site_total": total,
        "page1_products": len(a),
        "page2_products": len(b),
        "page2_new_products": len([x for x in b if x["url"] not in urls_a]),
        "page1_first_3": a[:3],
        "page2_first_3": b[:3],
        "sets_on_page1": [x["name"] for x in a if VP_SKIP.search(x["name"])],
    }
    vp_log(f"[VP-TEST] {result}")
    return jsonify(result)


@vperfumes_bp.route("/vperfumes/run")
def vp_run_route():
    if not vp_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not vp_lock.acquire(blocking=False):
        return jsonify({"error": "V Perfumes download already running"}), 409
    vp_state["running"] = True
    vp_log("[VP-RUN] Started")
    threading.Thread(target=vp_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/vperfumes/status"}), 202


@vperfumes_bp.route("/vperfumes/status")
def vp_status_route():
    return jsonify({"running": vp_state["running"], "last": vp_state["last"]})
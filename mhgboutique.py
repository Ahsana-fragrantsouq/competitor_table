"""
MHG Boutique catalog (mhgboutique.com -> Postgres french_fragrance_db, table mhgboutique_catalog)
Plugged into app.py as a Blueprint. Made from perfumedubai.py (same Shopify way of reading).

MHG Boutique (Hind Al Oud, Anfasic Dokhoon ...) is a Shopify store, so its product list is public:
    https://www.mhgboutique.com/products.json?limit=250&page=1, 2, 3 ...   (whole shop)
ONLY PERFUMES are saved:
  1. Shopify "product_type" must have a word of MH_TYPE_WORDS (theirs is "Perfumes | ...") -> incense, khaltat,
     beauty, gifting ... are left out
  2. then products whose title has a word of MH_SKIP_WORDS are left out (body spray, gift set ... - their body
     sprays are ALSO typed "Perfumes")
  /mhgboutique/test shows the product types seen + skipped titles -> check it first.
Size: often NOT in the title ("Mrs. Always Shay Parfum") -> taken from the tags ("75ml") when needed.
Their SKUs (e.g. 10021497) are shop numbers, not barcodes -> a SKU counts as barcode only with 12-14 digits.
One row per variant (a product with 50ml + 100ml = 2 rows).
Brand = Shopify "vendor" (e.g. "Hind Al Oud"), name = product title + size, link opens the right size.

Routes (open in the browser):
  GET /mhgboutique/test?secret=XXX    read page 1 only and SHOW what was found (run this first)
  GET /mhgboutique/run?secret=XXX     download everything into mhgboutique_catalog (background, ~1 min)
  GET /mhgboutique/status             progress

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

mhgboutique_bp = Blueprint("mhgboutique", __name__)

MH_BASE = "https://www.mhgboutique.com"
MH_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
              "Accept": "application/json"}
MH_MAX_PAGES = 100              # Shopify limit: max 100 pages x 250 = 25,000 products
MH_RETRY_WAITS = [10, 30, 60, 120]
# ONLY PERFUMES - change these lists if /mhgboutique/test shows wrong products:
MH_COLLECTIONS = []                  # none: the whole shop is read, perfumes picked by product type (below)
# keep a product only when its Shopify product_type has one of these words ("perfume*" = word start -> "Perfumes")
MH_TYPE_WORDS = ["perfume*", "parfum*", "fragrance*"]
# always left out: the title has one of these WHOLE words (any capitals). "moisturi*" = word start.
MH_SKIP_WORDS = ["gift set", "giftset", "coffret", "set", "body mist", "hair mist", "body spray", "deodorant",
                 "deo", "body lotion", "body cream", "shower gel", "bath", "soap", "candle", "diffuser",
                 "lipstick", "mascara", "foundation", "serum", "moisturi*", "cleanser", "bag", "pouch",
                 "gift card", "sample"]

mh_lock = threading.Lock()
mh_state = {"running": False, "last": None, "source": None}   # source = collection used (or "all products")


def mh_log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- database
MH_CREATE = """
CREATE TABLE IF NOT EXISTS mhgboutique_catalog (
    id            SERIAL PRIMARY KEY,
    variant_id    BIGINT UNIQUE NOT NULL,   -- Shopify variant id (one row per size)
    product_url   TEXT,                     -- https://www.mhgboutique.com/products/...?variant=...
    gtin          TEXT,                     -- barcode, only when their SKU is a real barcode
    sku           TEXT,                     -- their SKU as written in Shopify
    brand         TEXT,                     -- Shopify vendor, e.g. "Hind Al Oud"
    name          TEXT,                     -- "Mrs. Always Shay Parfum"
    product_type  TEXT,                     -- Shopify product type, e.g. "Eau de Parfum"
    price_inc_tax NUMERIC(10,2),            -- price shown on the site (UAE prices include 5% VAT)
    price         NUMERIC(10,2),            -- without VAT (price_inc_tax / 1.05)
    stock         TEXT,                     -- In stock / Out of stock / Not on site
    volume        TEXT,                     -- e.g. "100 ml"
    updated_at    TIMESTAMP DEFAULT NOW()
);
"""

MH_UPSERT = """
INSERT INTO mhgboutique_catalog (variant_id, product_url, gtin, sku, brand, name, product_type,
                              price_inc_tax, price, stock, volume)
VALUES %s
ON CONFLICT (variant_id) DO UPDATE SET
    product_url = EXCLUDED.product_url, gtin = EXCLUDED.gtin, sku = EXCLUDED.sku, brand = EXCLUDED.brand,
    name = EXCLUDED.name, product_type = EXCLUDED.product_type, price_inc_tax = EXCLUDED.price_inc_tax,
    price = EXCLUDED.price, stock = EXCLUDED.stock, volume = EXCLUDED.volume, updated_at = NOW();
"""


def mh_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    mh_log("[MH-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ---------------------------------------------------------------- reading products
def mh_volume(*texts):
    """'100ml' / 'Oud Wood EDP 100 ML' -> '100 ml'"""
    for t in texts:
        m = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", str(t or ""), re.I)
        if m:
            return f"{m.group(1)} ml"
    return None


def mh_is_perfume(p, whole_shop):
    """True = keep: product_type is a perfume type (MH_TYPE_WORDS) and the title has no word of MH_SKIP_WORDS.
    whole_shop is not used (kept so the code stays like perfumedubai.py)."""
    text = f" {p.get('title') or ''} ".lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)                 # "Gift-Set" -> " gift set "
    for w in MH_SKIP_WORDS:                                 # whole words only ("set" does not hit "settings")
        pat = rf"\b{re.escape(w.rstrip('*'))}" + ("" if w.endswith("*") else r"\b")
        if re.search(pat, text):
            return False
    ptype = re.sub(r"[^a-z0-9]+", " ", (p.get("product_type") or "").lower())
    return any(re.search(rf"\b{re.escape(w.rstrip('*'))}" + ("" if w.endswith("*") else r"\b"), ptype)
               for w in MH_TYPE_WORDS)


def mh_items(products, whole_shop=False):
    """Shopify products -> one dict per variant (perfumes only, see mh_is_perfume)."""
    out = []
    for p in products:
        if not mh_is_perfume(p, whole_shop):
            continue
        title = (p.get("title") or "").strip()
        variants = p.get("variants", [])
        for v in variants:
            vtitle = (v.get("title") or "").strip()
            name = title if vtitle.lower() in ("", "default title") else f"{title} - {vtitle}"
            url = f"{MH_BASE}/products/{p.get('handle')}"
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
                "gtin": digits if sku and digits == sku and 12 <= len(digits) <= 14 else None,   # 8-digit SKUs = shop numbers
                "sku": sku, "brand": (p.get("vendor") or "").strip() or None, "name": name,
                "product_type": (p.get("product_type") or "").strip() or None,
                "price_inc_tax": price_inc, "price": round(price_inc / 1.05, 2) if price_inc else None,
                "stock": "In stock" if v.get("available") else "Out of stock",
                "volume": mh_volume(vtitle, title, *(p.get("tags") or [])),   # tags: "75ml"
            })
    return out


def mh_source():
    """Path of the perfume list: /collections/<first of MH_COLLECTIONS with products>/products.json,
    or /products.json (whole shop, filtered by product type) when none exists. Remembered in mh_state."""
    for handle in MH_COLLECTIONS:
        path = f"/collections/{handle}/products.json"
        try:
            r = requests.get(f"{MH_BASE}{path}", params={"limit": 1}, headers=MH_HEADERS, timeout=60)
            n = len(r.json().get("products", [])) if r.status_code == 200 else 0
        except (requests.RequestException, ValueError) as ex:
            mh_log(f"[MH] collection '{handle}': not readable ({str(ex)[:80]})")
            continue
        mh_log(f"[MH] collection '{handle}': HTTP {r.status_code}, {n} product(s) on a test read")
        if n:
            mh_state["source"] = path
            return path
    mh_log("[MH] whole shop /products.json, perfumes picked by product type (MH_TYPE_WORDS)")
    mh_state["source"] = "/products.json"
    return "/products.json"


def mh_get(page, path="/products.json"):
    """One page of products.json (retries when busy). Returns list of products, [] at the end, None on failure."""
    for attempt, wait in enumerate([0] + MH_RETRY_WAITS):
        if wait:
            mh_log(f"[MH] page {page}: retry {attempt}/{len(MH_RETRY_WAITS)} in {wait}s")
            time.sleep(wait)
        try:
            r = requests.get(f"{MH_BASE}{path}", params={"limit": 250, "page": page},
                             headers=MH_HEADERS, timeout=60)
        except requests.RequestException as ex:
            mh_log(f"[MH] page {page}: network error {ex}")
            continue
        if r.status_code == 429 or r.status_code >= 500:
            mh_log(f"[MH] page {page}: HTTP {r.status_code} (busy)")
            continue
        if r.status_code in (400, 404):
            return []
        if r.status_code != 200:
            raise RuntimeError(f"MHG Boutique answered HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json().get("products", [])
        except ValueError:
            raise RuntimeError(f"Not a product list (blocked?): {r.text[:200]}")
    return None


# ---------------------------------------------------------------- full download
def mh_run():
    started = time.time()
    st = {"status": "running", "pages": 0, "products": 0, "saved": 0, "in_stock": 0, "out_of_stock": 0,
          "incomplete": False}
    mh_state["last"] = st
    conn = mh_conn()
    cur = conn.cursor()
    try:
        cur.execute(MH_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()
        path = mh_source()                                  # perfume collection (or whole shop)
        whole_shop = path == "/products.json"
        st["source"] = path
        for page in range(1, MH_MAX_PAGES + 1):
            products = mh_get(page, path)
            if products is None:
                st["incomplete"] = True
                mh_log(f"[MH] page {page}: gave up -> stopping (run is incomplete)")
                break
            if not products:
                break
            items = mh_items(products, whole_shop)
            st["skipped_not_perfume"] = st.get("skipped_not_perfume", 0) + sum(
                1 for p in products if not mh_is_perfume(p, whole_shop))
            rows = [(i["variant_id"], i["url"], i["gtin"], i["sku"], i["brand"], i["name"], i["product_type"],
                     i["price_inc_tax"], i["price"], i["stock"], i["volume"]) for i in items if i["variant_id"]]
            if rows:
                execute_values(cur, MH_UPSERT, rows)
                conn.commit()
            st["pages"] = page
            st["products"] += len(products)
            st["saved"] += len(rows)
            st["in_stock"] += sum(i["stock"] == "In stock" for i in items)
            st["out_of_stock"] += sum(i["stock"] != "In stock" for i in items)
            st["seconds"] = round(time.time() - started, 1)
            mh_log(f"[MH] page {page}: {len(products)} products, {len(rows)} variants saved (total {st['saved']})")
            if len(products) < 250:
                break
            time.sleep(1.5)
        if not st["incomplete"] and st["saved"]:             # products gone from the site
            cur.execute("UPDATE mhgboutique_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            st["marked_not_on_site"] = cur.rowcount
            conn.commit()
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        mh_log(f"[MH-DONE] {st}")


def mh_worker():
    try:
        mh_run()
    except Exception as ex:
        mh_log(f"[MH-ERROR] {ex}\n{traceback.format_exc()}")
        mh_state["last"] = {**(mh_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        mh_state["running"] = False
        mh_lock.release()


# ---------------------------------------------------------------- routes
def mh_secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


@mhgboutique_bp.route("/mhgboutique/test")
def mh_test_route():
    """Read page 1 only - shows whether Render can read MHG Boutique and what is read."""
    if not mh_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        path = mh_source()
        whole_shop = path == "/products.json"
        products = mh_get(1, path)
    except RuntimeError as ex:
        mh_log(f"[MH-TEST] {ex}")
        return jsonify({"ok": False, "error": str(ex)})
    items = mh_items(products or [], whole_shop)
    result = {"ok": bool(items), "source": path, "page1_products": len(products or []), "page1_variants": len(items),
              "skipped_not_perfume": [p.get("title") for p in (products or []) if not mh_is_perfume(p, whole_shop)][:30],
              "with_sku": sum(1 for i in items if i["sku"]), "with_barcode": sum(1 for i in items if i["gtin"]),
              "brands_seen": sorted({i["brand"] or "-" for i in items})[:20],
              "product_types": sorted({i["product_type"] or "-" for i in items})[:20],
              "first_3": items[:3]}
    mh_log(f"[MH-TEST] {result}")
    return jsonify(result)


@mhgboutique_bp.route("/mhgboutique/run")
def mh_run_route():
    if not mh_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not mh_lock.acquire(blocking=False):
        return jsonify({"error": "MHG Boutique download already running"}), 409
    mh_state["running"] = True
    mh_log("[MH-RUN] Started")
    threading.Thread(target=mh_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/mhgboutique/status"}), 202


@mhgboutique_bp.route("/mhgboutique/status")
def mh_status_route():
    return jsonify({"running": mh_state["running"], "last": mh_state["last"]})
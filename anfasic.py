"""
Anfasic catalog (anfasic.ae -> Postgres french_fragrance_db, table anfasic_catalog)
Plugged into app.py as a Blueprint. Made from perfumedubai.py (same Shopify way of reading).
Shown in the menu "Brands" (not "Competitors") - the brand's own shop; everything else works like a competitor.
Anfasic (Anfasic Dokhoon) = same group as MHG Boutique; it sells a lot of dokhoon / incense -> strict perfume filter.

Anfasic is a Shopify store, so its product list is public.
ONLY PERFUMES are saved (no make up, skincare, bags, gift sets, body mists ...):
  1. the shop's PERFUME collection is read: the first of AN_COLLECTIONS that has products, e.g.
       https://www.anfasic.ae/collections/perfume/products.json?limit=250&page=1, 2, 3 ...
     (no such collection -> the whole shop /products.json: product type "Perfumes..." (AN_TYPE_WORDS) or a perfume word
      in the title (AN_PERFUME_WORDS))
  2. then products whose title / type has a word of AN_SKIP_WORDS are left out (gift set, body mist, deodorant ...)
  /anfasic/test shows which collection was used + the product types / skipped titles -> check it first.
One row per variant (a product with 50ml + 100ml = 2 rows).
Brand = Shopify "vendor" (e.g. "Anfasic Dokhoon"). Size from the tags ("75ml") when not in the title., name = product title + size, link opens the right size.

Routes (open in the browser):
  GET /anfasic/test?secret=XXX    read page 1 only and SHOW what was found (run this first)
  GET /anfasic/run?secret=XXX     download everything into anfasic_catalog (background, ~1 min)
  GET /anfasic/status             progress

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

anfasic_bp = Blueprint("anfasic", __name__)

AN_BASE = "https://www.anfasic.ae"
AN_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
              "Accept": "application/json"}
AN_MAX_PAGES = 100              # Shopify limit: max 100 pages x 250 = 25,000 products
AN_RETRY_WAITS = [10, 30, 60, 120]
# ONLY PERFUMES - change these lists if /anfasic/test shows wrong products:
AN_COLLECTIONS = ["perfumes", "perfume", "fragrances", "all-fragrances", "fragrance", "all-perfumes"]   # tried in this order
# used only when none of AN_COLLECTIONS exists: a product must have one of these words in its title / type
AN_PERFUME_WORDS = ["perfume", "perfumes", "parfum", "toilette", "cologne", "edp", "edt", "extrait", "fragrance"]
# whole shop: a product type with one of these words is a perfume too ("perfume*" = word start -> "Perfumes | عطور")
AN_TYPE_WORDS = ["perfume*", "parfum*", "fragrance*"]
# always left out: title or product type has one of these WHOLE words (any capitals). "moisturi*" = word start.
AN_SKIP_WORDS = ["gift set", "giftset", "coffret", "set", "body mist", "hair mist", "body spray", "deodorant",
                 "deo", "body lotion", "body cream", "shower gel", "bath", "soap", "candle", "diffuser",
                 "lipstick", "mascara", "foundation", "serum", "moisturi*", "cleanser", "bag", "pouch",
                 "gift card", "sample",
                 # Anfasic Dokhoon home / incense items
                 "oil", "oils", "dakhoon", "dokhoon", "dukhoon", "bakhoor", "bukhoor", "incense", "burner", "mabkhara",
                 "mubakhar", "room spray", "home spray", "car perfume", "car freshener", "kit", "box"]

an_lock = threading.Lock()
an_state = {"running": False, "last": None, "source": None}   # source = collection used (or "all products")


def an_log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- database
AN_CREATE = """
CREATE TABLE IF NOT EXISTS anfasic_catalog (
    id            SERIAL PRIMARY KEY,
    variant_id    BIGINT UNIQUE NOT NULL,   -- Shopify variant id (one row per size)
    product_url   TEXT,                     -- https://www.anfasic.ae/products/...?variant=...
    gtin          TEXT,                     -- barcode, only when their SKU is a real barcode
    sku           TEXT,                     -- their SKU as written in Shopify
    brand         TEXT,                     -- Shopify vendor, e.g. "Anfasic Dokhoon"
    name          TEXT,                     -- "Oud Wood EDP 50 ml"
    product_type  TEXT,                     -- Shopify product type, e.g. "Eau de Parfum"
    price_inc_tax NUMERIC(10,2),            -- price shown on the site (UAE prices include 5% VAT)
    price         NUMERIC(10,2),            -- without VAT (price_inc_tax / 1.05)
    stock         TEXT,                     -- In stock / Out of stock / Not on site
    volume        TEXT,                     -- e.g. "100 ml"
    updated_at    TIMESTAMP DEFAULT NOW()
);
"""

AN_UPSERT = """
INSERT INTO anfasic_catalog (variant_id, product_url, gtin, sku, brand, name, product_type,
                              price_inc_tax, price, stock, volume)
VALUES %s
ON CONFLICT (variant_id) DO UPDATE SET
    product_url = EXCLUDED.product_url, gtin = EXCLUDED.gtin, sku = EXCLUDED.sku, brand = EXCLUDED.brand,
    name = EXCLUDED.name, product_type = EXCLUDED.product_type, price_inc_tax = EXCLUDED.price_inc_tax,
    price = EXCLUDED.price, stock = EXCLUDED.stock, volume = EXCLUDED.volume, updated_at = NOW();
"""


def an_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    an_log("[AN-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ---------------------------------------------------------------- reading products
def an_volume(*texts):
    """'100ml' / 'Oud Wood EDP 100 ML' -> '100 ml'"""
    for t in texts:
        m = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", str(t or ""), re.I)
        if m:
            return f"{m.group(1)} ml"
    return None


def an_is_perfume(p, whole_shop):
    """True = keep this Shopify product. whole_shop = read from /products.json (no perfume collection found)."""
    text = f" {p.get('title') or ''} {p.get('product_type') or ''} ".lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)                 # "Gift-Set" -> " gift set "
    for w in AN_SKIP_WORDS:                                 # whole words only ("set" does not hit "settings")
        pat = rf"\b{re.escape(w.rstrip('*'))}" + ("" if w.endswith("*") else r"\b")
        if re.search(pat, text):
            return False
    if not whole_shop:
        return True
    ptype = re.sub(r"[^a-z0-9]+", " ", (p.get("product_type") or "").lower())
    return (any(re.search(rf"\b{re.escape(w.rstrip('*'))}" + ("" if w.endswith("*") else r"\b"), ptype)
                for w in AN_TYPE_WORDS)
            or any(re.search(rf"\b{w}\b", text) for w in AN_PERFUME_WORDS))


def an_items(products, whole_shop=False):
    """Shopify products -> one dict per variant (perfumes only, see an_is_perfume)."""
    out = []
    for p in products:
        if not an_is_perfume(p, whole_shop):
            continue
        title = (p.get("title") or "").strip()
        variants = p.get("variants", [])
        for v in variants:
            vtitle = (v.get("title") or "").strip()
            name = title if vtitle.lower() in ("", "default title") else f"{title} - {vtitle}"
            url = f"{AN_BASE}/products/{p.get('handle')}"
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
                "volume": an_volume(vtitle, title, *(p.get("tags") or [])),   # tags: "75ml"
            })
    return out


def an_source():
    """Path of the perfume list: /collections/<first of AN_COLLECTIONS with products>/products.json,
    or /products.json (whole shop, filtered by AN_PERFUME_WORDS) when none exists. Remembered in an_state."""
    for handle in AN_COLLECTIONS:
        path = f"/collections/{handle}/products.json"
        try:
            r = requests.get(f"{AN_BASE}{path}", params={"limit": 1}, headers=AN_HEADERS, timeout=60)
            n = len(r.json().get("products", [])) if r.status_code == 200 else 0
        except (requests.RequestException, ValueError) as ex:
            an_log(f"[AN] collection '{handle}': not readable ({str(ex)[:80]})")
            continue
        an_log(f"[AN] collection '{handle}': HTTP {r.status_code}, {n} product(s) on a test read")
        if n:
            an_state["source"] = path
            return path
    an_log("[AN] no perfume collection found -> whole shop /products.json, perfumes picked by words")
    an_state["source"] = "/products.json"
    return "/products.json"


def an_get(page, path="/products.json"):
    """One page of products.json (retries when busy). Returns list of products, [] at the end, None on failure."""
    for attempt, wait in enumerate([0] + AN_RETRY_WAITS):
        if wait:
            an_log(f"[AN] page {page}: retry {attempt}/{len(AN_RETRY_WAITS)} in {wait}s")
            time.sleep(wait)
        try:
            r = requests.get(f"{AN_BASE}{path}", params={"limit": 250, "page": page},
                             headers=AN_HEADERS, timeout=60)
        except requests.RequestException as ex:
            an_log(f"[AN] page {page}: network error {ex}")
            continue
        if r.status_code == 429 or r.status_code >= 500:
            an_log(f"[AN] page {page}: HTTP {r.status_code} (busy)")
            continue
        if r.status_code in (400, 404):
            return []
        if r.status_code != 200:
            raise RuntimeError(f"Anfasic answered HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json().get("products", [])
        except ValueError:
            raise RuntimeError(f"Not a product list (blocked?): {r.text[:200]}")
    return None


# ---------------------------------------------------------------- full download
def an_run():
    started = time.time()
    st = {"status": "running", "pages": 0, "products": 0, "saved": 0, "in_stock": 0, "out_of_stock": 0,
          "incomplete": False}
    an_state["last"] = st
    conn = an_conn()
    cur = conn.cursor()
    try:
        cur.execute(AN_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()
        path = an_source()                                  # perfume collection (or whole shop)
        whole_shop = path == "/products.json"
        st["source"] = path
        for page in range(1, AN_MAX_PAGES + 1):
            products = an_get(page, path)
            if products is None:
                st["incomplete"] = True
                an_log(f"[AN] page {page}: gave up -> stopping (run is incomplete)")
                break
            if not products:
                break
            items = an_items(products, whole_shop)
            st["skipped_not_perfume"] = st.get("skipped_not_perfume", 0) + sum(
                1 for p in products if not an_is_perfume(p, whole_shop))
            rows = [(i["variant_id"], i["url"], i["gtin"], i["sku"], i["brand"], i["name"], i["product_type"],
                     i["price_inc_tax"], i["price"], i["stock"], i["volume"]) for i in items if i["variant_id"]]
            if rows:
                execute_values(cur, AN_UPSERT, rows)
                conn.commit()
            st["pages"] = page
            st["products"] += len(products)
            st["saved"] += len(rows)
            st["in_stock"] += sum(i["stock"] == "In stock" for i in items)
            st["out_of_stock"] += sum(i["stock"] != "In stock" for i in items)
            st["seconds"] = round(time.time() - started, 1)
            an_log(f"[AN] page {page}: {len(products)} products, {len(rows)} variants saved (total {st['saved']})")
            if len(products) < 250:
                break
            time.sleep(1.5)
        if not st["incomplete"] and st["saved"]:             # products gone from the site
            cur.execute("UPDATE anfasic_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            st["marked_not_on_site"] = cur.rowcount
            conn.commit()
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        an_log(f"[AN-DONE] {st}")


def an_worker():
    try:
        an_run()
    except Exception as ex:
        an_log(f"[AN-ERROR] {ex}\n{traceback.format_exc()}")
        an_state["last"] = {**(an_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        an_state["running"] = False
        an_lock.release()


# ---------------------------------------------------------------- routes
def an_secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


@anfasic_bp.route("/anfasic/test")
def an_test_route():
    """Read page 1 only - shows whether Render can read Anfasic and what is read."""
    if not an_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        path = an_source()
        whole_shop = path == "/products.json"
        products = an_get(1, path)
    except RuntimeError as ex:
        an_log(f"[AN-TEST] {ex}")
        return jsonify({"ok": False, "error": str(ex)})
    items = an_items(products or [], whole_shop)
    result = {"ok": bool(items), "source": path, "page1_products": len(products or []), "page1_variants": len(items),
              "skipped_not_perfume": [p.get("title") for p in (products or []) if not an_is_perfume(p, whole_shop)][:30],
              "with_sku": sum(1 for i in items if i["sku"]), "with_barcode": sum(1 for i in items if i["gtin"]),
              "brands_seen": sorted({i["brand"] or "-" for i in items})[:20],
              "product_types": sorted({i["product_type"] or "-" for i in items})[:20],
              "first_3": items[:3]}
    an_log(f"[AN-TEST] {result}")
    return jsonify(result)


@anfasic_bp.route("/anfasic/run")
def an_run_route():
    if not an_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not an_lock.acquire(blocking=False):
        return jsonify({"error": "Anfasic download already running"}), 409
    an_state["running"] = True
    an_log("[AN-RUN] Started")
    threading.Thread(target=an_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/anfasic/status"}), 202


@anfasic_bp.route("/anfasic/status")
def an_status_route():
    return jsonify({"running": an_state["running"], "last": an_state["last"]})
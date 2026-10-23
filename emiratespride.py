"""
Emirates Pride catalog (emiratespride.com -> Postgres french_fragrance_db, table emiratespride_catalog)
Plugged into app.py as a Blueprint. Made from perfumedubai.py (same Shopify way of reading).
Shown in the menu "Brands" (not "Competitors") - a brand's own shop; everything else works like a competitor.

Emirates Pride is a Shopify store with a UAE part (/ae), so its product list is public.
Product lists are read from the first address of EP_API_BASES that answers with products (UAE part /ae first,
then the main address); links always open the UAE shop (EP_BASE).
  /emiratespride/test shows the prices: compare one with the website (must be AED).
ONLY PERFUMES are saved (no make up, skincare, bags, gift sets, body mists ...):
  1. the shop's PERFUME collection is read: the first of EP_COLLECTIONS that has products, e.g.
       https://emiratespride.com/ae/collections/perfume/products.json?limit=250&page=1, 2, 3 ...
     (no such collection -> the whole shop /products.json, and only products that look like perfume: EP_PERFUME_WORDS)
  2. then products whose title / type has a word of EP_SKIP_WORDS are left out (gift set, body mist, deodorant ...)
  /emiratespride/test shows which collection was used + the product types / skipped titles -> check it first.
One row per variant (a product with 50ml + 100ml = 2 rows).
Brand = Shopify "vendor" (e.g. "Emirates Pride"), name = product title + size, link opens the right size.

Routes (open in the browser):
  GET /emiratespride/test?secret=XXX    read page 1 only and SHOW what was found (run this first)
  GET /emiratespride/run?secret=XXX     download everything into emiratespride_catalog (background, ~1 min)
  GET /emiratespride/status             progress

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

emiratespride_bp = Blueprint("emiratespride", __name__)

EP_BASE = "https://emiratespride.com/ae"      # UAE part of the shop: product links start with this
# where the product lists are read (tried in this order; /emiratespride/test shows the HTTP answer of each one)
EP_API_BASES = ["https://emiratespride.com/ae", "https://emiratespride.com", "https://www.emiratespride.com"]
EP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
              "Accept": "application/json"}
EP_MAX_PAGES = 100              # Shopify limit: max 100 pages x 250 = 25,000 products
EP_RETRY_WAITS = [10, 30, 60, 120]
# ONLY PERFUMES - change these lists if /emiratespride/test shows wrong products:
# Emirates Pride menu "ALL FRAGRANCES" = their perfumes (Gift Sets / Home Collection: oud, dakhoon, oils = separate)
EP_COLLECTIONS = ["all-fragrances", "fragrances", "perfumes", "perfume", "fragrance"]   # tried in this order
# used only when none of EP_COLLECTIONS exists: a product must have one of these words in its title / type
EP_PERFUME_WORDS = ["perfume", "parfum", "toilette", "cologne", "edp", "edt", "extrait", "fragrance", "attar", "oud"]
# always left out: title or product type has one of these WHOLE words (any capitals). "moisturi*" = word start.
EP_SKIP_WORDS = ["gift set", "giftset", "coffret", "set", "body mist", "hair mist", "body spray", "deodorant",
                 "deo", "body lotion", "body cream", "shower gel", "bath", "soap", "candle", "diffuser",
                 "lipstick", "mascara", "foundation", "serum", "moisturi*", "cleanser", "bag", "pouch",
                 "gift card", "sample",
                 # Emirates Pride Home Collection items, in case some are also listed under ALL FRAGRANCES:
                 "oil", "oils", "dakhoon", "dukhoon", "bakhoor", "bukhoor", "incense", "burner", "mabkhara",
                 "room spray", "home spray", "car perfume", "car freshener"]

ep_lock = threading.Lock()
ep_state = {"running": False, "last": None, "source": None}   # source = collection used (or "all products")


def ep_log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- database
EP_CREATE = """
CREATE TABLE IF NOT EXISTS emiratespride_catalog (
    id            SERIAL PRIMARY KEY,
    variant_id    BIGINT UNIQUE NOT NULL,   -- Shopify variant id (one row per size)
    product_url   TEXT,                     -- https://www.emiratespride.com/products/...?variant=...
    gtin          TEXT,                     -- barcode, only when their SKU is a real barcode
    sku           TEXT,                     -- their SKU as written in Shopify
    brand         TEXT,                     -- Shopify vendor, e.g. "Emirates Pride"
    name          TEXT,                     -- "Oud Wood EDP 50 ml"
    product_type  TEXT,                     -- Shopify product type, e.g. "Eau de Parfum"
    price_inc_tax NUMERIC(10,2),            -- price shown on the site (UAE prices include 5% VAT)
    price         NUMERIC(10,2),            -- without VAT (price_inc_tax / 1.05)
    stock         TEXT,                     -- In stock / Out of stock / Not on site
    volume        TEXT,                     -- e.g. "100 ml"
    updated_at    TIMESTAMP DEFAULT NOW()
);
"""

EP_UPSERT = """
INSERT INTO emiratespride_catalog (variant_id, product_url, gtin, sku, brand, name, product_type,
                              price_inc_tax, price, stock, volume)
VALUES %s
ON CONFLICT (variant_id) DO UPDATE SET
    product_url = EXCLUDED.product_url, gtin = EXCLUDED.gtin, sku = EXCLUDED.sku, brand = EXCLUDED.brand,
    name = EXCLUDED.name, product_type = EXCLUDED.product_type, price_inc_tax = EXCLUDED.price_inc_tax,
    price = EXCLUDED.price, stock = EXCLUDED.stock, volume = EXCLUDED.volume, updated_at = NOW();
"""


def ep_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    ep_log("[EP-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ---------------------------------------------------------------- reading products
def ep_volume(*texts):
    """'100ml' / 'Oud Wood EDP 100 ML' -> '100 ml'"""
    for t in texts:
        m = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", str(t or ""), re.I)
        if m:
            return f"{m.group(1)} ml"
    return None


def ep_is_perfume(p, whole_shop):
    """True = keep this Shopify product. whole_shop = read from /products.json (no perfume collection found)."""
    text = f" {p.get('title') or ''} {p.get('product_type') or ''} ".lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)                 # "Gift-Set" -> " gift set "
    for w in EP_SKIP_WORDS:                                 # whole words only ("set" does not hit "settings")
        pat = rf"\b{re.escape(w.rstrip('*'))}" + ("" if w.endswith("*") else r"\b")
        if re.search(pat, text):
            return False
    return not whole_shop or any(re.search(rf"\b{w}\b", text) for w in EP_PERFUME_WORDS)


def ep_items(products, whole_shop=False):
    """Shopify products -> one dict per variant (perfumes only, see ep_is_perfume)."""
    out = []
    for p in products:
        if not ep_is_perfume(p, whole_shop):
            continue
        title = (p.get("title") or "").strip()
        variants = p.get("variants", [])
        for v in variants:
            vtitle = (v.get("title") or "").strip()
            name = title if vtitle.lower() in ("", "default title") else f"{title} - {vtitle}"
            url = f"{EP_BASE}/products/{p.get('handle')}"
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
                "volume": ep_volume(vtitle, title),
            })
    return out


def ep_try(url):
    """One test read (1 product). Returns (number of products, short answer text) - also saved in ep_state['tried']."""
    try:
        r = requests.get(url, params={"limit": 1}, headers=EP_HEADERS, timeout=60)
    except requests.RequestException as ex:
        answer, n = f"network error {str(ex)[:80]}", 0
    else:
        try:
            n = len(r.json().get("products", [])) if r.status_code == 200 else 0
            answer = f"HTTP {r.status_code}, {n} product(s)"
        except ValueError:
            n, answer = 0, f"HTTP {r.status_code}, not a product list (web page / blocked?): {r.text[:80]!r}"
    ep_state.setdefault("tried", []).append({"url": url, "answer": answer})
    ep_log(f"[EP] test read {url}: {answer}")
    return n, answer


def ep_source():
    """Path of the perfume list: /collections/<first of EP_COLLECTIONS with products>/products.json, else
    /products.json (whole shop, filtered by EP_PERFUME_WORDS). Tries every address of EP_API_BASES; the one that
    works is kept in ep_state['api_base'] (used by ep_get)."""
    ep_state["tried"] = []
    for base in EP_API_BASES:
        for path in [f"/collections/{h}/products.json" for h in EP_COLLECTIONS] + ["/products.json"]:
            n, _ = ep_try(f"{base}{path}")
            if n:
                ep_state["api_base"], ep_state["source"] = base, path
                ep_log(f"[EP] using {base}{path}")
                return path
    ep_log("[EP] NO address answered with products - see 'tried' on /emiratespride/test")
    ep_state["api_base"], ep_state["source"] = EP_API_BASES[0], "/products.json"
    return "/products.json"


def ep_get(page, path="/products.json"):
    """One page of products.json (retries when busy). Returns list of products, [] at the end, None on failure."""
    for attempt, wait in enumerate([0] + EP_RETRY_WAITS):
        if wait:
            ep_log(f"[EP] page {page}: retry {attempt}/{len(EP_RETRY_WAITS)} in {wait}s")
            time.sleep(wait)
        try:
            r = requests.get(f"{ep_state.get('api_base') or EP_API_BASES[0]}{path}", params={"limit": 250, "page": page},
                             headers=EP_HEADERS, timeout=60)
        except requests.RequestException as ex:
            ep_log(f"[EP] page {page}: network error {ex}")
            continue
        if r.status_code == 429 or r.status_code >= 500:
            ep_log(f"[EP] page {page}: HTTP {r.status_code} (busy)")
            continue
        if r.status_code in (400, 404):
            return []
        if r.status_code != 200:
            raise RuntimeError(f"Emirates Pride answered HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json().get("products", [])
        except ValueError:
            raise RuntimeError(f"Not a product list (blocked?): {r.text[:200]}")
    return None


# ---------------------------------------------------------------- full download
def ep_run():
    started = time.time()
    st = {"status": "running", "pages": 0, "products": 0, "saved": 0, "in_stock": 0, "out_of_stock": 0,
          "incomplete": False}
    ep_state["last"] = st
    conn = ep_conn()
    cur = conn.cursor()
    try:
        cur.execute(EP_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()
        path = ep_source()                                  # perfume collection (or whole shop)
        whole_shop = path == "/products.json"
        st["source"] = path
        st["api_base"] = ep_state.get("api_base")
        for page in range(1, EP_MAX_PAGES + 1):
            products = ep_get(page, path)
            if products is None:
                st["incomplete"] = True
                ep_log(f"[EP] page {page}: gave up -> stopping (run is incomplete)")
                break
            if not products:
                break
            items = ep_items(products, whole_shop)
            st["skipped_not_perfume"] = st.get("skipped_not_perfume", 0) + sum(
                1 for p in products if not ep_is_perfume(p, whole_shop))
            rows = [(i["variant_id"], i["url"], i["gtin"], i["sku"], i["brand"], i["name"], i["product_type"],
                     i["price_inc_tax"], i["price"], i["stock"], i["volume"]) for i in items if i["variant_id"]]
            if rows:
                execute_values(cur, EP_UPSERT, rows)
                conn.commit()
            st["pages"] = page
            st["products"] += len(products)
            st["saved"] += len(rows)
            st["in_stock"] += sum(i["stock"] == "In stock" for i in items)
            st["out_of_stock"] += sum(i["stock"] != "In stock" for i in items)
            st["seconds"] = round(time.time() - started, 1)
            ep_log(f"[EP] page {page}: {len(products)} products, {len(rows)} variants saved (total {st['saved']})")
            if len(products) < 250:
                break
            time.sleep(1.5)
        if not st["incomplete"] and st["saved"]:             # products gone from the site
            cur.execute("UPDATE emiratespride_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            st["marked_not_on_site"] = cur.rowcount
            conn.commit()
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        ep_log(f"[EP-DONE] {st}")


def ep_worker():
    try:
        ep_run()
    except Exception as ex:
        ep_log(f"[EP-ERROR] {ex}\n{traceback.format_exc()}")
        ep_state["last"] = {**(ep_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        ep_state["running"] = False
        ep_lock.release()


# ---------------------------------------------------------------- routes
def ep_secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


@emiratespride_bp.route("/emiratespride/test")
def ep_test_route():
    """Read page 1 only - shows whether Render can read Emirates Pride and what is read."""
    if not ep_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        path = ep_source()
        whole_shop = path == "/products.json"
        products = ep_get(1, path)
    except RuntimeError as ex:
        ep_log(f"[EP-TEST] {ex}")
        return jsonify({"ok": False, "error": str(ex)})
    items = ep_items(products or [], whole_shop)
    result = {"ok": bool(items), "source": path, "api_base": ep_state.get("api_base"),
              "tried": ep_state.get("tried", []), "page1_products": len(products or []), "page1_variants": len(items),
              "skipped_not_perfume": [p.get("title") for p in (products or []) if not ep_is_perfume(p, whole_shop)][:30],
              "with_sku": sum(1 for i in items if i["sku"]), "with_barcode": sum(1 for i in items if i["gtin"]),
              "brands_seen": sorted({i["brand"] or "-" for i in items})[:20],
              "product_types": sorted({i["product_type"] or "-" for i in items})[:20],
              "first_3": items[:3]}
    ep_log(f"[EP-TEST] {result}")
    return jsonify(result)


@emiratespride_bp.route("/emiratespride/run")
def ep_run_route():
    if not ep_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not ep_lock.acquire(blocking=False):
        return jsonify({"error": "Emirates Pride download already running"}), 409
    ep_state["running"] = True
    ep_log("[EP-RUN] Started")
    threading.Thread(target=ep_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/emiratespride/status"}), 202


@emiratespride_bp.route("/emiratespride/status")
def ep_status_route():
    return jsonify({"running": ep_state["running"], "last": ep_state["last"]})
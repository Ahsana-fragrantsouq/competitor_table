"""
Oud Elite catalog (ae.oudelite.com -> Postgres french_fragrance_db, table oudelite_catalog)
Plugged into app.py as a Blueprint. Made from khaltat.py (same Shopify way of reading).
Shown in the menu "Brands" (not "Competitors") - the brand's own shop; everything else works like a competitor.
Oud Elite = same group as MHG Boutique / Oud Elite; besides perfumes it sells oils, dokhoon, incense -> strict perfume filter.

Oud Elite is a Shopify store, so its product list is public.
ONLY PERFUMES are saved (no make up, skincare, bags, gift sets, body mists ...):
  1. the shop's PERFUME collection is read: the first of OE_COLLECTIONS that has products, e.g.
       https://ae.oudelite.com/collections/perfume/products.json?limit=250&page=1, 2, 3 ...
     (no such collection -> the whole shop /products.json: product type "Perfumes..." (OE_TYPE_WORDS) or a perfume word
      in the title (OE_PERFUME_WORDS))
  2. then products whose title / type has a word of OE_SKIP_WORDS are left out (gift set, body mist, deodorant ...)
  /oudelite/test shows which collection was used + the product types / skipped titles -> check it first.
One row per variant (a product with 50ml + 100ml = 2 rows).
Brand = Shopify "vendor" (e.g. "Oud Elite"). Size from the tags ("75ml") when not in the title., name = product title + size, link opens the right size.

Routes (open in the browser):
  GET /oudelite/test?secret=XXX    read page 1 only and SHOW what was found (run this first)
  GET /oudelite/run?secret=XXX     download everything into oudelite_catalog (background, ~1 min)
  GET /oudelite/status             progress

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

oudelite_bp = Blueprint("oudelite", __name__)

OE_BASE = "https://ae.oudelite.com"          # UAE shop: product links start with this
# where the product list is read (tried in this order; /oudelite/test shows the answer of each one in 'tried')
OE_API_BASES = ["https://ae.oudelite.com", "https://oudelite.com", "https://www.oudelite.com"]
OE_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
              "Accept": "application/json"}
OE_MAX_PAGES = 100              # Shopify limit: max 100 pages x 250 = 25,000 products
OE_RETRY_WAITS = [10, 30, 60, 120]
# ONLY PERFUMES - change these lists if /oudelite/test shows wrong products:
OE_COLLECTIONS = ["perfumes", "perfume", "fragrances", "all-fragrances", "fragrance", "all-perfumes"]   # tried in this order
# used only when none of OE_COLLECTIONS exists: a product must have one of these words in its title / type
OE_PERFUME_WORDS = ["perfume", "perfumes", "parfum", "toilette", "cologne", "edp", "edt", "extrait", "fragrance"]
# whole shop: a product type with one of these words is a perfume too ("perfume*" = word start -> "Perfumes | عطور")
OE_TYPE_WORDS = ["perfume*", "parfum*", "fragrance*"]
# always left out: title or product type has one of these WHOLE words (any capitals). "moisturi*" = word start.
OE_SKIP_WORDS = ["gift set", "giftset", "coffret", "set", "body mist", "hair mist", "body spray", "deodorant",
                 "deo", "body lotion", "body cream", "shower gel", "bath", "soap", "candle", "diffuser",
                 "lipstick", "mascara", "foundation", "serum", "moisturi*", "cleanser", "bag", "pouch",
                 "gift card", "sample",
                 # home / incense items (oils, dokhoon ...)
                 "oil", "oils", "dakhoon", "dokhoon", "dukhoon", "bakhoor", "bukhoor", "incense", "burner", "mabkhara",
                 "mubakhar", "room spray", "home spray", "car perfume", "car freshener", "kit", "box",
                 "giveaway*"]                     # product type "Giveaways" = free promo items, not for sale

oe_lock = threading.Lock()
oe_state = {"running": False, "last": None, "source": None}   # source = collection used (or "all products")


def oe_log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- database
OE_CREATE = """
CREATE TABLE IF NOT EXISTS oudelite_catalog (
    id            SERIAL PRIMARY KEY,
    variant_id    BIGINT UNIQUE NOT NULL,   -- Shopify variant id (one row per size)
    product_url   TEXT,                     -- https://ae.oudelite.com/products/...?variant=...
    gtin          TEXT,                     -- barcode, only when their SKU is a real barcode
    sku           TEXT,                     -- their SKU as written in Shopify
    brand         TEXT,                     -- Shopify vendor, e.g. "Oud Elite"
    name          TEXT,                     -- "Oud Wood EDP 50 ml"
    product_type  TEXT,                     -- Shopify product type, e.g. "Eau de Parfum"
    price_inc_tax NUMERIC(10,2),            -- price shown on the site (UAE prices include 5% VAT)
    price         NUMERIC(10,2),            -- without VAT (price_inc_tax / 1.05)
    stock         TEXT,                     -- In stock / Out of stock / Not on site
    volume        TEXT,                     -- e.g. "100 ml"
    updated_at    TIMESTAMP DEFAULT NOW()
);
"""

OE_UPSERT = """
INSERT INTO oudelite_catalog (variant_id, product_url, gtin, sku, brand, name, product_type,
                              price_inc_tax, price, stock, volume)
VALUES %s
ON CONFLICT (variant_id) DO UPDATE SET
    product_url = EXCLUDED.product_url, gtin = EXCLUDED.gtin, sku = EXCLUDED.sku, brand = EXCLUDED.brand,
    name = EXCLUDED.name, product_type = EXCLUDED.product_type, price_inc_tax = EXCLUDED.price_inc_tax,
    price = EXCLUDED.price, stock = EXCLUDED.stock, volume = EXCLUDED.volume, updated_at = NOW();
"""


def oe_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    oe_log("[OE-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ---------------------------------------------------------------- reading products
def oe_volume(*texts):
    """'100ml' / 'Oud Wood EDP 100 ML' -> '100 ml'"""
    for t in texts:
        m = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", str(t or ""), re.I)
        if m:
            return f"{m.group(1)} ml"
    return None


def oe_is_perfume(p, whole_shop):
    """True = keep this Shopify product. whole_shop = read from /products.json (no perfume collection found)."""
    text = f" {p.get('title') or ''} {p.get('product_type') or ''} ".lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)                 # "Gift-Set" -> " gift set "
    for w in OE_SKIP_WORDS:                                 # whole words only ("set" does not hit "settings")
        pat = rf"\b{re.escape(w.rstrip('*'))}" + ("" if w.endswith("*") else r"\b")
        if re.search(pat, text):
            return False
    if not whole_shop:
        return True
    ptype = re.sub(r"[^a-z0-9]+", " ", (p.get("product_type") or "").lower())
    return (any(re.search(rf"\b{re.escape(w.rstrip('*'))}" + ("" if w.endswith("*") else r"\b"), ptype)
                for w in OE_TYPE_WORDS)
            or any(re.search(rf"\b{w}\b", text) for w in OE_PERFUME_WORDS))


def oe_items(products, whole_shop=False):
    """Shopify products -> one dict per variant (perfumes only, see oe_is_perfume)."""
    out = []
    for p in products:
        if not oe_is_perfume(p, whole_shop):
            continue
        title = (p.get("title") or "").strip()
        variants = p.get("variants", [])
        for v in variants:
            vtitle = (v.get("title") or "").strip()
            name = title if vtitle.lower() in ("", "default title") else f"{title} - {vtitle}"
            url = f"{OE_BASE}/products/{p.get('handle')}"
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
                "volume": oe_volume(vtitle, title, *(p.get("tags") or [])),   # tags: "75ml"
            })
    return out


def oe_try(url):
    """One test read (1 product). Returns number of products - the answer is saved in oe_state['tried']."""
    try:
        r = requests.get(url, params={"limit": 1}, headers=OE_HEADERS, timeout=60)
    except requests.RequestException as ex:
        answer, n = f"network error {str(ex)[:80]}", 0
    else:
        try:
            n = len(r.json().get("products", [])) if r.status_code == 200 else 0
            answer = f"HTTP {r.status_code}, {n} product(s)"
        except ValueError:
            n, answer = 0, f"HTTP {r.status_code}, not a product list (web page / blocked?): {r.text[:80]!r}"
    oe_state.setdefault("tried", []).append({"url": url, "answer": answer})
    oe_log(f"[OE] test read {url}: {answer}")
    return n


def oe_source():
    """Perfume list path: /collections/<first of OE_COLLECTIONS with products>/products.json, else /products.json
    (whole shop, filtered by product type / words). Every address of OE_API_BASES is tried; the one that answers
    with products is kept in oe_state['api_base'] (used by oe_get). /oudelite/test shows every answer ('tried')."""
    oe_state["tried"] = []
    for base in OE_API_BASES:
        for path in [f"/collections/{h}/products.json" for h in OE_COLLECTIONS] + ["/products.json"]:
            if oe_try(f"{base}{path}"):
                oe_state["api_base"], oe_state["source"] = base, path
                oe_log(f"[OE] using {base}{path}")
                return path
    oe_log("[OE] NO address answered with products - see 'tried' on /oudelite/test")
    oe_state["api_base"], oe_state["source"] = OE_API_BASES[0], "/products.json"
    return "/products.json"


def oe_get(page, path="/products.json"):
    """One page of products.json (retries when busy). Returns list of products, [] at the end, None on failure."""
    for attempt, wait in enumerate([0] + OE_RETRY_WAITS):
        if wait:
            oe_log(f"[OE] page {page}: retry {attempt}/{len(OE_RETRY_WAITS)} in {wait}s")
            time.sleep(wait)
        try:
            r = requests.get(f"{oe_state.get('api_base') or OE_BASE}{path}", params={"limit": 250, "page": page},
                             headers=OE_HEADERS, timeout=60)
        except requests.RequestException as ex:
            oe_log(f"[OE] page {page}: network error {ex}")
            continue
        if r.status_code == 429 or r.status_code >= 500:
            oe_log(f"[OE] page {page}: HTTP {r.status_code} (busy)")
            continue
        if r.status_code in (400, 404):
            return []
        if r.status_code != 200:
            raise RuntimeError(f"Oud Elite answered HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json().get("products", [])
        except ValueError:
            raise RuntimeError(f"Not a product list (blocked?): {r.text[:200]}")
    return None


# ---------------------------------------------------------------- full download
def oe_run():
    started = time.time()
    st = {"status": "running", "pages": 0, "products": 0, "saved": 0, "in_stock": 0, "out_of_stock": 0,
          "incomplete": False}
    oe_state["last"] = st
    conn = oe_conn()
    cur = conn.cursor()
    try:
        cur.execute(OE_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()
        path = oe_source()                                  # perfume collection (or whole shop)
        whole_shop = path == "/products.json"
        st["source"] = path
        for page in range(1, OE_MAX_PAGES + 1):
            products = oe_get(page, path)
            if products is None:
                st["incomplete"] = True
                oe_log(f"[OE] page {page}: gave up -> stopping (run is incomplete)")
                break
            if not products:
                break
            items = oe_items(products, whole_shop)
            st["skipped_not_perfume"] = st.get("skipped_not_perfume", 0) + sum(
                1 for p in products if not oe_is_perfume(p, whole_shop))
            rows = [(i["variant_id"], i["url"], i["gtin"], i["sku"], i["brand"], i["name"], i["product_type"],
                     i["price_inc_tax"], i["price"], i["stock"], i["volume"]) for i in items if i["variant_id"]]
            if rows:
                execute_values(cur, OE_UPSERT, rows)
                conn.commit()
            st["pages"] = page
            st["products"] += len(products)
            st["saved"] += len(rows)
            st["in_stock"] += sum(i["stock"] == "In stock" for i in items)
            st["out_of_stock"] += sum(i["stock"] != "In stock" for i in items)
            st["seconds"] = round(time.time() - started, 1)
            oe_log(f"[OE] page {page}: {len(products)} products, {len(rows)} variants saved (total {st['saved']})")
            if len(products) < 250:
                break
            time.sleep(1.5)
        if not st["incomplete"] and st["saved"]:             # products gone from the site
            cur.execute("UPDATE oudelite_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            st["marked_not_on_site"] = cur.rowcount
            conn.commit()
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        oe_log(f"[OE-DONE] {st}")


def oe_worker():
    try:
        oe_run()
    except Exception as ex:
        oe_log(f"[OE-ERROR] {ex}\n{traceback.format_exc()}")
        oe_state["last"] = {**(oe_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        oe_state["running"] = False
        oe_lock.release()


# ---------------------------------------------------------------- routes
def oe_secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


@oudelite_bp.route("/oudelite/test")
def oe_test_route():
    """Read page 1 only - shows whether Render can read Oud Elite and what is read."""
    if not oe_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        path = oe_source()
        whole_shop = path == "/products.json"
        products = oe_get(1, path)
    except RuntimeError as ex:
        oe_log(f"[OE-TEST] {ex}")
        return jsonify({"ok": False, "error": str(ex)})
    items = oe_items(products or [], whole_shop)
    result = {"ok": bool(items), "source": path, "api_base": oe_state.get("api_base"),
              "tried": oe_state.get("tried", []), "page1_products": len(products or []), "page1_variants": len(items),
              "skipped_not_perfume": [p.get("title") for p in (products or []) if not oe_is_perfume(p, whole_shop)][:30],
              "with_sku": sum(1 for i in items if i["sku"]), "with_barcode": sum(1 for i in items if i["gtin"]),
              "brands_seen": sorted({i["brand"] or "-" for i in items})[:20],
              "product_types": sorted({i["product_type"] or "-" for i in items})[:20],
              "first_3": items[:3]}
    oe_log(f"[OE-TEST] {result}")
    return jsonify(result)


@oudelite_bp.route("/oudelite/run")
def oe_run_route():
    if not oe_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not oe_lock.acquire(blocking=False):
        return jsonify({"error": "Oud Elite download already running"}), 409
    oe_state["running"] = True
    oe_log("[OE-RUN] Started")
    threading.Thread(target=oe_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/oudelite/status"}), 202


@oudelite_bp.route("/oudelite/status")
def oe_status_route():
    return jsonify({"running": oe_state["running"], "last": oe_state["last"]})
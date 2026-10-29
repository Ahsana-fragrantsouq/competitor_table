"""
Anfasic catalog (anfasic.ae -> Postgres french_fragrance_db, table anfasic_catalog)
Plugged into app.py as a Blueprint. Made from perfumedubai.py (same Shopify way of reading).
Shown in the menu "Brands" (not "Competitors") - the brand's own shop; everything else works like a competitor.
Anfasic (Anfasic Dokhoon) = same group as MHG Boutique; it sells a lot of dokhoon / incense -> strict perfume filter.

Anfasic is a Shopify store, so its product list is public.
FRAGRANCES + GIFT SETS are saved (user's choice, same as Oud Elite) - EVERYTHING in these 2 collections:
  1. fragrances: the first of AN_COLLECTIONS that has products, e.g.
       https://www.anfasic.ae/collections/perfumes/products.json?limit=250&page=1, 2, 3 ...
     (no such collection -> whole shop /products.json: product type "Perfumes..." (AN_TYPE_WORDS) or a perfume word
      in the title (AN_PERFUME_WORDS), minus AN_SKIP_WORDS)
  2. gift sets: the first of AN_GIFT_COLLECTIONS that has products
     (no such collection -> whole shop /products.json: title / type with a word of AN_GIFT_WORDS)
  Perfume collection: oils / dokhoon / incense ... (AN_SKIP_WORDS) still left out, gift sets kept. Gift-set collection:
  only AN_ALWAYS_SKIP left out (gift card, giveaways). A product in both lists is saved once.
  /anfasic/test shows which collections were used + the product types / skipped titles -> check it first.
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
# gift sets (user's choice: fragrances + gift sets) - collections tried in this order
AN_GIFT_COLLECTIONS = ["gift-sets", "gift-set", "giftsets", "gifts", "gift", "sets", "gifting"]
# used only when none of AN_GIFT_COLLECTIONS exists: whole shop, title / type has one of these words
AN_GIFT_WORDS = ["gift set", "giftset", "gift box", "coffret", "set", "gift", "gifts",
                 "collection", "trio", "duo"]           # Anfasic perfume sets: "SHAY MUCH COLLECTION", "TRIO COLLECTION" ...
# left out EVERYWHERE (also inside the collections): not real products for sale
AN_ALWAYS_SKIP = ["gift card", "giveaway*", "sample", "discovery"]   # discovery kit = minis, like Brands Perfume
# perfume collection + whole-shop fragrance search: title or product type with one of these words is NOT a fragrance
AN_SKIP_WORDS = ["body mist", "hair mist", "body spray", "deodorant",
                 "deo", "body lotion", "body cream", "shower gel", "bath", "soap", "candle", "diffuser",
                 "lipstick", "mascara", "foundation", "serum", "moisturi*", "cleanser", "bag", "pouch",
                 "gift card", "sample",
                 # Anfasic Dokhoon home / incense items
                 "oil", "oils", "dakhoon", "dokhoon", "dukhoon", "bakhoor", "bukhoor", "incense", "burner", "mabkhara",
                 "mubakhar", "room spray", "home spray", "car perfume", "car freshener", "kit", "box",
                 "giveaway*"]                     # product type "Giveaways" = free promo items, not for sale

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


def an_has(words, text):
    """True when text has one of the WHOLE words ("perfume*" = word start; any capitals, "Gift-Set" = "gift set")."""
    text = " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()) + " "
    return any(re.search(rf"\b{re.escape(w.rstrip('*'))}" + ("" if w.endswith("*") else r"\b"), text) for w in words)


def an_is_perfume(p, kind):
    """True = keep this Shopify product. kind (see an_sources):
    'collection' = from the GIFT-SET collection -> everything except AN_ALWAYS_SKIP
    'fragcoll'   = from the PERFUME collection -> gift sets kept, else AN_SKIP_WORDS left out (oils, dokhoon, incense ...
                   Anfasic sells a lot of those - same strict filter as before)
    'perfume'    = whole shop, fragrances picked by words      'gift' = whole shop, gift sets picked by words"""
    text = f"{p.get('title') or ''} {p.get('product_type') or ''}"
    if an_has(AN_ALWAYS_SKIP, text):
        return False
    if kind == "gift":
        return an_has(AN_GIFT_WORDS, text)
    if kind == "fragcoll":
        return an_has(AN_GIFT_WORDS, text) or not an_has(AN_SKIP_WORDS, text)
    if kind == "perfume":
        if an_has(AN_SKIP_WORDS, text):
            return False
        return an_has(AN_TYPE_WORDS, p.get("product_type")) or an_has(AN_PERFUME_WORDS, text)
    return True


def an_items(products, kind="collection"):
    """Shopify products -> one dict per variant (fragrances / gift sets only, see an_is_perfume)."""
    out = []
    for p in products:
        if not an_is_perfume(p, kind):
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


def an_collection(handles, label):
    """Path of the first collection in handles that has products, else None."""
    for handle in handles:
        path = f"/collections/{handle}/products.json"
        try:
            r = requests.get(f"{AN_BASE}{path}", params={"limit": 1}, headers=AN_HEADERS, timeout=60)
            n = len(r.json().get("products", [])) if r.status_code == 200 else 0
        except (requests.RequestException, ValueError) as ex:
            an_log(f"[AN] {label} collection '{handle}': not readable ({str(ex)[:80]})")
            continue
        an_log(f"[AN] {label} collection '{handle}': HTTP {r.status_code}, {n} product(s) on a test read")
        if n:
            return path
    return None


def an_sources():
    """The 2 lists to read: [(path, kind), ...] - fragrances first, then gift sets.
    kind 'fragcoll' = perfume collection (strict filter); 'collection' = gift-set collection (keep everything);
    'perfume' / 'gift' = whole shop, picked by words."""
    out = []
    path = an_collection(AN_COLLECTIONS, "fragrance")
    if path:
        out.append((path, "fragcoll"))                      # perfume collection: strict filter (no oils / dokhoon)
    else:
        an_log("[AN] no fragrance collection found -> whole shop, fragrances picked by words")
        out.append(("/products.json", "perfume"))
    path = an_collection(AN_GIFT_COLLECTIONS, "gift set")
    if path:
        out.append((path, "collection"))
    else:
        an_log("[AN] no gift set collection found -> whole shop, gift sets picked by words")
        out.append(("/products.json", "gift"))
    an_state["source"] = [f"{p} ({k})" for p, k in out]
    return out


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
        sources = an_sources()                              # fragrances + gift sets
        st["source"] = [f"{p} ({k})" for p, k in sources]
        seen = set()                                        # variant ids already saved (product in both lists)
        for path, kind in sources:
            if st["incomplete"]:
                break
            an_log(f"[AN] reading {path} ({kind})")
            for page in range(1, AN_MAX_PAGES + 1):
                products = an_get(page, path)
                if products is None:
                    st["incomplete"] = True
                    an_log(f"[AN] {path} page {page}: gave up -> stopping (run is incomplete)")
                    break
                if not products:
                    break
                items = [i for i in an_items(products, kind) if i["variant_id"] and i["variant_id"] not in seen]
                seen.update(i["variant_id"] for i in items)
                st["skipped_not_perfume"] = st.get("skipped_not_perfume", 0) + sum(
                    1 for p in products if not an_is_perfume(p, kind))
                rows = [(i["variant_id"], i["url"], i["gtin"], i["sku"], i["brand"], i["name"], i["product_type"],
                         i["price_inc_tax"], i["price"], i["stock"], i["volume"]) for i in items]
                if rows:
                    execute_values(cur, AN_UPSERT, rows)
                    conn.commit()
                st["pages"] += 1
                st["products"] += len(products)
                st["saved"] += len(rows)
                st["in_stock"] += sum(i["stock"] == "In stock" for i in items)
                st["out_of_stock"] += sum(i["stock"] != "In stock" for i in items)
                st["seconds"] = round(time.time() - started, 1)
                an_log(f"[AN] {path} page {page}: {len(products)} products, {len(rows)} new variants saved "
                       f"(total {st['saved']})")
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
        lists = []
        for path, kind in an_sources():                     # fragrances + gift sets, page 1 of each
            products = an_get(1, path) or []
            items = an_items(products, kind)
            lists.append({"source": f"{path} ({kind})", "page1_products": len(products),
                          "page1_variants": len(items),
                          "skipped": [p.get("title") for p in products if not an_is_perfume(p, kind)][:30],
                          "product_types": sorted({i["product_type"] or "-" for i in items})[:20],
                          "first_3": items[:3]})
    except RuntimeError as ex:
        an_log(f"[AN-TEST] {ex}")
        return jsonify({"ok": False, "error": str(ex)})
    result = {"ok": any(x["page1_variants"] for x in lists), "lists": lists}
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
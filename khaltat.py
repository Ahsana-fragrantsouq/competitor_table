"""
Khaltat catalog (khaltat.com -> Postgres french_fragrance_db, table khaltat_catalog)
Plugged into app.py as a Blueprint. Made from anfasic.py (same Shopify way of reading).
Shown in the menu "Brands" (not "Competitors") - the brand's own shop; everything else works like a competitor.
Khaltat = same group as MHG Boutique / Khaltat; besides perfumes it sells oils, dokhoon, incense -> strict perfume filter.

Khaltat is a Shopify store, so its product list is public.
FRAGRANCES + GIFT SETS are saved (user's choice, same as Oud Elite) - EVERYTHING in these 2 collections:
  1. fragrances: the first of KH_COLLECTIONS that has products, e.g.
       https://www.khaltat.com/collections/perfumes/products.json?limit=250&page=1, 2, 3 ...
     (no such collection -> whole shop /products.json: product type "Perfumes..." (KH_TYPE_WORDS) or a perfume word
      in the title (KH_PERFUME_WORDS), minus KH_SKIP_WORDS)
  2. gift sets: the first of KH_GIFT_COLLECTIONS that has products
     (no such collection -> whole shop /products.json: title / type with a word of KH_GIFT_WORDS)
  Inside a collection only KH_ALWAYS_SKIP is left out (gift card, giveaways). A product in both lists is saved once.
  /khaltat/test shows which collections were used + the product types / skipped titles -> check it first.
One row per variant (a product with 50ml + 100ml = 2 rows).
Brand = Shopify "vendor" (e.g. "Khaltat"). Size from the tags ("75ml") when not in the title., name = product title + size, link opens the right size.

Routes (open in the browser):
  GET /khaltat/test?secret=XXX    read page 1 only and SHOW what was found (run this first)
  GET /khaltat/run?secret=XXX     download everything into khaltat_catalog (background, ~1 min)
  GET /khaltat/status             progress

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

khaltat_bp = Blueprint("khaltat", __name__)

KH_BASE = "https://www.khaltat.com"
KH_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
              "Accept": "application/json"}
KH_MAX_PAGES = 100              # Shopify limit: max 100 pages x 250 = 25,000 products
KH_RETRY_WAITS = [10, 30, 60, 120]
# ONLY PERFUMES - change these lists if /khaltat/test shows wrong products:
KH_COLLECTIONS = ["perfumes", "perfume", "fragrances", "all-fragrances", "fragrance", "all-perfumes"]   # tried in this order
# used only when none of KH_COLLECTIONS exists: a product must have one of these words in its title / type
KH_PERFUME_WORDS = ["perfume", "perfumes", "parfum", "toilette", "cologne", "edp", "edt", "extrait", "fragrance"]
# whole shop: a product type with one of these words is a perfume too ("perfume*" = word start -> "Perfumes | عطور")
KH_TYPE_WORDS = ["perfume*", "parfum*", "fragrance*"]
# always left out: title or product type has one of these WHOLE words (any capitals). "moisturi*" = word start.
# gift sets (user's choice: fragrances + gift sets) - collections tried in this order
KH_GIFT_COLLECTIONS = ["gift-sets", "gift-set", "giftsets", "gifts", "gift", "sets", "gifting"]
# used only when none of KH_GIFT_COLLECTIONS exists: whole shop, title / type has one of these words
KH_GIFT_WORDS = ["gift set", "giftset", "gift box", "coffret", "set", "gift", "gifts"]
# left out EVERYWHERE (also inside the collections): not real products for sale
KH_ALWAYS_SKIP = ["gift card", "giveaway*", "sample"]
# whole-shop fragrance search only: title or product type with one of these words is NOT a fragrance
KH_SKIP_WORDS = ["body mist", "hair mist", "body spray", "deodorant",
                 "deo", "body lotion", "body cream", "shower gel", "bath", "soap", "candle", "diffuser",
                 "lipstick", "mascara", "foundation", "serum", "moisturi*", "cleanser", "bag", "pouch",
                 "gift card", "sample",
                 # home / incense items (oils, dokhoon ...)
                 "oil", "oils", "dakhoon", "dokhoon", "dukhoon", "bakhoor", "bukhoor", "incense", "burner", "mabkhara",
                 "mubakhar", "room spray", "home spray", "car perfume", "car freshener", "kit", "box",
                 "giveaway*"]                     # product type "Giveaways" = free promo items, not for sale

kh_lock = threading.Lock()
kh_state = {"running": False, "last": None, "source": None}   # source = collection used (or "all products")


def kh_log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- database
KH_CREATE = """
CREATE TABLE IF NOT EXISTS khaltat_catalog (
    id            SERIAL PRIMARY KEY,
    variant_id    BIGINT UNIQUE NOT NULL,   -- Shopify variant id (one row per size)
    product_url   TEXT,                     -- https://www.khaltat.com/products/...?variant=...
    gtin          TEXT,                     -- barcode, only when their SKU is a real barcode
    sku           TEXT,                     -- their SKU as written in Shopify
    brand         TEXT,                     -- Shopify vendor, e.g. "Khaltat"
    name          TEXT,                     -- "Oud Wood EDP 50 ml"
    product_type  TEXT,                     -- Shopify product type, e.g. "Eau de Parfum"
    price_inc_tax NUMERIC(10,2),            -- price shown on the site (UAE prices include 5% VAT)
    price         NUMERIC(10,2),            -- without VAT (price_inc_tax / 1.05)
    stock         TEXT,                     -- In stock / Out of stock / Not on site
    volume        TEXT,                     -- e.g. "100 ml"
    updated_at    TIMESTAMP DEFAULT NOW()
);
"""

KH_UPSERT = """
INSERT INTO khaltat_catalog (variant_id, product_url, gtin, sku, brand, name, product_type,
                              price_inc_tax, price, stock, volume)
VALUES %s
ON CONFLICT (variant_id) DO UPDATE SET
    product_url = EXCLUDED.product_url, gtin = EXCLUDED.gtin, sku = EXCLUDED.sku, brand = EXCLUDED.brand,
    name = EXCLUDED.name, product_type = EXCLUDED.product_type, price_inc_tax = EXCLUDED.price_inc_tax,
    price = EXCLUDED.price, stock = EXCLUDED.stock, volume = EXCLUDED.volume, updated_at = NOW();
"""


def kh_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    kh_log("[KH-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ---------------------------------------------------------------- reading products
def kh_volume(*texts):
    """'100ml' / 'Oud Wood EDP 100 ML' -> '100 ml'"""
    for t in texts:
        m = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", str(t or ""), re.I)
        if m:
            return f"{m.group(1)} ml"
    return None


def kh_has(words, text):
    """True when text has one of the WHOLE words ("perfume*" = word start; any capitals, "Gift-Set" = "gift set")."""
    text = " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()) + " "
    return any(re.search(rf"\b{re.escape(w.rstrip('*'))}" + ("" if w.endswith("*") else r"\b"), text) for w in words)


def kh_is_perfume(p, kind):
    """True = keep this Shopify product. kind (see kh_sources):
    'collection' = from a fragrance / gift-set collection -> everything except KH_ALWAYS_SKIP
    'perfume'    = whole shop, fragrances picked by words      'gift' = whole shop, gift sets picked by words"""
    text = f"{p.get('title') or ''} {p.get('product_type') or ''}"
    if kh_has(KH_ALWAYS_SKIP, text):
        return False
    if kind == "gift":
        return kh_has(KH_GIFT_WORDS, text)
    if kind == "perfume":
        if kh_has(KH_SKIP_WORDS, text):
            return False
        return kh_has(KH_TYPE_WORDS, p.get("product_type")) or kh_has(KH_PERFUME_WORDS, text)
    return True


def kh_items(products, kind="collection"):
    """Shopify products -> one dict per variant (fragrances / gift sets only, see kh_is_perfume)."""
    out = []
    for p in products:
        if not kh_is_perfume(p, kind):
            continue
        title = (p.get("title") or "").strip()
        variants = p.get("variants", [])
        for v in variants:
            vtitle = (v.get("title") or "").strip()
            name = title if vtitle.lower() in ("", "default title") else f"{title} - {vtitle}"
            url = f"{KH_BASE}/products/{p.get('handle')}"
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
                "volume": kh_volume(vtitle, title, *(p.get("tags") or [])),   # tags: "75ml"
            })
    return out


def kh_collection(handles, label):
    """Path of the first collection in handles that has products, else None."""
    for handle in handles:
        path = f"/collections/{handle}/products.json"
        try:
            r = requests.get(f"{KH_BASE}{path}", params={"limit": 1}, headers=KH_HEADERS, timeout=60)
            n = len(r.json().get("products", [])) if r.status_code == 200 else 0
        except (requests.RequestException, ValueError) as ex:
            kh_log(f"[KH] {label} collection '{handle}': not readable ({str(ex)[:80]})")
            continue
        kh_log(f"[KH] {label} collection '{handle}': HTTP {r.status_code}, {n} product(s) on a test read")
        if n:
            return path
    return None


def kh_sources():
    """The 2 lists to read: [(path, kind), ...] - fragrances first, then gift sets.
    kind 'collection' = keep everything in it; 'perfume' / 'gift' = whole shop, picked by words."""
    out = []
    path = kh_collection(KH_COLLECTIONS, "fragrance")
    if path:
        out.append((path, "collection"))
    else:
        kh_log("[KH] no fragrance collection found -> whole shop, fragrances picked by words")
        out.append(("/products.json", "perfume"))
    path = kh_collection(KH_GIFT_COLLECTIONS, "gift set")
    if path:
        out.append((path, "collection"))
    else:
        kh_log("[KH] no gift set collection found -> whole shop, gift sets picked by words")
        out.append(("/products.json", "gift"))
    kh_state["source"] = [f"{p} ({k})" for p, k in out]
    return out


def kh_get(page, path="/products.json"):
    """One page of products.json (retries when busy). Returns list of products, [] at the end, None on failure."""
    for attempt, wait in enumerate([0] + KH_RETRY_WAITS):
        if wait:
            kh_log(f"[KH] page {page}: retry {attempt}/{len(KH_RETRY_WAITS)} in {wait}s")
            time.sleep(wait)
        try:
            r = requests.get(f"{KH_BASE}{path}", params={"limit": 250, "page": page},
                             headers=KH_HEADERS, timeout=60)
        except requests.RequestException as ex:
            kh_log(f"[KH] page {page}: network error {ex}")
            continue
        if r.status_code == 429 or r.status_code >= 500:
            kh_log(f"[KH] page {page}: HTTP {r.status_code} (busy)")
            continue
        if r.status_code in (400, 404):
            return []
        if r.status_code != 200:
            raise RuntimeError(f"Khaltat answered HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json().get("products", [])
        except ValueError:
            raise RuntimeError(f"Not a product list (blocked?): {r.text[:200]}")
    return None


# ---------------------------------------------------------------- full download
def kh_run():
    started = time.time()
    st = {"status": "running", "pages": 0, "products": 0, "saved": 0, "in_stock": 0, "out_of_stock": 0,
          "incomplete": False}
    kh_state["last"] = st
    conn = kh_conn()
    cur = conn.cursor()
    try:
        cur.execute(KH_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()
        sources = kh_sources()                              # fragrances + gift sets
        st["source"] = [f"{p} ({k})" for p, k in sources]
        seen = set()                                        # variant ids already saved (product in both lists)
        for path, kind in sources:
            if st["incomplete"]:
                break
            kh_log(f"[KH] reading {path} ({kind})")
            for page in range(1, KH_MAX_PAGES + 1):
                products = kh_get(page, path)
                if products is None:
                    st["incomplete"] = True
                    kh_log(f"[KH] {path} page {page}: gave up -> stopping (run is incomplete)")
                    break
                if not products:
                    break
                items = [i for i in kh_items(products, kind) if i["variant_id"] and i["variant_id"] not in seen]
                seen.update(i["variant_id"] for i in items)
                st["skipped_not_perfume"] = st.get("skipped_not_perfume", 0) + sum(
                    1 for p in products if not kh_is_perfume(p, kind))
                rows = [(i["variant_id"], i["url"], i["gtin"], i["sku"], i["brand"], i["name"], i["product_type"],
                         i["price_inc_tax"], i["price"], i["stock"], i["volume"]) for i in items]
                if rows:
                    execute_values(cur, KH_UPSERT, rows)
                    conn.commit()
                st["pages"] += 1
                st["products"] += len(products)
                st["saved"] += len(rows)
                st["in_stock"] += sum(i["stock"] == "In stock" for i in items)
                st["out_of_stock"] += sum(i["stock"] != "In stock" for i in items)
                st["seconds"] = round(time.time() - started, 1)
                kh_log(f"[KH] {path} page {page}: {len(products)} products, {len(rows)} new variants saved "
                       f"(total {st['saved']})")
                if len(products) < 250:
                    break
                time.sleep(1.5)
        if not st["incomplete"] and st["saved"]:             # products gone from the site
            cur.execute("UPDATE khaltat_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            st["marked_not_on_site"] = cur.rowcount
            conn.commit()
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        kh_log(f"[KH-DONE] {st}")


def kh_worker():
    try:
        kh_run()
    except Exception as ex:
        kh_log(f"[KH-ERROR] {ex}\n{traceback.format_exc()}")
        kh_state["last"] = {**(kh_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        kh_state["running"] = False
        kh_lock.release()


# ---------------------------------------------------------------- routes
def kh_secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


@khaltat_bp.route("/khaltat/test")
def kh_test_route():
    """Read page 1 only - shows whether Render can read Khaltat and what is read."""
    if not kh_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        lists = []
        for path, kind in kh_sources():                     # fragrances + gift sets, page 1 of each
            products = kh_get(1, path) or []
            items = kh_items(products, kind)
            lists.append({"source": f"{path} ({kind})", "page1_products": len(products),
                          "page1_variants": len(items),
                          "skipped": [p.get("title") for p in products if not kh_is_perfume(p, kind)][:30],
                          "product_types": sorted({i["product_type"] or "-" for i in items})[:20],
                          "first_3": items[:3]})
    except RuntimeError as ex:
        kh_log(f"[KH-TEST] {ex}")
        return jsonify({"ok": False, "error": str(ex)})
    result = {"ok": any(x["page1_variants"] for x in lists), "lists": lists}
    kh_log(f"[KH-TEST] {result}")
    return jsonify(result)


@khaltat_bp.route("/khaltat/run")
def kh_run_route():
    if not kh_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not kh_lock.acquire(blocking=False):
        return jsonify({"error": "Khaltat download already running"}), 409
    kh_state["running"] = True
    kh_log("[KH-RUN] Started")
    threading.Thread(target=kh_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/khaltat/status"}), 202


@khaltat_bp.route("/khaltat/status")
def kh_status_route():
    return jsonify({"running": kh_state["running"], "last": kh_state["last"]})
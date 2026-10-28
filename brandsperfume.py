"""
Brands Perfume catalog (brandsperfume.com -> Postgres french_fragrance_db, table brandsperfume_catalog)
Made from perfumedubai.py (same Shopify way of reading, PERFUMES ONLY). Prefix br (bp = Branded Perfume).

Brands Perfume is read like a Shopify store (like Perfume Dubai / Amberiya): public products.json.
  (Could not be checked from Claude's side - /brandsperfume/test shows at once whether it works: an error
   'Not a product list' or HTTP 404 = NOT Shopify -> tell Claude, it needs another way of reading.)
ONLY PERFUMES are saved (no make up, skincare, bags, gift sets, body mists ...):
  1. the shop's PERFUME collection is read: the first of BR_COLLECTIONS that has products, e.g.
       https://brandsperfume.com/collections/perfume/products.json?limit=250&page=1, 2, 3 ...
     (no such collection -> the whole shop /products.json, and only products that look like perfume: BR_PERFUME_WORDS)
  2. then products whose title / type has a word of BR_SKIP_WORDS are left out (gift set, body mist, deodorant ...)
  /brandsperfume/test shows which collection was used + the product types / skipped titles -> check it first.
One row per variant (a product with 50ml + 100ml = 2 rows).
Brand = Shopify "vendor" (e.g. "Tom Ford"), name = product title + size, link opens the right size.

Routes (open in the browser):
  GET /brandsperfume/test?secret=XXX    read page 1 only and SHOW what was found (run this first)
  GET /brandsperfume/run?secret=XXX     download everything into brandsperfume_catalog (background, ~1 min)
  GET /brandsperfume/status             progress

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

brandsperfume_bp = Blueprint("brandsperfume", __name__)

BR_BASE = "https://brandsperfume.com"
BR_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
              "Accept": "application/json"}
BR_MAX_PAGES = 100              # Shopify limit: max 100 pages x 250 = 25,000 products
BR_RETRY_WAITS = [10, 30, 60, 120]
# ONLY PERFUMES - change these lists if /brandsperfume/test shows wrong products:
BR_COLLECTIONS = ["perfume", "perfumes", "fragrance", "fragrances", "all-perfumes"]   # tried in this order
# used only when none of BR_COLLECTIONS exists: a product must have one of these words in its title / type
BR_PERFUME_WORDS = ["perfume", "parfum", "toilette", "cologne", "edp", "edt", "extrait", "fragrance", "attar", "oud"]
# always left out: title or product type has one of these WHOLE words (any capitals). "moisturi*" = word start.
BR_SKIP_WORDS = ["gift set", "giftset", "coffret", "set", "body mist", "hair mist", "body spray", "deodorant",
                 "deo", "body lotion", "body cream", "shower gel", "bath", "soap", "candle", "diffuser",
                 "lipstick", "mascara", "foundation", "serum", "moisturi*", "cleanser", "bag", "pouch",
                 "gift card", "sample"]

br_lock = threading.Lock()
br_state = {"running": False, "last": None, "source": None}   # source = collection used (or "all products")


def br_log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- database
BR_CREATE = """
CREATE TABLE IF NOT EXISTS brandsperfume_catalog (
    id            SERIAL PRIMARY KEY,
    variant_id    BIGINT UNIQUE NOT NULL,   -- Shopify variant id (one row per size)
    product_url   TEXT,                     -- https://brandsperfume.com/products/...?variant=...
    gtin          TEXT,                     -- barcode, only when their SKU is a real barcode
    sku           TEXT,                     -- their SKU as written in Shopify
    brand         TEXT,                     -- Shopify vendor, e.g. "Tom Ford"
    name          TEXT,                     -- "Oud Wood EDP 50 ml"
    product_type  TEXT,                     -- Shopify product type, e.g. "Eau de Parfum"
    price_inc_tax NUMERIC(10,2),            -- price shown on the site (UAE prices include 5% VAT)
    price         NUMERIC(10,2),            -- without VAT (price_inc_tax / 1.05)
    stock         TEXT,                     -- In stock / Out of stock / Not on site
    volume        TEXT,                     -- e.g. "100 ml"
    updated_at    TIMESTAMP DEFAULT NOW()
);
"""

BR_UPSERT = """
INSERT INTO brandsperfume_catalog (variant_id, product_url, gtin, sku, brand, name, product_type,
                              price_inc_tax, price, stock, volume)
VALUES %s
ON CONFLICT (variant_id) DO UPDATE SET
    product_url = EXCLUDED.product_url, gtin = EXCLUDED.gtin, sku = EXCLUDED.sku, brand = EXCLUDED.brand,
    name = EXCLUDED.name, product_type = EXCLUDED.product_type, price_inc_tax = EXCLUDED.price_inc_tax,
    price = EXCLUDED.price, stock = EXCLUDED.stock, volume = EXCLUDED.volume, updated_at = NOW();
"""


def br_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    br_log("[BR-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ---------------------------------------------------------------- reading products
def br_volume(*texts):
    """'100ml' / 'Oud Wood EDP 100 ML' -> '100 ml'"""
    for t in texts:
        m = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", str(t or ""), re.I)
        if m:
            return f"{m.group(1)} ml"
    return None


def br_is_perfume(p, whole_shop):
    """True = keep this Shopify product. whole_shop = read from /products.json (no perfume collection found)."""
    text = f" {p.get('title') or ''} {p.get('product_type') or ''} ".lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)                 # "Gift-Set" -> " gift set "
    for w in BR_SKIP_WORDS:                                 # whole words only ("set" does not hit "settings")
        pat = rf"\b{re.escape(w.rstrip('*'))}" + ("" if w.endswith("*") else r"\b")
        if re.search(pat, text):
            return False
    return not whole_shop or any(re.search(rf"\b{w}\b", text) for w in BR_PERFUME_WORDS)


def br_items(products, whole_shop=False):
    """Shopify products -> one dict per variant (perfumes only, see br_is_perfume)."""
    out = []
    for p in products:
        if not br_is_perfume(p, whole_shop):
            continue
        title = (p.get("title") or "").strip()
        variants = p.get("variants", [])
        for v in variants:
            vtitle = (v.get("title") or "").strip()
            name = title if vtitle.lower() in ("", "default title") else f"{title} - {vtitle}"
            url = f"{BR_BASE}/products/{p.get('handle')}"
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
                "volume": br_volume(vtitle, title, *(p.get("tags") or [])),   # tags: "100ml"
            })
    return out


def br_source():
    """Path of the perfume list: /collections/<first of BR_COLLECTIONS with products>/products.json,
    or /products.json (whole shop, filtered by BR_PERFUME_WORDS) when none exists. Remembered in br_state."""
    for handle in BR_COLLECTIONS:
        path = f"/collections/{handle}/products.json"
        try:
            r = requests.get(f"{BR_BASE}{path}", params={"limit": 1}, headers=BR_HEADERS, timeout=60)
            n = len(r.json().get("products", [])) if r.status_code == 200 else 0
        except (requests.RequestException, ValueError) as ex:
            br_log(f"[BR] collection '{handle}': not readable ({str(ex)[:80]})")
            continue
        br_log(f"[BR] collection '{handle}': HTTP {r.status_code}, {n} product(s) on a test read")
        if n:
            br_state["source"] = path
            return path
    br_log("[BR] no perfume collection found -> whole shop /products.json, perfumes picked by words")
    br_state["source"] = "/products.json"
    return "/products.json"


def br_get(page, path="/products.json"):
    """One page of products.json (retries when busy). Returns list of products, [] at the end, None on failure."""
    for attempt, wait in enumerate([0] + BR_RETRY_WAITS):
        if wait:
            br_log(f"[BR] page {page}: retry {attempt}/{len(BR_RETRY_WAITS)} in {wait}s")
            time.sleep(wait)
        try:
            r = requests.get(f"{BR_BASE}{path}", params={"limit": 250, "page": page},
                             headers=BR_HEADERS, timeout=60)
        except requests.RequestException as ex:
            br_log(f"[BR] page {page}: network error {ex}")
            continue
        if r.status_code == 429 or r.status_code >= 500:
            br_log(f"[BR] page {page}: HTTP {r.status_code} (busy)")
            continue
        if r.status_code in (400, 404):
            return []
        if r.status_code != 200:
            raise RuntimeError(f"Brands Perfume answered HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json().get("products", [])
        except ValueError:
            raise RuntimeError(f"Not a product list (blocked?): {r.text[:200]}")
    return None


# ---------------------------------------------------------------- full download
def br_run():
    started = time.time()
    st = {"status": "running", "pages": 0, "products": 0, "saved": 0, "in_stock": 0, "out_of_stock": 0,
          "incomplete": False}
    br_state["last"] = st
    conn = br_conn()
    cur = conn.cursor()
    try:
        cur.execute(BR_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()
        path = br_source()                                  # perfume collection (or whole shop)
        whole_shop = path == "/products.json"
        st["source"] = path
        for page in range(1, BR_MAX_PAGES + 1):
            products = br_get(page, path)
            if products is None:
                st["incomplete"] = True
                br_log(f"[BR] page {page}: gave up -> stopping (run is incomplete)")
                break
            if not products:
                break
            items = br_items(products, whole_shop)
            st["skipped_not_perfume"] = st.get("skipped_not_perfume", 0) + sum(
                1 for p in products if not br_is_perfume(p, whole_shop))
            rows = [(i["variant_id"], i["url"], i["gtin"], i["sku"], i["brand"], i["name"], i["product_type"],
                     i["price_inc_tax"], i["price"], i["stock"], i["volume"]) for i in items if i["variant_id"]]
            if rows:
                execute_values(cur, BR_UPSERT, rows)
                conn.commit()
            st["pages"] = page
            st["products"] += len(products)
            st["saved"] += len(rows)
            st["in_stock"] += sum(i["stock"] == "In stock" for i in items)
            st["out_of_stock"] += sum(i["stock"] != "In stock" for i in items)
            st["seconds"] = round(time.time() - started, 1)
            br_log(f"[BR] page {page}: {len(products)} products, {len(rows)} variants saved (total {st['saved']})")
            if len(products) < 250:
                break
            time.sleep(1.5)
        if not st["incomplete"] and st["saved"]:             # products gone from the site
            cur.execute("UPDATE brandsperfume_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            st["marked_not_on_site"] = cur.rowcount
            conn.commit()
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        br_log(f"[BR-DONE] {st}")


def br_worker():
    try:
        br_run()
    except Exception as ex:
        br_log(f"[BR-ERROR] {ex}\n{traceback.format_exc()}")
        br_state["last"] = {**(br_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        br_state["running"] = False
        br_lock.release()


# ---------------------------------------------------------------- routes
def br_secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


# ---------------------------------------------------------------- shop check (when products.json gives nothing)
# (name, path to try, word that must be in the answer) - the usual public product lists of other shop systems
BR_PROBES = [("Shopify products.json", "/products.json?limit=1", '"products"'),
             ("WooCommerce Store API", "/wp-json/wc/store/v1/products?per_page=1", '"prices"'),
             ("WooCommerce sitemap", "/product-sitemap.xml", "<loc>"),
             ("Sitemap", "/sitemap.xml", "<loc>"),
             ("Magento GraphQL", "/graphql?query={storeConfig{store_name}}", "storeConfig")]
# words in the HOME page that tell the shop system
BR_PLATFORM_WORDS = {"Shopify": ["cdn.shopify.com", "Shopify.theme"], "WooCommerce": ["woocommerce", "wp-content"],
                     "Salla": ["salla.sa", "salla.network"], "Zid": ["zid.store", "zidcdn"],
                     "Magento": ["Magento_", "mage/cookies"], "OpenCart": ["route=product"],
                     "Wix": ["wixstatic.com"], "Squarespace": ["squarespace"], "BigCommerce": ["bigcommerce"],
                     "Cloudflare block": ["cf-chl", "Just a moment..."]}


def br_check_shop():
    """What answers at brandsperfume.com from Render: home page status, shop system, which product list works."""
    out = {"home": None, "platform": [], "probes": {}}
    try:
        r = requests.get(BR_BASE + "/", headers={**BR_HEADERS, "Accept": "text/html"}, timeout=40)
        html = r.text or ""
        out["home"] = {"status": r.status_code, "final_url": r.url, "server": r.headers.get("server"),
                       "powered_by": r.headers.get("x-powered-by"), "size": len(html),
                       "title": (re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I) or [None, None])[1]}
        out["platform"] = [name for name, words in BR_PLATFORM_WORDS.items() if any(w.lower() in html.lower() for w in words)]
    except requests.RequestException as ex:
        out["home"] = {"error": str(ex)[:200]}
    for name, path, word in BR_PROBES:
        try:
            r = requests.get(BR_BASE + path, headers={**BR_HEADERS, "Accept": "*/*"}, timeout=40)
            out["probes"][name] = {"status": r.status_code, "works": r.ok and word in (r.text or ""),
                                   "start": (r.text or "")[:120]}
        except requests.RequestException as ex:
            out["probes"][name] = {"error": str(ex)[:120]}
    out.update(br_check_deeper())
    br_log(f"[BR-CHECK] {out}")
    return out


def br_locs(xml):
    """All <loc> links of a sitemap (index or list)."""
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml or "")


def br_check_deeper():
    """Custom web app (no shop system found): read the sitemap, open ONE product page and look in the site's
    JavaScript for the API it loads its products from. Everything shown -> Claude builds the reader from it."""
    out = {"sitemaps": {}, "product_page": None, "api_hints": []}
    try:
        index = requests.get(BR_BASE + "/sitemap.xml", headers={**BR_HEADERS, "Accept": "*/*"}, timeout=40).text
        children = br_locs(index)
        out["sitemap_index"] = children[:20]
        product_url = None
        for sm in [c for c in children if c.endswith(".xml")][:6] or ([BR_BASE + "/sitemap.xml"] if not children else []):
            r = requests.get(sm, headers={**BR_HEADERS, "Accept": "*/*"}, timeout=40)
            links = br_locs(r.text)
            out["sitemaps"][sm] = {"status": r.status_code, "links": len(links), "sample": links[:4]}
            product_url = product_url or next((l for l in links if re.search(r"/(product|products|p|item)s?/", l)), None)
        if not product_url and children and not children[0].endswith(".xml"):    # plain list of pages
            product_url = next((l for l in children if re.search(r"/(product|products|p|item)s?/", l)), None)
        if not product_url:                              # no product link -> read the home page's code instead
            product_url = BR_BASE + "/"
        if product_url:
            r = requests.get(product_url, headers={**BR_HEADERS, "Accept": "text/html"}, timeout=40)
            html = r.text or ""
            out["product_page"] = {
                "url": product_url, "status": r.status_code, "size": len(html),
                "title": (re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I) or [None, None])[1],
                "ld_json": "application/ld+json" in html, "og_price": bool(re.search(r"product:price|og:price", html)),
                "aed_in_page": len(re.findall(r"AED|د\.إ", html)),
                "scripts": re.findall(r'<script[^>]+src="([^"]+)"', html)[:8],
            }
            # API address: look inside the site's own JavaScript files
            hints = set()
            for src in out["product_page"]["scripts"][:4]:
                js_url = src if src.startswith("http") else BR_BASE + "/" + src.lstrip("/")
                try:
                    js = requests.get(js_url, headers={**BR_HEADERS, "Accept": "*/*"}, timeout=40).text
                except requests.RequestException:
                    continue
                hints.update(re.findall(r'https?://[a-zA-Z0-9.\-]+(?:/[a-zA-Z0-9_\-./]*)?(?:api|graphql|products?)[a-zA-Z0-9_\-./]*', js))
                hints.update(re.findall(r'["\'`](/(?:api|v1|v2|graphql)/[a-zA-Z0-9_\-./]*)', js))
            out["api_hints"] = sorted(hints)[:40]
    except requests.RequestException as ex:
        out["deeper_error"] = str(ex)[:200]
    return out


@brandsperfume_bp.route("/brandsperfume/test")
def br_test_route():
    """Read page 1 only - shows whether Render can read Brands Perfume and what is read."""
    if not br_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        path = br_source()
        whole_shop = path == "/products.json"
        products = br_get(1, path)
    except RuntimeError as ex:
        br_log(f"[BR-TEST] {ex}")
        return jsonify({"ok": False, "error": str(ex), "shop_check": br_check_shop()})
    items = br_items(products or [], whole_shop)
    result = {"ok": bool(items), "source": path, "page1_products": len(products or []), "page1_variants": len(items),
              "skipped_not_perfume": [p.get("title") for p in (products or []) if not br_is_perfume(p, whole_shop)][:30],
              "with_sku": sum(1 for i in items if i["sku"]), "with_barcode": sum(1 for i in items if i["gtin"]),
              "brands_seen": sorted({i["brand"] or "-" for i in items})[:20],
              "product_types": sorted({i["product_type"] or "-" for i in items})[:20],
              "first_3": items[:3]}
    if not items:                                       # nothing read -> show what the site really is (send to Claude)
        result["shop_check"] = br_check_shop()
    br_log(f"[BR-TEST] {result}")
    return jsonify(result)


@brandsperfume_bp.route("/brandsperfume/run")
def br_run_route():
    if not br_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not br_lock.acquire(blocking=False):
        return jsonify({"error": "Brands Perfume download already running"}), 409
    br_state["running"] = True
    br_log("[BR-RUN] Started")
    threading.Thread(target=br_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/brandsperfume/status"}), 202


@brandsperfume_bp.route("/brandsperfume/status")
def br_status_route():
    return jsonify({"running": br_state["running"], "last": br_state["last"]})
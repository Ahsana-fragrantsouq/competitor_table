"""
Emirates Pride catalog (emiratespride.com -> Postgres french_fragrance_db, table emiratespride_catalog)
Plugged into app.py as a Blueprint. Made from perfumedubai.py (same Shopify way of reading).
RUNS ON THE PC (Run 1, PC_LISTS in daily_update.py -> ep_scraper.py): the UAE pages answer "404" to Render's US
servers but work from the PC (UAE internet). The /emiratespride/... routes on Render therefore find nothing.
Shown in the menu "Brands" (not "Competitors") - a brand's own shop; everything else works like a competitor.

Emirates Pride is a Shopify store with a UAE part (/ae), so its product list is public.
The product LIST comes from the Shopify shop (first address of EP_API_BASES that answers, = the main address).
Its prices are NOT the UAE prices (Midnight Bloom: 245 there, AED 585 on the UAE site) -> every product is opened
on the UAE website itself: emiratespride.com/ae/product/<handle> (EP_PRODUCT_PAGE, ep_uae):
  page not found = not sold in the UAE -> left out; found = AED price (EP_PRICE_PATTERNS) + stock from that page.
  No AED price found on the page -> saved WITHOUT price (never a wrong price). Links open that UAE page.
Size options that are sets / oils ("100ml + 8ml Oil") are left out too (ep_variant_ok).
  /emiratespride/test shows the prices: compare one with the website (must be AED).
ONLY PERFUMES are saved (no make up, skincare, bags, gift sets, body mists ...):
  1. the shop's PERFUME collection is read: the first of EP_COLLECTIONS that has products, e.g.
       https://emiratespride.com/ae/collections/perfume/products.json?limit=250&page=1, 2, 3 ...
     (no such collection -> the whole shop /products.json, and only products that look like perfume: EP_PERFUME_WORDS)
  2. then products whose title / type has a word of EP_SKIP_WORDS are left out (body mist, deodorant, oils ...)
  3. GIFT SETS (user's choice): the first of EP_GIFT_COLLECTIONS with products, EVERYTHING in it except EP_GIFT_SKIP
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
try:
    from flask import Blueprint, request, jsonify
except ImportError:                      # on the PC (ep_scraper.py) Flask may be missing: the routes are not needed there
    class Blueprint:                     # stand-in: @emiratespride_bp.route(...) just returns the function
        def __init__(self, *a, **k):
            pass

        def route(self, *a, **k):
            return lambda f: f
    request = jsonify = None

emiratespride_bp = Blueprint("emiratespride", __name__)

EP_BASE = "https://emiratespride.com/ae"      # UAE part of the shop: product links start with this
# where the product lists are read (tried in this order; /emiratespride/test shows the HTTP answer of each one)
EP_API_BASES = ["https://emiratespride.com/ae", "https://emiratespride.com", "https://www.emiratespride.com"]
EP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
              "Accept": "application/json"}
EP_MAX_PAGES = 100              # Shopify limit: max 100 pages x 250 = 25,000 products
EP_RETRY_WAITS = [10, 30, 60, 120]
# PERFUMES (+ GIFT SETS below) - change these lists if /emiratespride/test shows wrong products:
# Emirates Pride menu "ALL FRAGRANCES" = their perfumes (Gift Sets / Home Collection: oud, dakhoon, oils = separate)
EP_COLLECTIONS = ["all-fragrances", "fragrances", "perfumes", "perfume", "fragrance"]   # tried in this order
# GIFT SETS too (user's choice 2026-10-29): first of these collections that has products -> EVERYTHING in it is kept
# (only EP_GIFT_SKIP left out; the "100ml + oil" size rule ep_variant_ok is NOT used there - sets are combos)
EP_GIFT_COLLECTIONS = ["gift-sets", "gift-set", "giftsets", "gifts", "gift", "gifting"]
EP_GIFT_SKIP = ["gift card", "giveaway*", "sample"]
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


def ep_is_perfume(p, whole_shop, gift=False):
    """True = keep this Shopify product. whole_shop = read from /products.json (no perfume collection found).
    gift = from the GIFT SETS collection -> everything kept except EP_GIFT_SKIP."""
    text = f" {p.get('title') or ''} {p.get('product_type') or ''} ".lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)                 # "Gift-Set" -> " gift set "
    if gift:
        return not any(re.search(rf"\b{re.escape(w.rstrip('*'))}" + ("" if w.endswith("*") else r"\b"), text)
                       for w in EP_GIFT_SKIP)
    for w in EP_SKIP_WORDS:                                 # whole words only ("set" does not hit "settings")
        pat = rf"\b{re.escape(w.rstrip('*'))}" + ("" if w.endswith("*") else r"\b")
        if re.search(pat, text):
            return False
    return not whole_shop or any(re.search(rf"\b{w}\b", text) for w in EP_PERFUME_WORDS)


EP_PRODUCT_PAGE = EP_BASE + "/product/{handle}"   # the UAE shop's own product page (singular "product")
# The site answers by LOCATION: Render (USA) gets the Shopify list but NOT the UAE pages; the PC (UAE) gets the UAE
# pages but NOT the list. So: Render reads the LIST (EP_CHECK_UAE = False -> saved WITHOUT price), then the PC
# (ep_scraper.py -> ep_fill_prices) opens every UAE page and fills AED price + stock.
EP_CHECK_UAE = False
# the AED price on that page, tried in this order (page data / meta tags / structured data)
EP_PRICE_PATTERNS = [
    r'product:price:amount"\s+content="([0-9]+(?:\.[0-9]+)?)"',
    r'itemprop="price"\s+content="([0-9]+(?:\.[0-9]+)?)"',
    r'"price"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?\s*,\s*"priceCurrency"\s*:\s*"AED"',
    r'"priceCurrency"\s*:\s*"AED"\s*,\s*"price"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)',
    r'"amount"\s*:\s*"([0-9]+(?:\.[0-9]+)?)"\s*,\s*"currencyCode"\s*:\s*"AED"',
    r'"currencyCode"\s*:\s*"AED"\s*,\s*"amount"\s*:\s*"([0-9]+(?:\.[0-9]+)?)"',
]


def ep_uae(handle):
    """The product as the UAE shop sells it, read from its own page EP_PRODUCT_PAGE (emiratespride.com/ae/product/...).
    -> None when the page does not exist (not sold in the UAE), else {"price": AED or None, "available": bool or None,
    "pattern": which EP_PRICE_PATTERNS found the price}. Network trouble -> {} (price left empty, nothing guessed)."""
    url = EP_PRODUCT_PAGE.format(handle=handle)
    try:
        r = requests.get(url, headers={**EP_HEADERS, "Accept": "text/html"}, timeout=60)
    except requests.RequestException as ex:
        ep_log(f"[EP-UAE] {handle}: network error {str(ex)[:80]}")
        return {}
    html = r.text or ""
    up = html.upper()
    if r.status_code == 404 or ("ERROR 404" in up and "PAGE NOT FOUND" in up):   # their "PAGE NOT FOUND" page
        ep_state.setdefault("uae_checks", []).append({"page": url, "answer": f"HTTP {r.status_code}: not found"})
        return None
    price, pattern = None, None
    for n, pat in enumerate(EP_PRICE_PATTERNS, 1):
        m = re.search(pat, html)
        if m:
            price, pattern = round(float(m.group(1)), 2), n
            break
    # stock = structured data of the page only ("schema.org/InStock" / "OutOfStock"); none -> None (main shop stock)
    available = (False if re.search(r"schema\.org/(OutOfStock|SoldOut)", html)
                 else True if re.search(r"schema\.org/InStock", html) else None)
    ep_log(f"[EP-UAE] {handle}: AED {price} (pattern {pattern}), available {available}")
    ep_state.setdefault("uae_checks", []).append({"page": url, "answer": f"HTTP {r.status_code}", "aed_price": price,
                                                  "price_pattern": pattern, "in_stock": available})
    return {"price": price, "available": available, "pattern": pattern}


def ep_fill_prices(conn):
    """PC part: every saved product (one UAE page per product_url) -> AED price + stock from its UAE page.
    Page not found -> 'Not on site' (not sold in the UAE). Several sizes on one page -> no price (cannot tell
    which size the page price is). Returns a summary dict."""
    cur = conn.cursor()
    cur.execute("SELECT product_url, COUNT(*) FROM emiratespride_catalog WHERE product_url LIKE %s "
                "AND COALESCE(stock, '') <> 'Not on site' GROUP BY product_url ORDER BY product_url",
                (EP_PRODUCT_PAGE.format(handle="") + "%",))
    pages = cur.fetchall()
    res = {"pages": len(pages), "priced": 0, "not_in_uae": 0, "no_price": 0, "several_sizes": 0}
    ep_log(f"[EP-PRICES] {len(pages)} UAE pages to read")
    for i, (url, n) in enumerate(pages, 1):
        handle = url.rsplit("/", 1)[-1].split("?")[0]
        uae = ep_uae(handle)
        if uae is None:                                   # not sold in the UAE
            cur.execute("UPDATE emiratespride_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE product_url = %s", (url,))
            res["not_in_uae"] += 1
        elif n > 1:
            res["several_sizes"] += 1
            ep_log(f"[EP-PRICES] {handle}: {n} sizes on one page -> price left empty")
        elif uae.get("price"):
            stock_sql = "" if uae.get("available") is None else ", stock = %s"
            params = [uae["price"], round(uae["price"] / 1.05, 2)]
            if uae.get("available") is not None:
                params.append("In stock" if uae["available"] else "Out of stock")
            cur.execute(f"UPDATE emiratespride_catalog SET price_inc_tax = %s, price = %s{stock_sql}, updated_at = NOW() "
                        "WHERE product_url = %s", params + [url])
            res["priced"] += 1
        else:
            res["no_price"] += 1
        conn.commit()
        if i % 10 == 0:
            ep_log(f"[EP-PRICES] {i}/{len(pages)} pages read")
        time.sleep(0.5)                                   # be gentle with their website
    ep_log(f"[EP-PRICES] Done: {res}")
    return res


def ep_variant_ok(vtitle):
    """False for a size option that is really a set / an oil: "100ml + 8ml Oil", "Refill + Oil" ..."""
    t = " " + re.sub(r"[^a-z0-9+]+", " ", (vtitle or "").lower()) + " "
    return "+" not in t and not re.search(r"\boils?\b", t)


def ep_items(products, whole_shop=False, check_uae=True, skipped=None, gift=False):
    """Shopify products -> one dict per variant (perfumes only, see ep_is_perfume).
    check_uae: every product is looked up in the UAE shop (ep_uae): not sold there -> left out;
    sold there -> UAE price + stock. skipped (list) collects the left-out names for /emiratespride/test."""
    out = []
    for p in products:
        if not ep_is_perfume(p, whole_shop, gift):
            continue
        title = (p.get("title") or "").strip()
        uae = ep_uae(p.get("handle")) if check_uae else {"price": None, "available": None}   # list only: no price
        if uae is None:                                   # page not found in the UAE shop -> not sold in UAE
            ep_log(f"[EP-UAE] {title}: not in the UAE shop -> skipped")
            if skipped is not None:
                skipped.append(f"{title} (not in UAE shop)")
            continue
        if check_uae:
            time.sleep(0.3)                               # be gentle: one request per product
        variants = [v for v in p.get("variants", []) if gift or ep_variant_ok(v.get("title"))]   # gift set: all
        for v in p.get("variants", []):
            if not gift and not ep_variant_ok(v.get("title")) and skipped is not None:
                skipped.append(f"{title} - {v.get('title')} (set / oil)")
        for v in variants:
            vtitle = (v.get("title") or "").strip()
            name = title if vtitle.lower() in ("", "default title") else f"{title} - {vtitle}"
            url = EP_PRODUCT_PAGE.format(handle=p.get("handle"))   # UAE page (the main shop's prices are NOT AED)
            # price = the AED price shown on the UAE page; one page = one price -> only for a single-size product
            price_inc = uae.get("price") if len(variants) == 1 else None
            if price_inc is None and check_uae:
                ep_log(f"[EP-UAE] {name}: no AED price read -> saved without price (not used for least price)")
            available = uae.get("available")
            if available is None:
                available = bool(v.get("available"))             # page did not say -> main shop stock
            sku = (v.get("sku") or "").strip() or None
            digits = re.sub(r"\D", "", sku or "")
            out.append({
                "variant_id": v.get("id"), "url": url,
                "gtin": digits if sku and digits == sku and len(digits) >= 8 else None,   # pure barcode SKU only
                "sku": sku, "brand": (p.get("vendor") or "").strip() or None, "name": name,
                "product_type": (p.get("product_type") or "").strip() or None,
                "price_inc_tax": price_inc, "price": round(price_inc / 1.05, 2) if price_inc else None,
                "stock": "In stock" if available else "Out of stock",
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


def ep_gift_source():
    """Path of the GIFT SETS collection on the address ep_source() found (None when there is none)."""
    base = ep_state.get("api_base") or EP_API_BASES[0]
    for h in EP_GIFT_COLLECTIONS:
        n, _ = ep_try(f"{base}/collections/{h}/products.json")
        if n:
            ep_log(f"[EP] gift sets: using {base}/collections/{h}/products.json")
            return f"/collections/{h}/products.json"
    ep_log("[EP] no gift set collection found -> gift sets not read")
    return None


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
        st["source"] = path
        st["api_base"] = ep_state.get("api_base")
        sources = [(path, False)]                           # (path, gift?) - perfumes first, then gift sets
        gift_path = ep_gift_source()
        if gift_path:
            sources.append((gift_path, True))
        st["gift_source"] = gift_path
        seen = set()                                        # variant ids already saved (product in both lists)
        for path, gift in sources:
            whole_shop = path == "/products.json"
            for page in range(1, EP_MAX_PAGES + 1):
                products = ep_get(page, path)
                if products is None:
                    st["incomplete"] = True
                    ep_log(f"[EP] {path} page {page}: gave up -> stopping (run is incomplete)")
                    break
                if not products:
                    break
                skipped = []
                items = [i for i in ep_items(products, whole_shop, check_uae=EP_CHECK_UAE, skipped=skipped, gift=gift)
                         if i["variant_id"] and i["variant_id"] not in seen]
                seen.update(i["variant_id"] for i in items)
                st["skipped_not_perfume"] = st.get("skipped_not_perfume", 0) + sum(
                    1 for p in products if not ep_is_perfume(p, whole_shop, gift))
                st["skipped_not_in_uae_or_set"] = st.get("skipped_not_in_uae_or_set", 0) + len(skipped)
                rows = [(i["variant_id"], i["url"], i["gtin"], i["sku"], i["brand"], i["name"], i["product_type"],
                         i["price_inc_tax"], i["price"], i["stock"], i["volume"]) for i in items]
                if rows:
                    execute_values(cur, EP_UPSERT, rows)
                    conn.commit()
                st["pages"] += 1
                st["products"] += len(products)
                st["saved"] += len(rows)
                if gift:
                    st["gift_sets_saved"] = st.get("gift_sets_saved", 0) + len(rows)
                st["in_stock"] += sum(i["stock"] == "In stock" for i in items)
                st["out_of_stock"] += sum(i["stock"] != "In stock" for i in items)
                st["seconds"] = round(time.time() - started, 1)
                ep_log(f"[EP] {path} page {page}: {len(products)} products, {len(rows)} variants saved "
                       f"(total {st['saved']})")
                if len(products) < 250:
                    break
                time.sleep(1.5)
            if st["incomplete"]:
                break
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
    ep_state["uae_checks"] = []                          # filled by ep_uae: every UAE page read on this test
    try:
        path = ep_source()
        whole_shop = path == "/products.json"
        products = ep_get(1, path)
    except RuntimeError as ex:
        ep_log(f"[EP-TEST] {ex}")
        return jsonify({"ok": False, "error": str(ex)})
    skipped = []
    items = ep_items(products or [], whole_shop, check_uae=EP_CHECK_UAE, skipped=skipped)
    result = {"ok": bool(items), "source": path, "api_base": ep_state.get("api_base"),
              "tried": ep_state.get("tried", []),
              "skipped_not_in_uae_or_set": skipped,
              "uae_pages": ep_state.get("uae_checks", [])[:15],   # compare these AED prices with the website "page1_products": len(products or []), "page1_variants": len(items),
              "skipped_not_perfume": [p.get("title") for p in (products or []) if not ep_is_perfume(p, whole_shop)][:30],
              "with_sku": sum(1 for i in items if i["sku"]), "with_barcode": sum(1 for i in items if i["gtin"]),
              "brands_seen": sorted({i["brand"] or "-" for i in items})[:20],
              "product_types": sorted({i["product_type"] or "-" for i in items})[:20],
              "first_3": items[:3]}
    gift_path = ep_gift_source()                         # gift sets (user's choice): page 1 of that collection
    result["gift_source"] = gift_path
    if gift_path:
        gproducts = ep_get(1, gift_path) or []
        gitems = ep_items(gproducts, False, check_uae=EP_CHECK_UAE, gift=True)
        result["gift_page1_products"] = len(gproducts)
        result["gift_first_5"] = [f"{i['name']} | {i['brand']}" for i in gitems[:5]]
        result["ok"] = result["ok"] or bool(gitems)
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
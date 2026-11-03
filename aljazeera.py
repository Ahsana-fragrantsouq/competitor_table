"""
Al Jazeera Perfumes catalog (uae.aljazeeraperfumes.com/en -> Postgres french_fragrance_db, table aljazeera_catalog)
Prefix jz (aj = Al Injaz). Menu "Brands Store" (the brand's own shop; COPY of arabianoud.py), works like a competitor
(tab, matching, least price, Run 2).
PERFUMES + GIFT SETS only (user's choice, same as the other Brands Store shops): oils, bakhoor, incense, oud wood,
burners, home / body / hair items are left out (jz_keep).

The shop system could NOT be checked from Claude's side (network blocked, 2026-11-03), so the reader tries 2 ways (mode):
  1. "shopify":  /products.json works -> whole shop list (like Khaltat / Anfasic), filtered by jz_keep
  2. "sitemap":  else /sitemap.xml (none -> the "Sitemap:" lines of robots.txt) -> sitemaps with "product" in the
                 name: ALL their links; other (non-blog) sitemaps: only product-looking links (Salla ".../p123456",
                 "/product/", Magento ".html"), else their English links -> Arabic "/ar/" links switched to
                 English JZ_LANG_PATH "/en/", every link moved to JZ_BASE (UAE site, AED)
                 -> every product page read from its schema.org JSON-LD (same reader as brandsperfume.py: br_parse)
  /aljazeera/test shows which way works + 3 products read (+ the raw page data when nothing could be read)

Routes (open in the browser):
  GET /aljazeera/test?secret=XXX    which way works + first products (run this first)
  GET /aljazeera/run?secret=XXX     download everything into aljazeera_catalog (background)
  GET /aljazeera/status             progress

Env vars (already set on Render): FF_DATABASE_URL, RUN_SECRET
"""

import os
from urllib.parse import urlsplit, urlunsplit
import re
import json
import time
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

import requests
import psycopg2
from psycopg2.extras import execute_values
from flask import Blueprint, request, jsonify

import brandsperfume as br                 # sitemap + JSON-LD product page reader (br_get, br_locs, br_parse, br_raw)

aljazeera_bp = Blueprint("aljazeera", __name__)


# ===== SECTION 1: settings
JZ_BASE = "https://uae.aljazeeraperfumes.com"
JZ_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
              "Accept": "application/json"}
JZ_BRAND = "Al Jazeera Perfumes"        # brand when the shop does not say one (it is their own shop)
JZ_LANG_PATH = "/en/"                      # English pages (the user's link: uae.aljazeeraperfumes.com/en)
JZ_SITEMAP_WORD = "product"                # product sitemaps in /sitemap.xml
JZ_WORKERS = 1                             # ONE page at a time (Arabian Oud answered 429 "too many requests" with 4;
                                           # same safe speed here until we know this site)
JZ_PAGE_PAUSE = 1.5                        # seconds between 2 product pages (be polite -> no 429)
JZ_BUSY_WAITS = [60, 120]                  # still 429 after br_get's own retries -> wait this long, try again
JZ_BATCH = 100                             # rows saved per database write
JZ_MAX_PAGES = 100                         # Shopify: max 100 pages x 250
# PERFUMES + GIFT SETS (jz_keep, in this order). WHOLE words, any capitals; "x*" = word start.
JZ_ALWAYS_SKIP = ["gift card", "sample", "samples", "discovery", "giveaway*"]
JZ_GIFT_WORDS = ["gift set", "gift sets", "giftset", "gift box", "coffret", "set", "collection"]   # -> always kept
JZ_SKIP_WORDS = ["oil", "oils", "dehn", "dahn", "bakhoor", "bukhoor", "bakhour", "incense", "dokhoon", "dakhoon",
                 "mabkhara", "mubakhar", "burner", "wood", "chips", "muattar", "candle", "diffuser", "reed",
                 "room spray", "home spray", "car", "body", "lotion", "cream", "shower", "soap", "hair", "deodorant",
                 "accessories", "bag", "pouch", "box only",
                 "gm", "gms", "gram", "grams", "kg"]   # sold by WEIGHT = oud wood / bakhoor ("Oud 350 - 30 gm"), not perfume
JZ_SKIP_WORDS += ["home fragrance*", "air freshener*", "freshener*", "fabric spray", "linen spray",
                  "aoud & oils"]       # Al Jazeera menu (2026-11-03): HOME FRAGRANCES + AOUD & OILS are not perfumes
JZ_PERFUME_WORDS = ["perfume*", "parfum*", "eau", "edp", "edt", "cologne", "fragrance*", "spray", "extrait"]

jz_lock = threading.Lock()
jz_state = {"running": False, "last": None}


def jz_log(*args):
    print(*args, flush=True)


# ===== SECTION 2: database
JZ_CREATE = """
CREATE TABLE IF NOT EXISTS aljazeera_catalog (
    id            SERIAL PRIMARY KEY,
    variant_id    BIGINT UNIQUE NOT NULL,   -- Shopify variant id, or a number made from link + SKU (sitemap way)
    product_url   TEXT,
    gtin          TEXT,                     -- barcode (12-14 digits)
    sku           TEXT,
    brand         TEXT,
    name          TEXT,
    product_type  TEXT,                     -- shop's product type / category
    price_inc_tax NUMERIC(10,2),            -- price shown on the site (AED, incl. 5% VAT)
    price         NUMERIC(10,2),            -- without VAT
    stock         TEXT,                     -- In stock / Out of stock / Not on site
    volume        TEXT,                     -- e.g. "100 ml"
    updated_at    TIMESTAMP DEFAULT NOW()
);
"""

JZ_UPSERT = """
INSERT INTO aljazeera_catalog (variant_id, product_url, gtin, sku, brand, name, product_type,
                                price_inc_tax, price, stock, volume)
VALUES %s
ON CONFLICT (variant_id) DO UPDATE SET
    product_url = EXCLUDED.product_url, gtin = EXCLUDED.gtin, sku = EXCLUDED.sku, brand = EXCLUDED.brand,
    name = EXCLUDED.name, product_type = EXCLUDED.product_type, price_inc_tax = EXCLUDED.price_inc_tax,
    price = EXCLUDED.price, stock = EXCLUDED.stock, volume = EXCLUDED.volume, updated_at = NOW();
"""


def jz_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    jz_log("[JZ-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ===== SECTION 3: filter (perfumes + gift sets)
def jz_keep(name, category):
    """Order: JZ_ALWAYS_SKIP -> out | gift set word -> kept | JZ_SKIP_WORDS -> out | category given -> must look like
    perfume / gift | else kept (perfume)."""
    text = f"{name or ''} {category or ''}"
    if br.br_has(JZ_ALWAYS_SKIP, text):
        return False
    if br.br_has(JZ_GIFT_WORDS, text):
        return True
    if br.br_has(JZ_SKIP_WORDS, text):
        return False
    if category:
        return br.br_has(JZ_PERFUME_WORDS + ["gift*", "oud"], category) or br.br_has(JZ_PERFUME_WORDS, name)
    return True


# ===== SECTION 4: way 1 - Shopify products.json
def jz_shopify_page(page):
    """One page of /products.json -> list of products, [] at the end / not Shopify, None when it failed."""
    try:
        r = requests.get(f"{JZ_BASE}/products.json", params={"limit": 250, "page": page}, headers=JZ_HEADERS, timeout=60)
    except requests.RequestException as ex:
        jz_log(f"[JZ] products.json page {page}: network error {str(ex)[:80]}")
        return None
    if r.status_code != 200:
        return []
    try:
        return r.json().get("products", [])
    except ValueError:
        return []


def jz_shopify_rows(products):
    """Shopify products -> rows (one per variant), perfumes + gift sets only."""
    rows, skipped = [], []
    for p in products:
        title, ptype = (p.get("title") or "").strip(), (p.get("product_type") or "").strip()
        if not jz_keep(title, ptype):
            skipped.append(title)
            continue
        variants = p.get("variants") or []
        for v in variants:
            vt = (v.get("title") or "").strip()
            name = title if vt.lower() in ("", "default title") else f"{title} - {vt}"
            url = f"{JZ_BASE}/products/{p.get('handle')}" + (f"?variant={v.get('id')}" if len(variants) > 1 else "")
            price = br.br_num(v.get("price"))
            if price is not None and price <= 0:
                continue                                    # AED 0 = free gift copy
            sku = (v.get("sku") or "").strip() or None
            d = re.sub(r"\D", "", sku or "")
            rows.append({"variant_id": v.get("id"), "url": url, "gtin": d if sku and d == sku and 12 <= len(d) <= 14 else None,
                         "sku": sku, "brand": (p.get("vendor") or "").strip() or JZ_BRAND, "name": name,
                         "product_type": ptype or None, "price_inc_tax": price,
                         "price": round(price / 1.05, 2) if price else None,
                         "stock": "In stock" if v.get("available") else "Out of stock",
                         "volume": br.br_volume(vt, title, *(p.get("tags") or []))})
    return rows, skipped


# ===== SECTION 5: way 2 - sitemap + product pages (JSON-LD, brandsperfume.py reader)
JZ_NOT_PRODUCT = (r"/page-\d+|/blog|/brands?/|/c\d+/?$|/(cart|login|account|search|customer|checkout|"
                  r"collections?|categor(y|ies)|pages?|about|contact|faq|privacy|terms|stores?)(\b|/)")   # English links that are NOT products
JZ_PRODUCT_LINK = r"/product-details/|/p/?\d{3,}(?:[/?#]|$)|/products?/|\.html?(?:[?#]|$)"   # Salla p123456 / Shopify-Woo /product(s)/
                                                                           # / Magento ".html" product pages
# A sitemap may list the MAIN store's links (other host): the same products exist on the UAE site -> every link is
# moved to OUR host (JZ_BASE = uae.aljazeeraperfumes.com) -> AED prices


def jz_product_links():
    """Product links from the sitemaps -> (links, {sitemap: counts}, info).
    Salla: /sitemap.xml lists ar/sitemap-1.xml, -2 ... (no "product" in the name) -> every non-blog sitemap is read,
    product links = JZ_PRODUCT_LINK, Arabic links switched to English ("/ar/" -> JZ_LANG_PATH; Salla finds the page
    by its p-number)."""
    status, index = br.br_get(JZ_BASE + "/sitemap.xml", "*/*")
    locs = br.br_locs(index)
    if not locs:                                        # no /sitemap.xml -> the sitemaps named in robots.txt
        rst, robots = br.br_get(JZ_BASE + "/robots.txt", "*/*")
        maps_r = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots or "")
        jz_log(f"[JZ] sitemap.xml HTTP {status}: empty -> robots.txt HTTP {rst}: {len(maps_r)} sitemap(s)")
        for sm in maps_r:
            st2, xml2 = br.br_get(sm, "*/*")
            locs.extend(br.br_locs(xml2))
    xmls = [u for u in locs if u.endswith(".xml")]
    maps = [u for u in xmls if JZ_SITEMAP_WORD in u.lower()] or [u for u in xmls if "blog" not in u.lower()]
    counts, links = {}, []
    if not xmls:                                        # sitemap.xml itself lists the pages
        links = [u for u in locs if re.search(JZ_PRODUCT_LINK, u) and not re.search(JZ_NOT_PRODUCT, u)]
        counts[JZ_BASE + "/sitemap.xml"] = {"status": status, "links": len(locs), "product_links": len(links)}
    for sm in maps:
        st, xml = br.br_get(sm, "*/*")
        found = br.br_locs(xml)
        prods = (found if JZ_SITEMAP_WORD in sm.lower()                    # a "products" sitemap: every link
                 else [u for u in found if re.search(JZ_PRODUCT_LINK, u) and not re.search(JZ_NOT_PRODUCT, u)])
        if not prods:
            # product links with no product mark (like Arabian Oud: .../en/0201020036-oud-350-30-gm) ->
            # take the ENGLISH links (no language in the links -> all of them), minus info pages and blog;
            # pages without product data are skipped later by the JSON-LD reader
            eng = [u for u in found if JZ_LANG_PATH in u] or [u for u in found if "/ar/" not in u]
            prods = [u for u in eng if not re.search(JZ_NOT_PRODUCT, u)]
        counts[sm] = {"status": st, "links": len(found), "product_links": len(prods),
                      "sample": found[:5]}                                   # shown on /aljazeera/test
        links.extend(prods)
    base = urlsplit(JZ_BASE)                                                 # https://uae.aljazeeraperfumes.com
    links = [urlunsplit(urlsplit(u)._replace(netloc=base.netloc, scheme=base.scheme)).replace("/ar/", JZ_LANG_PATH)
             for u in links]                                                 # UAE site, English page
    pnum = lambda u: (re.search(r"/p/?(\d{3,})(?:[/?#]|$)", u) or [None, u])[1]   # same p-number twice -> once
    seen, out = set(), []
    for u in links:
        if pnum(u) not in seen:
            seen.add(pnum(u))
            out.append(u)
    jz_log(f"[JZ] sitemap.xml HTTP {status}: {len(maps)} sitemap(s) read, {len(out)} product links")
    return out, counts, {"status": status, "index_sample": locs[:15]}


def jz_page(url):
    """One product page -> (status, rows kept, names skipped). Pauses JZ_PAGE_PAUSE first; when the shop still
    says "too busy" (429 -> br_get gives up with status 0) it waits JZ_BUSY_WAITS and tries again."""
    time.sleep(JZ_PAGE_PAUSE)
    status, html = br.br_get(url)
    for wait in JZ_BUSY_WAITS:
        if status != 0:
            break
        jz_log(f"[JZ] {url}: still too busy -> waiting {wait}s and trying again")
        time.sleep(wait)
        status, html = br.br_get(url)
    if status != 200:
        return status, [], []
    rows = br.br_parse(url, html)
    for r in rows:
        r["brand"] = r["brand"] or JZ_BRAND
    return status, [r for r in rows if jz_keep(r["name"], r["product_type"])], \
        [r["name"] for r in rows if not jz_keep(r["name"], r["product_type"])]


# ===== SECTION 5b: find the shop's API (live test 2026-11-03: the site is an ANGULAR app -> pages have NO product
# data in the HTML (0 JSON-LD, no AED), the browser gets products from an API. The API address is inside the app's
# main-*.js file -> /aljazeera/test lists the web addresses + api / product paths found there (send to Claude).
JZ_JS_SKIP = ("w3.org", "angular.io", "github.com", "google", "facebook", "twitter", "instagram", "youtube",
              "apple.com", "mozilla", "jquery", "cloudflare", "gstatic", "schema.org", "tiktok", "snapchat")


def jz_discover(html):
    """Home page HTML -> {scripts, urls, api_paths} found in the app's main js (nothing is saved)."""
    from urllib.parse import urljoin
    base = (re.search(r'<base\s+href="([^"]*)"', html or "") or [None, "/"])[1]
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', html or "")
    mains = [u for u in scripts if "main" in u.lower()][:1] or scripts[-1:]
    out = {"base_href": base, "main_js": [], "urls": [], "api_paths": [], "env_snippets": []}
    for src in mains:
        for root in (JZ_BASE + base, JZ_BASE + "/"):                    # js is relative to <base href>
            url = urljoin(root if root.endswith("/") else root + "/", src)
            st, js = br.br_get(url, "*/*")
            out["main_js"].append({"url": url, "status": st, "size": len(js or "")})
            if st == 200 and js:
                break
        if not js:
            continue
        urls = sorted({u.rstrip("/\\") for u in re.findall(r"https?://[A-Za-z0-9._\-]+(?:/[A-Za-z0-9._\-/]*)?", js)
                       if not any(w in u for w in JZ_JS_SKIP)})
        out["urls"] = urls[:60]
        paths = sorted({m for m in re.findall(r"[\"'`]([A-Za-z0-9_\-/{}.$]*(?:api|Api|API|product|Product)[A-Za-z0-9_\-/{}.$?=&]*)[\"'`]", js)
                        if 3 < len(m) < 120})
        out["api_paths"] = paths[:120]
        out["env_snippets"] = [js[max(0, m.start() - 120):m.start() + 200]
                               for m in re.finditer(r"(?i)(apiurl|baseurl|api_url|base_url|apibase|serverurl)", js)][:6]
    jz_log(f"[JZ-DISCOVER] base {base}, main js {out['main_js']}, {len(out['urls'])} urls, {len(out['api_paths'])} api paths")
    out["_js"] = js if mains else ""                   # kept for jz_api_probe (removed before showing)
    return out


# ===== SECTION 5c: try the El-Dokan API (live test 2026-11-03: Al Jazeera = EL-DOKAN shop platform,
# apiEndPoint https://api-uae.aljazeeraperfumes.com, product calls ${baseUrl}/products/... with
# baseUrl = ${apiEndPoint}/api/customer). Docs are not public -> /aljazeera/test calls the likely addresses and shows
# what comes back + the app code around the product calls (headers, paths) -> send to Claude to write the reader.
JZ_API = "https://api-uae.aljazeeraperfumes.com"
JZ_API_HEADERS = {**JZ_HEADERS, "Accept": "application/json", "Accept-Language": "en", "lang": "en",
                  "Origin": JZ_BASE, "Referer": JZ_BASE + "/en"}
JZ_API_TRY = ["/api/customer/products?page=1", "/api/customer/products/search?page=1",
              "/api/customer/products/search?page=1&keyword=oud", "/api/customer/products/163?variant=164",
              "/api/customer/products/163", "/api/customer/products/details/163?variant=164",
              "/api/customer/categories", "/api/customer/home", "/api/customer/view/page"]
JZ_JS_WORDS = ["/products/search", "/products/`", "/products/${", "baseUrl=`${", "setHeaders", "headers.set(",
               "paginationApi", "apiEndPoint:", "product-details"]


def jz_api_get(path, method="GET", body=None):
    """One API call -> (status, text). Nothing saved."""
    try:
        r = requests.request(method, JZ_API + path, headers=JZ_API_HEADERS, json=body, timeout=40)
        return r.status_code, r.text
    except requests.RequestException as ex:
        return 0, f"network error {str(ex)[:120]}"


def jz_api_probe(js):
    """Try JZ_API_TRY (GET, and POST for search) + show the app code around the product calls."""
    tries = []
    for path in JZ_API_TRY:
        st, txt = jz_api_get(path)
        tries.append({"get": path, "status": st, "size": len(txt or ""), "start": (txt or "")[:700]})
        jz_log(f"[JZ-API] GET {path} -> HTTP {st}, {len(txt or '')} chars")
    st, txt = jz_api_get("/api/customer/products/search", "POST", {"page": 1, "keyword": ""})
    tries.append({"post": "/api/customer/products/search", "status": st, "size": len(txt or ""), "start": (txt or "")[:700]})
    code = {}
    for w in JZ_JS_WORDS:
        hits = [m.start() for m in re.finditer(re.escape(w), js or "")][:3]
        code[w] = [js[max(0, i - 250):i + 350] for i in hits]
    return {"api": JZ_API, "tries": tries, "app_code": code}


# ===== SECTION 5d: THE READER - El-Dokan API product list (live test 2026-11-03)
#   POST {JZ_API}/api/customer/products/search  body {"page": n, ...}  -> {"code":200,"data":{"products":[{id, parent_id,
#        sku "Main_11050000", name "ONYX", name_en, description "ONYX 100 ML", image ...}, ...]}}
#   (GET /api/customer/products?page= = 404; GET .../products/search needs "q"; GET .../products/<id> = one product)
#   Product link = the app's own rule (goToProductDetails): /en/product-details/<name, spaces -> '-'>/<parent_id or id>?variant=<id>
# Field names for price / stock were not visible in the test (answer cut short) -> the usual El-Dokan / Laravel names
# are tried in this order; /aljazeera/test shows the real keys ("api_shape") to check them.
JZ_SEARCH = "/api/customer/products/search"
JZ_PRICE_KEYS = ["price_after_discount", "discount_price", "sale_price", "special_price", "final_price",
                 "price_after_sale", "offer_price", "price"]                      # first one > 0 = price shown (AED)
JZ_STOCK_KEYS = ["in_stock", "is_in_stock", "available", "is_available", "availability",
                 "stock", "quantity", "stock_quantity", "qty", "available_quantity"]
JZ_VARIANT_KEYS = ["variants", "variations", "children", "child_products", "options_products", "sub_products"]
JZ_API_PAUSE = 1.0                     # seconds between 2 list pages
JZ_API_MAX_PAGES = 200


def jz_api_page(page):
    """One page of the product list -> (products list, raw json). [] = end; None = failed."""
    for attempt, wait in enumerate([0, 20, 60]):
        if wait:
            time.sleep(wait)
        try:
            r = requests.post(JZ_API + JZ_SEARCH, params={"page": page}, headers=JZ_API_HEADERS,
                              json={"page": page, "q": "", "keyword": ""}, timeout=60)
        except requests.RequestException as ex:
            jz_log(f"[JZ-API] page {page}: network error {str(ex)[:80]} (try {attempt + 1})")
            continue
        if r.status_code == 429 or r.status_code >= 500:
            jz_log(f"[JZ-API] page {page}: HTTP {r.status_code} busy (try {attempt + 1})")
            continue
        try:
            js = r.json()
        except ValueError:
            jz_log(f"[JZ-API] page {page}: HTTP {r.status_code}, not JSON")
            return None, None
        data = js.get("data") if isinstance(js, dict) else None
        prods = data.get("products") if isinstance(data, dict) else None
        if isinstance(prods, dict):                       # Laravel paginator {"data":[...], "last_page":..}
            prods = prods.get("data")
        return (prods if isinstance(prods, list) else []), js
    return None, None


def jz_first(d, keys):
    """First key of d that has a value (not None / '')."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return k, d.get(k)
    return None, None


def jz_price(d):
    """AED price: first price key > 0."""
    for k in JZ_PRICE_KEYS:
        v = br.br_num((d or {}).get(k))
        if v and v > 0:
            return v
    return None


def jz_in_stock(d):
    """True / False from the first stock key; None = shop did not say."""
    k, v = jz_first(d, JZ_STOCK_KEYS)
    if k is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v > 0
    t = str(v).strip().lower()
    return t in ("1", "true", "yes", "in stock", "instock", "available") or (t.isdigit() and int(t) > 0)


def jz_category(p):
    """Category names of a product (for jz_keep)."""
    out = []
    for k in ("category", "categories", "category_name", "sub_category", "type", "product_type"):
        v = (p or {}).get(k)
        for x in (v if isinstance(v, list) else [v]):
            if isinstance(x, dict):
                x = x.get("name_en") or x.get("name")
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
    return " / ".join(out) or None


def jz_api_rows(p, keep_all=False):
    """One API product -> rows (one per variant that has a price), perfumes + gift sets only -> (rows, skipped names)."""
    name = (p.get("name_en") or p.get("name") or "").strip()
    desc = (p.get("description_en") or p.get("description") or "").strip()     # "ONYX 100 ML"
    cat = jz_category(p)
    if not keep_all and not jz_keep(f"{name} {desc}", cat):        # keep_all: "Add new link" (new_link.py)
        return [], [name]
    variants = []
    for k in JZ_VARIANT_KEYS:                                                   # sizes as own variants
        v = p.get(k)
        if isinstance(v, list):
            variants += [x for x in v if isinstance(x, dict) and x.get("id")]
    rows = []
    for v in (variants or [p]):
        price = jz_price(v) or jz_price(p)
        if not price:
            continue                                                            # no price / AED 0 -> skipped
        vname = (v.get("name_en") or v.get("name") or "").strip() if v is not p else ""
        full = name if not vname or vname.lower() == name.lower() else (vname if name.lower() in vname.lower() else f"{name} - {vname}")
        ins = jz_in_stock(v)
        ins = jz_in_stock(p) if ins is None else ins
        pid, vid = p.get("parent_id") or p.get("id"), v.get("id")
        slug = re.sub(r"\s", "-", name)                                       # app: name.replace(/\s/g, "-")
        sku = (v.get("sku") or p.get("sku") or "").strip() or None
        bc = str(v.get("barcode") or p.get("barcode") or "").strip()
        rows.append({"variant_id": int(vid), "name": full, "brand": JZ_BRAND, "product_type": cat,
                     "url": f"{JZ_BASE}/en/product-details/{slug}/{pid}?variant={vid}",
                     "gtin": bc if bc.isdigit() and 12 <= len(bc) <= 14 else None, "sku": sku,
                     "price_inc_tax": price, "price": round(price / 1.05, 2),
                     "stock": "In stock" if ins is not False else "Out of stock",   # shop silent -> listed = in stock
                     "volume": br.br_volume(vname, desc, name, v.get("description_en"), v.get("description"))})
    return rows, []


def jz_api_shape(js):
    """Keys of the answer / first product / its variant (shown on /aljazeera/test to check JZ_PRICE_KEYS etc.)."""
    data = (js or {}).get("data") or {}
    prods = data.get("products") if isinstance(data, dict) else None
    prods = prods.get("data") if isinstance(prods, dict) else prods
    p = prods[0] if prods else {}
    var = next((p[k][0] for k in JZ_VARIANT_KEYS if isinstance(p.get(k), list) and p[k]), None)
    small = lambda d: {k: (v if not isinstance(v, (list, dict)) else f"<{type(v).__name__} {len(v)}>")
                       for k, v in (d or {}).items() if k not in ("long_description", "long_description_ar")}
    return {"answer_keys": list((js or {}).keys()), "data_keys": list(data.keys()) if isinstance(data, dict) else None,
            "paging": {k: v for k, v in (data.items() if isinstance(data, dict) else []) if k != "products"},
            "product_keys": small(p), "variant_keys": small(var) if var else None}


def jz_api_all(on_rows):
    """Whole list page by page -> on_rows(rows, skipped) per page. Returns (pages, products, incomplete)."""
    seen, page, total = set(), 1, 0
    while page <= JZ_API_MAX_PAGES:
        prods, _ = jz_api_page(page)
        if prods is None:
            return page - 1, total, True
        new = [p for p in prods if isinstance(p, dict) and p.get("id") not in seen]
        if not new:                                     # end (or the API ignores 'page' -> same list again)
            break
        seen.update(p.get("id") for p in new)
        total += len(new)
        rows, skipped = [], []
        for p in new:
            r, sk = jz_api_rows(p)
            rows += r
            skipped += sk
        on_rows(rows, skipped)
        jz_log(f"[JZ-API] page {page}: {len(prods)} products ({len(new)} new), {len(rows)} rows kept, {len(skipped)} not perfume")
        page += 1
        time.sleep(JZ_API_PAUSE)
    return page - 1, total, False


# ===== SECTION 6: full download
def jz_save(cur, conn, rows):
    execute_values(cur, JZ_UPSERT, [(r["variant_id"], r["url"], r["gtin"], r["sku"], r["brand"], r["name"],
                                     r["product_type"], r["price_inc_tax"], r["price"], r["stock"], r["volume"])
                                    for r in rows if r["variant_id"]])
    conn.commit()


def jz_run():
    started = time.time()
    st = {"status": "running", "mode": None, "saved": 0, "skipped_not_perfume": 0, "in_stock": 0, "out_of_stock": 0,
          "incomplete": False}
    jz_state["last"] = st
    conn = jz_conn()
    cur = conn.cursor()
    try:
        cur.execute(JZ_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()

        def count(rows):
            st["saved"] += len(rows)
            st["in_stock"] += sum(r["stock"] == "In stock" for r in rows)
            st["out_of_stock"] += sum(r["stock"] != "In stock" for r in rows)

        first, _ = jz_api_page(1)
        if first:                                           # way 0: El-Dokan API (SECTION 5d) = Al Jazeera
            st["mode"] = "api"
            seen_ids = set()

            def on_rows(rows, skipped):
                rows = [r for r in rows if r["variant_id"] not in seen_ids]    # a variant twice -> saved once
                seen_ids.update(r["variant_id"] for r in rows)
                st["skipped_not_perfume"] += len(skipped)
                if rows:
                    jz_save(cur, conn, rows)
                count(rows)
            st["pages"], st["products"], st["incomplete"] = jz_api_all(on_rows)
            first = None
        elif jz_shopify_page(1):                            # way 1: Shopify
            first = jz_shopify_page(1)
            st["mode"] = "shopify"
            page, products = 1, first
            while products:
                rows, skipped = jz_shopify_rows(products)
                st["skipped_not_perfume"] += len(skipped)
                if rows:
                    jz_save(cur, conn, rows)
                count(rows)
                jz_log(f"[JZ] shopify page {page}: {len(products)} products, {len(rows)} saved (total {st['saved']})")
                if len(products) < 250 or page >= JZ_MAX_PAGES:
                    break
                page += 1
                time.sleep(1.5)
                products = jz_shopify_page(page)
                if products is None:
                    st["incomplete"] = True
                    break
        if st["mode"] is None:                              # way 2: sitemap + product pages
            st["mode"] = "sitemap"
            links, counts, _ = jz_product_links()
            st["links"], st["sitemaps"] = len(links), counts
            if not links:
                raise RuntimeError("no product list found (not Shopify, no product sitemap) - check /aljazeera/test")
            st.update({"pages_done": 0, "pages_without_data": 0, "pages_failed": 0})
            batch = []
            with ThreadPoolExecutor(max_workers=JZ_WORKERS) as pool:
                for status, kept, skipped in pool.map(jz_page, links):
                    st["pages_done"] += 1
                    if status not in (200, 404):
                        st["pages_failed"] += 1
                    elif status == 200 and not kept and not skipped:
                        st["pages_without_data"] += 1
                    st["skipped_not_perfume"] += len(skipped)
                    batch.extend(kept)
                    if len(batch) >= JZ_BATCH:
                        jz_save(cur, conn, batch)
                        count(batch)
                        batch = []
                    if st["pages_done"] % 200 == 0:
                        jz_log(f"[JZ] {st['pages_done']}/{len(links)} pages, {st['saved']} saved")
            if batch:
                jz_save(cur, conn, batch)
                count(batch)
            st["incomplete"] = st["pages_failed"] > len(links) * 0.05
        if not st["incomplete"] and st["saved"]:            # products gone from the site
            cur.execute("UPDATE aljazeera_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            st["marked_not_on_site"] = cur.rowcount
            conn.commit()
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        jz_log(f"[JZ-DONE] {st}")


def jz_worker():
    try:
        jz_run()
    except Exception as ex:
        jz_log(f"[JZ-ERROR] {ex}\n{traceback.format_exc()}")
        jz_state["last"] = {**(jz_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        jz_state["running"] = False
        jz_lock.release()


# ===== SECTION 7: routes
def jz_secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


@aljazeera_bp.route("/aljazeera/test")
def jz_test_route():
    """Which way works (Shopify / sitemap) + the first products read - nothing is saved."""
    if not jz_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    prods, js = jz_api_page(1)
    if prods:                                               # El-Dokan API (SECTION 5d)
        rows, skipped = [], []
        for p in prods:
            r, sk = jz_api_rows(p)
            rows += r
            skipped += sk
        result = {"ok": bool(rows), "mode": "api", "page1_products": len(prods), "page1_rows": len(rows),
                  "kept_without_price": sum(1 for p in prods if not jz_api_rows(p)[0] and not jz_api_rows(p)[1]),
                  "skipped_not_perfume": skipped[:30], "categories": sorted({r["product_type"] or "-" for r in rows})[:20],
                  "first_5": rows[:5], "api_shape": jz_api_shape(js)}
        p2, _ = jz_api_page(2)                              # does 'page' work?
        result["page2_new_products"] = len({x.get("id") for x in (p2 or [])} - {x.get("id") for x in prods})
        jz_log(f"[JZ-TEST] {json.dumps(result, default=str)[:2000]}")
        return jsonify(result)
    first = jz_shopify_page(1)
    if first:
        rows, skipped = jz_shopify_rows(first)
        result = {"ok": bool(rows), "mode": "shopify", "page1_products": len(first), "page1_rows": len(rows),
                  "skipped_not_perfume": skipped[:30], "product_types": sorted({r["product_type"] or "-" for r in rows})[:20],
                  "first_5": rows[:5]}
    else:
        links, counts, info = jz_product_links()
        result = {"ok": False, "mode": "sitemap", "shopify_products_json": "not working", "sitemap": info,
                  "sitemaps": counts, "product_links": len(links), "first_links": links[:3], "pages": []}
        st, html = br.br_get(JZ_BASE + "/en")                # home page: what kind of site + its API (SECTION 5b)
        result["home_en"] = {"status": st, **br.br_raw(JZ_BASE + "/en", html)}
        disc = jz_discover(html) if st == 200 else {"error": f"home page HTTP {st}"}
        result["api_probe"] = jz_api_probe(disc.pop("_js", ""))   # El-Dokan API answers (SECTION 5c)
        result["api_search"] = {k: v for k, v in disc.items() if k in ("main_js", "urls")}   # paths already known
        for url in links[:1]:                               # Angular: every page is the same empty shell
            st, html = br.br_get(url)
            rows = br.br_parse(url, html) if st == 200 else []
            page = {"url": url, "status": st,
                    "rows": [{**r, "brand": r["brand"] or JZ_BRAND, "kept": jz_keep(r["name"], r["product_type"])} for r in rows]}
            if not rows:
                page["raw"] = br.br_raw(url, html)
            result["pages"].append(page)
        result["ok"] = any(p["rows"] for p in result["pages"])
    jz_log(f"[JZ-TEST] {json.dumps(result, default=str)[:2000]}")
    return jsonify(result)


@aljazeera_bp.route("/aljazeera/run")
def jz_run_route():
    if not jz_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not jz_lock.acquire(blocking=False):
        return jsonify({"error": "Al Jazeera Perfumes download already running"}), 409
    jz_state["running"] = True
    jz_log("[JZ-RUN] Started")
    threading.Thread(target=jz_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/aljazeera/status"}), 202


@aljazeera_bp.route("/aljazeera/status")
def jz_status_route():
    return jsonify({"running": jz_state["running"], "last": jz_state["last"]})

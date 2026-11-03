"""
Ajmal catalog (en-ae.ajmal.com -> Postgres french_fragrance_db, table ajmal_catalog)
Prefix jm (am = Amberiya, aj = Al Injaz). Menu "Brands Store" (the brand's own shop; COPY of aljazeera.py), works like a competitor
(tab, matching, least price, Run 2).
PERFUMES + GIFT SETS only (user's choice, same as the other Brands Store shops): oils, bakhoor, incense, oud wood,
burners, home / body / hair items are left out (jm_keep).

The shop system could NOT be checked from Claude's side (network blocked, 2026-11-03), so the reader tries 3 ways (mode):
  0. "graphql":  Magento 2 GraphQL product list (SECTION 5c) - Ajmal's en-ae / ar-ae store addresses look like Magento
  1. "shopify":  /products.json works -> whole shop list (like Khaltat / Anfasic), filtered by jm_keep
  2. "sitemap":  else /sitemap.xml (none -> the "Sitemap:" lines of robots.txt) -> sitemaps with "product" in the
                 name: ALL their links; other (non-blog) sitemaps: only product-looking links (Salla ".../p123456",
                 "/product/", Magento ".html"), else their English links -> Arabic "/ar/" links switched to
                 English JM_LANG_PATH "/en/", every link moved to JM_BASE (UAE site, AED)
                 -> every product page read from its schema.org JSON-LD (same reader as brandsperfume.py: br_parse)
  /ajmal/test shows which way works + the first products (+ graphql answer / raw page data when nothing could be read)

Routes (open in the browser):
  GET /ajmal/test?secret=XXX    which way works + first products (run this first)
  GET /ajmal/run?secret=XXX     download everything into ajmal_catalog (background)
  GET /ajmal/status             progress

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

ajmal_bp = Blueprint("ajmal", __name__)


# ===== SECTION 1: settings
JM_BASE = "https://en-ae.ajmal.com"
JM_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
              "Accept": "application/json"}
JM_BRAND = "Ajmal"        # brand when the shop does not say one (it is their own shop)
JM_LANG_PATH = "/en/"                      # language part of links (Ajmal: the language is in the host en-ae.)
JM_HOME = JM_BASE + "/"                    # home page (test: what kind of site)
JM_SITEMAP_WORD = "product"                # product sitemaps in /sitemap.xml
JM_WORKERS = 1                             # ONE page at a time (Arabian Oud answered 429 "too many requests" with 4;
                                           # same safe speed here until we know this site)
JM_PAGE_PAUSE = 1.5                        # seconds between 2 product pages (be polite -> no 429)
JM_BUSY_WAITS = [60, 120]                  # still 429 after br_get's own retries -> wait this long, try again
JM_BATCH = 100                             # rows saved per database write
JM_MAX_PAGES = 100                         # Shopify: max 100 pages x 250
# PERFUMES + GIFT SETS (jm_keep, in this order). WHOLE words, any capitals; "x*" = word start.
JM_ALWAYS_SKIP = ["gift card", "sample", "samples", "discovery", "giveaway*"]
JM_GIFT_WORDS = ["gift set", "gift sets", "giftset", "gift box", "coffret", "set", "collection"]   # -> always kept
JM_SKIP_WORDS = ["oil", "oils", "dehn", "dahn", "bakhoor", "bukhoor", "bakhour", "incense", "dokhoon", "dakhoon",
                 "mabkhara", "mubakhar", "burner", "wood", "chips", "muattar", "candle", "diffuser", "reed",
                 "room spray", "home spray", "car", "body", "lotion", "cream", "shower", "soap", "hair", "deodorant",
                 "accessories", "bag", "pouch", "box only",
                 "gm", "gms", "gram", "grams", "kg"]   # sold by WEIGHT = oud wood / bakhoor ("Oud 350 - 30 gm"), not perfume
JM_SKIP_WORDS += ["home fragrance*", "air freshener*", "freshener*", "fabric spray", "linen spray",
                  "aoud & oils", "dehn al oud", "concentrated perfume oil*", "attar*", "hand cream", "shampoo"]
                  # not perfumes (Ajmal also sells oils / dehn al oud / bakhoor / body care)
JM_PERFUME_WORDS = ["perfume*", "parfum*", "eau", "edp", "edt", "cologne", "fragrance*", "spray", "extrait"]

jm_lock = threading.Lock()
jm_state = {"running": False, "last": None}


def jm_log(*args):
    print(*args, flush=True)


# ===== SECTION 2: database
JM_CREATE = """
CREATE TABLE IF NOT EXISTS ajmal_catalog (
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

JM_UPSERT = """
INSERT INTO ajmal_catalog (variant_id, product_url, gtin, sku, brand, name, product_type,
                                price_inc_tax, price, stock, volume)
VALUES %s
ON CONFLICT (variant_id) DO UPDATE SET
    product_url = EXCLUDED.product_url, gtin = EXCLUDED.gtin, sku = EXCLUDED.sku, brand = EXCLUDED.brand,
    name = EXCLUDED.name, product_type = EXCLUDED.product_type, price_inc_tax = EXCLUDED.price_inc_tax,
    price = EXCLUDED.price, stock = EXCLUDED.stock, volume = EXCLUDED.volume, updated_at = NOW();
"""


def jm_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    jm_log("[JM-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ===== SECTION 3: filter (perfumes + gift sets)
def jm_keep(name, category):
    """Order: JM_ALWAYS_SKIP -> out | gift set word -> kept | JM_SKIP_WORDS -> out | category given -> must look like
    perfume / gift | else kept (perfume)."""
    text = f"{name or ''} {category or ''}"
    if br.br_has(JM_ALWAYS_SKIP, text):
        return False
    if br.br_has(JM_GIFT_WORDS, text):
        return True
    if br.br_has(JM_SKIP_WORDS, text):
        return False
    if category:
        return br.br_has(JM_PERFUME_WORDS + ["gift*", "oud"], category) or br.br_has(JM_PERFUME_WORDS, name)
    return True


# ===== SECTION 4: way 1 - Shopify products.json
def jm_shopify_page(page):
    """One page of /products.json -> list of products, [] at the end / not Shopify, None when it failed."""
    try:
        r = requests.get(f"{JM_BASE}/products.json", params={"limit": 250, "page": page}, headers=JM_HEADERS, timeout=60)
    except requests.RequestException as ex:
        jm_log(f"[JM] products.json page {page}: network error {str(ex)[:80]}")
        return None
    if r.status_code != 200:
        return []
    try:
        return r.json().get("products", [])
    except ValueError:
        return []


def jm_shopify_rows(products):
    """Shopify products -> rows (one per variant), perfumes + gift sets only."""
    rows, skipped = [], []
    for p in products:
        title, ptype = (p.get("title") or "").strip(), (p.get("product_type") or "").strip()
        if not jm_keep(title, ptype):
            skipped.append(title)
            continue
        variants = p.get("variants") or []
        for v in variants:
            vt = (v.get("title") or "").strip()
            name = title if vt.lower() in ("", "default title") else f"{title} - {vt}"
            url = f"{JM_BASE}/products/{p.get('handle')}" + (f"?variant={v.get('id')}" if len(variants) > 1 else "")
            price = br.br_num(v.get("price"))
            if price is not None and price <= 0:
                continue                                    # AED 0 = free gift copy
            sku = (v.get("sku") or "").strip() or None
            d = re.sub(r"\D", "", sku or "")
            rows.append({"variant_id": v.get("id"), "url": url, "gtin": d if sku and d == sku and 12 <= len(d) <= 14 else None,
                         "sku": sku, "brand": (p.get("vendor") or "").strip() or JM_BRAND, "name": name,
                         "product_type": ptype or None, "price_inc_tax": price,
                         "price": round(price / 1.05, 2) if price else None,
                         "stock": "In stock" if v.get("available") else "Out of stock",
                         "volume": br.br_volume(vt, title, *(p.get("tags") or []))})
    return rows, skipped


# ===== SECTION 5: way 2 - sitemap + product pages (JSON-LD, brandsperfume.py reader)
JM_NOT_PRODUCT = (r"/page-\d+|/blog|/brands?/|/c\d+/?$|/(cart|login|account|search|customer|checkout|"
                  r"collections?|categor(y|ies)|pages?|about|contact|faq|privacy|terms|stores?)(\b|/)")   # English links that are NOT products
JM_PRODUCT_LINK = r"/product-details/|/p/?\d{3,}(?:[/?#]|$)|/products?/|\.html?(?:[?#]|$)"   # Salla p123456 / Shopify-Woo /product(s)/
                                                                           # / Magento ".html" product pages
# A sitemap may list the MAIN store's links (other host): the same products exist on the UAE site -> every link is
# moved to OUR host (JM_BASE = en-ae.ajmal.com) -> AED prices


def jm_product_links():
    """Product links from the sitemaps -> (links, {sitemap: counts}, info).
    Salla: /sitemap.xml lists ar/sitemap-1.xml, -2 ... (no "product" in the name) -> every non-blog sitemap is read,
    product links = JM_PRODUCT_LINK, Arabic links switched to English ("/ar/" -> JM_LANG_PATH; Salla finds the page
    by its p-number)."""
    status, index = br.br_get(JM_BASE + "/sitemap.xml", "*/*")
    locs = br.br_locs(index)
    if not locs:                                        # no /sitemap.xml -> the sitemaps named in robots.txt
        rst, robots = br.br_get(JM_BASE + "/robots.txt", "*/*")
        maps_r = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots or "")
        jm_log(f"[JM] sitemap.xml HTTP {status}: empty -> robots.txt HTTP {rst}: {len(maps_r)} sitemap(s)")
        for sm in maps_r:
            st2, xml2 = br.br_get(sm, "*/*")
            locs.extend(br.br_locs(xml2))
    xmls = [u for u in locs if u.endswith(".xml")]
    maps = [u for u in xmls if JM_SITEMAP_WORD in u.lower()] or [u for u in xmls if "blog" not in u.lower()]
    counts, links = {}, []
    if not xmls:                                        # sitemap.xml itself lists the pages
        links = [u for u in locs if re.search(JM_PRODUCT_LINK, u) and not re.search(JM_NOT_PRODUCT, u)]
        counts[JM_BASE + "/sitemap.xml"] = {"status": status, "links": len(locs), "product_links": len(links)}
    for sm in maps:
        st, xml = br.br_get(sm, "*/*")
        found = br.br_locs(xml)
        prods = (found if JM_SITEMAP_WORD in sm.lower()                    # a "products" sitemap: every link
                 else [u for u in found if re.search(JM_PRODUCT_LINK, u) and not re.search(JM_NOT_PRODUCT, u)])
        if not prods:
            # product links with no product mark (like Arabian Oud: .../en/0201020036-oud-350-30-gm) ->
            # take the ENGLISH links (no language in the links -> all of them), minus info pages and blog;
            # pages without product data are skipped later by the JSON-LD reader
            eng = [u for u in found if JM_LANG_PATH in u] or [u for u in found if "/ar/" not in u]
            prods = [u for u in eng if not re.search(JM_NOT_PRODUCT, u)]
        counts[sm] = {"status": st, "links": len(found), "product_links": len(prods),
                      "sample": found[:5]}                                   # shown on /ajmal/test
        links.extend(prods)
    base = urlsplit(JM_BASE)                                                 # https://en-ae.ajmal.com
    links = [urlunsplit(urlsplit(u)._replace(netloc=base.netloc, scheme=base.scheme)).replace("/ar/", JM_LANG_PATH)
             for u in links]                                                 # UAE site, English page
    pnum = lambda u: (re.search(r"/p/?(\d{3,})(?:[/?#]|$)", u) or [None, u])[1]   # same p-number twice -> once
    seen, out = set(), []
    for u in links:
        if pnum(u) not in seen:
            seen.add(pnum(u))
            out.append(u)
    jm_log(f"[JM] sitemap.xml HTTP {status}: {len(maps)} sitemap(s) read, {len(out)} product links")
    return out, counts, {"status": status, "index_sample": locs[:15]}


def jm_page(url):
    """One product page -> (status, rows kept, names skipped). Pauses JM_PAGE_PAUSE first; when the shop still
    says "too busy" (429 -> br_get gives up with status 0) it waits JM_BUSY_WAITS and tries again."""
    time.sleep(JM_PAGE_PAUSE)
    status, html = br.br_get(url)
    for wait in JM_BUSY_WAITS:
        if status != 0:
            break
        jm_log(f"[JM] {url}: still too busy -> waiting {wait}s and trying again")
        time.sleep(wait)
        status, html = br.br_get(url)
    if status != 200:
        return status, [], []
    rows = br.br_parse(url, html)
    for r in rows:
        r["brand"] = r["brand"] or JM_BRAND
    return status, [r for r in rows if jm_keep(r["name"], r["product_type"])], \
        [r["name"] for r in rows if not jm_keep(r["name"], r["product_type"])]


# ===== SECTION 5b: find the shop's API (from aljazeera.py: for a site whose pages have NO product
# data in the HTML (0 JSON-LD, no AED), the browser gets products from an API. The API address is inside the app's
# main-*.js file -> /ajmal/test lists the web addresses + api / product paths found there (send to Claude).
JM_JS_SKIP = ("w3.org", "angular.io", "github.com", "google", "facebook", "twitter", "instagram", "youtube",
              "apple.com", "mozilla", "jquery", "cloudflare", "gstatic", "schema.org", "tiktok", "snapchat")


def jm_discover(html):
    """Home page HTML -> {scripts, urls, api_paths} found in the app's main js (nothing is saved)."""
    from urllib.parse import urljoin
    base = (re.search(r'<base\s+href="([^"]*)"', html or "") or [None, "/"])[1]
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', html or "")
    mains = [u for u in scripts if "main" in u.lower()][:1] or scripts[-1:]
    out = {"base_href": base, "main_js": [], "urls": [], "api_paths": [], "env_snippets": []}
    for src in mains:
        for root in (JM_BASE + base, JM_BASE + "/"):                    # js is relative to <base href>
            url = urljoin(root if root.endswith("/") else root + "/", src)
            st, js = br.br_get(url, "*/*")
            out["main_js"].append({"url": url, "status": st, "size": len(js or "")})
            if st == 200 and js:
                break
        if not js:
            continue
        urls = sorted({u.rstrip("/\\") for u in re.findall(r"https?://[A-Za-z0-9._\-]+(?:/[A-Za-z0-9._\-/]*)?", js)
                       if not any(w in u for w in JM_JS_SKIP)})
        out["urls"] = urls[:60]
        paths = sorted({m for m in re.findall(r"[\"'`]([A-Za-z0-9_\-/{}.$]*(?:api|Api|API|product|Product)[A-Za-z0-9_\-/{}.$?=&]*)[\"'`]", js)
                        if 3 < len(m) < 120})
        out["api_paths"] = paths[:120]
        out["env_snippets"] = [js[max(0, m.start() - 120):m.start() + 200]
                               for m in re.finditer(r"(?i)(apiurl|baseurl|api_url|base_url|apibase|serverurl)", js)][:6]
    jm_log(f"[JM-DISCOVER] base {base}, main js {out['main_js']}, {len(out['urls'])} urls, {len(out['api_paths'])} api paths")
    out["_js"] = js if mains else ""                   # kept for jm_api_probe (removed before showing)
    return out


# ===== SECTION 5c: THE READER - Magento GraphQL (tried FIRST, mode "graphql")
# Ajmal's shop (en-ae / ar-ae ... store addresses) looks like MAGENTO 2 -> its public product API is
#   POST {JM_BASE}/graphql  query products(filter / search, pageSize, currentPage) -> items: sku, name, url_key,
#   url_suffix, stock_status, categories, price_range.minimum_price.final_price (AED), sizes = ConfigurableProduct variants
# NOT checked from Claude's side (network blocked 2026-11-03) -> /ajmal/test shows what the shop answered
# (graphql_answer); not Magento -> Shopify / sitemap ways below + api_probe.
import zlib

JM_GRAPHQL = JM_BASE + "/graphql"
JM_GQL_HEADERS = {**JM_HEADERS, "Content-Type": "application/json", "Accept": "application/json"}
JM_PAGE_SIZE = 50
JM_API_PAUSE = 1.0                     # seconds between 2 list pages
JM_API_MAX_PAGES = 200
JM_PRICE = "price_range{minimum_price{final_price{value currency}}}"
# live test 2026-11-03 (488 products, 10 pages, list via price filter): first products were AGARWOOD (oud wood chips)
# sold by weight ("Agarwood Marourqi - 50GM", category "King of Oud / Agarwood"). Perfumes can ALSO be in the
# Agarwood category -> checked on the NAME (JM_NAME_SKIP) and the size label (JM_WEIGHT_WORDS), not the category.
JM_NAME_SKIP = ["agarwood*", "bakhoor", "bukhoor", "dahn", "dehn", "oud chips", "mamoul", "muattar"]
JM_WEIGHT_WORDS = ["gm", "gms", "gram", "grams", "kg", "g", "tola", "tolas"]
# "50GM" / "100 g" / "1kg" / "1 Tola" (number + unit, also written together) = sold by weight (live test 2026-11-03:
# 'Ramadan online Exclusive Cambodi - 50GM', 'RAW - Flakes - 50GM' got through the word check)
JM_WEIGHT_RE = r"\d\s*(g|gm|gms|gram|grams|kg|kgs|tola|tolas)\b"


def jm_by_weight(text):
    """Size / name sold by weight?"""
    return bool(re.search(JM_WEIGHT_RE, text or "", re.I)) or br.br_has(JM_WEIGHT_WORDS, text)
JM_ITEM = ("__typename sku name url_key url_suffix stock_status categories{name} " + JM_PRICE +
           " ... on ConfigurableProduct{variants{attributes{label code} product{sku name stock_status " + JM_PRICE + "}}}")
# Magento needs a filter or a search word to list products; tried in this order, the first that answers is used
JM_LISTS = ['filter:{price:{from:"0"}}', 'filter:{category_id:{from:"0"}}', 'search:"perfume"', 'search:"a"']
jm_list_used = {"arg": None}           # which JM_LISTS entry worked (shown in test / status)


def jm_gql(arg, page):
    """One GraphQL page -> (status, json or None, error text)."""
    q = ("query{products(" + arg + f", pageSize:{JM_PAGE_SIZE}, currentPage:{page})" +
         "{total_count page_info{total_pages current_page} items{" + JM_ITEM + "}}}")
    try:
        r = requests.post(JM_GRAPHQL, headers=JM_GQL_HEADERS, json={"query": q}, timeout=60)
    except requests.RequestException as ex:
        return 0, None, f"network error {str(ex)[:100]}"
    try:
        js = r.json()
    except ValueError:
        return r.status_code, None, f"HTTP {r.status_code}, not JSON: {r.text[:200]}"
    errs = "; ".join(e.get("message", "") for e in (js.get("errors") or []) if isinstance(e, dict))
    return r.status_code, js, errs


def jm_api_page(page):
    """One page of the product list -> (items list, raw json). [] = end; None = failed / not Magento."""
    args = [jm_list_used["arg"]] if jm_list_used["arg"] else JM_LISTS
    for arg in args:
        for attempt, wait in enumerate([0, 20, 60]):
            if wait:
                time.sleep(wait)
            st, js, err = jm_gql(arg, page)
            if st in (429, 502, 503, 504) or st == 0:
                jm_log(f"[JM-GQL] page {page}: HTTP {st} busy (try {attempt + 1})")
                continue
            break
        items = ((((js or {}).get("data") or {}).get("products") or {}).get("items"))
        if isinstance(items, list) and (items or jm_list_used["arg"]):
            if not jm_list_used["arg"]:
                jm_list_used["arg"] = arg
                jm_log(f"[JM-GQL] product list works with {arg}")
            return items, js
        jm_log(f"[JM-GQL] {arg} page {page}: HTTP {st} {err[:150]}")
    return None, None


def jm_vid(sku, url):
    """Number for variant_id (Magento sku is text)."""
    return zlib.crc32(f"{sku}|{url}".encode()) + 10 ** 12


def jm_final(d):
    """AED price shown (final price = after special price)."""
    try:
        return br.br_num(d["price_range"]["minimum_price"]["final_price"]["value"])
    except (KeyError, TypeError):
        return None


def jm_api_rows(p, keep_all=False):
    """One Magento product -> rows (one per size of a configurable product), perfumes + gift sets only."""
    name = (p.get("name") or "").strip()
    cat = " / ".join(c.get("name") for c in (p.get("categories") or []) if isinstance(c, dict) and c.get("name")) or None
    if not keep_all and (br.br_has(JM_NAME_SKIP, name) or not jm_keep(name, cat)):
        return [], [name]
    url = f"{JM_BASE}/{p.get('url_key')}{p.get('url_suffix') if p.get('url_suffix') is not None else '.html'}"
    sizes = [v for v in (p.get("variants") or []) if isinstance(v, dict) and isinstance(v.get("product"), dict)]
    rows = []
    for v in (sizes or [None]):
        prod = v["product"] if v else p
        label = " ".join(a.get("label") or "" for a in (v.get("attributes") or [])).strip() if v else ""
        if not keep_all and jm_by_weight(label):
            continue                                                  # size sold by WEIGHT (50GM) = oud wood, not perfume
        price = jm_final(prod) or jm_final(p)
        if not price:
            continue                                                  # no price / AED 0 -> skipped
        full = name if not label or label.lower() in name.lower() else f"{name} - {label}"
        # one Magento page for all sizes -> each size gets its own link (our tables key products by link; the shop
        # ignores ?size=, the page opens normally)
        link = f"{url}?size={re.sub(r'[^0-9A-Za-z.]+', '', label)}" if v and len(sizes) > 1 and label else url
        sku = (prod.get("sku") or p.get("sku") or "").strip() or None
        d = re.sub(r"\D", "", sku or "")
        main = str(p.get("sku") or "").strip()                        # Ajmal: main sku = barcode (6293701007223)
        gtin = (d if sku and d == sku and 12 <= len(d) <= 14 else
                main if main.isdigit() and 12 <= len(main) <= 14 and len(sizes) <= 1 else None)
        rows.append({"variant_id": jm_vid(sku, link), "url": link, "name": full, "brand": JM_BRAND, "product_type": cat,
                     "gtin": gtin, "sku": sku,
                     "price_inc_tax": price, "price": round(price / 1.05, 2),
                     "stock": "In stock" if (prod.get("stock_status") or p.get("stock_status")) != "OUT_OF_STOCK" else "Out of stock",
                     "volume": br.br_volume(label, name, prod.get("name"))})
    if not keep_all and not rows and sizes and all(jm_by_weight(" ".join(a.get("label") or "" for a in (v.get("attributes") or [])))
                                                   for v in sizes):
        return [], [name]                                             # every size by weight -> listed as left out
    return rows, []


def jm_api_shape(js):
    """Paging + keys of the first product (shown on /ajmal/test)."""
    pr = (((js or {}).get("data") or {}).get("products") or {})
    it = (pr.get("items") or [{}])[0]
    return {"list_used": jm_list_used["arg"], "total_count": pr.get("total_count"), "page_info": pr.get("page_info"),
            "first_item": {k: (v if not isinstance(v, (list, dict)) else f"<{type(v).__name__} {len(v)}>") for k, v in it.items()}}


def jm_api_all(on_rows):
    """Whole list page by page -> on_rows(rows, skipped) per page. Returns (pages, products, incomplete)."""
    seen, page, total = set(), 1, 0
    while page <= JM_API_MAX_PAGES:
        items, _ = jm_api_page(page)
        if items is None:
            return page - 1, total, True
        new = [p for p in items if isinstance(p, dict) and p.get("sku") not in seen]
        if not new:                                     # end (Magento repeats the last page when asked past it)
            break
        seen.update(p.get("sku") for p in new)
        total += len(new)
        rows, skipped = [], []
        for p in new:
            r, sk = jm_api_rows(p)
            rows += r
            skipped += sk
        on_rows(rows, skipped)
        jm_log(f"[JM-GQL] page {page}: {len(items)} products ({len(new)} new), {len(rows)} rows kept, {len(skipped)} not perfume")
        page += 1
        time.sleep(JM_API_PAUSE)
    return page - 1, total, False


JM_JS_WORDS = ["graphql", "/rest/V1", "/rest/default", "storeCode", "products.json", "api/", "salla", "Shopify"]


def jm_api_probe(js):
    """Not Magento? -> the raw GraphQL answer + the site's script code around API words (send to Claude)."""
    st, ans, err = jm_gql(JM_LISTS[0], 1)
    code = {}
    for w in JM_JS_WORDS:
        hits = [m.start() for m in re.finditer(re.escape(w), js or "")][:2]
        code[w] = [js[max(0, i - 200):i + 300] for i in hits]
    return {"graphql": JM_GRAPHQL, "graphql_status": st, "graphql_error": err,
            "graphql_answer_start": json.dumps(ans)[:600] if ans else None, "app_code": code}


# ===== SECTION 6: full download
def jm_save(cur, conn, rows):
    execute_values(cur, JM_UPSERT, [(r["variant_id"], r["url"], r["gtin"], r["sku"], r["brand"], r["name"],
                                     r["product_type"], r["price_inc_tax"], r["price"], r["stock"], r["volume"])
                                    for r in rows if r["variant_id"]])
    conn.commit()


def jm_run():
    started = time.time()
    st = {"status": "running", "mode": None, "saved": 0, "skipped_not_perfume": 0, "in_stock": 0, "out_of_stock": 0,
          "incomplete": False}
    jm_state["last"] = st
    conn = jm_conn()
    cur = conn.cursor()
    try:
        cur.execute(JM_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()

        def count(rows):
            st["saved"] += len(rows)
            st["in_stock"] += sum(r["stock"] == "In stock" for r in rows)
            st["out_of_stock"] += sum(r["stock"] != "In stock" for r in rows)

        first, _ = jm_api_page(1)
        if first:                                           # way 0: Magento GraphQL (SECTION 5c)
            st["mode"] = "graphql"
            seen_ids = set()

            def on_rows(rows, skipped):
                rows = [r for r in rows if r["variant_id"] not in seen_ids]    # a variant twice -> saved once
                seen_ids.update(r["variant_id"] for r in rows)
                st["skipped_not_perfume"] += len(skipped)
                if rows:
                    jm_save(cur, conn, rows)
                count(rows)
            st["pages"], st["products"], st["incomplete"] = jm_api_all(on_rows)
            first = None
        elif jm_shopify_page(1):                            # way 1: Shopify
            first = jm_shopify_page(1)
            st["mode"] = "shopify"
            page, products = 1, first
            while products:
                rows, skipped = jm_shopify_rows(products)
                st["skipped_not_perfume"] += len(skipped)
                if rows:
                    jm_save(cur, conn, rows)
                count(rows)
                jm_log(f"[JM] shopify page {page}: {len(products)} products, {len(rows)} saved (total {st['saved']})")
                if len(products) < 250 or page >= JM_MAX_PAGES:
                    break
                page += 1
                time.sleep(1.5)
                products = jm_shopify_page(page)
                if products is None:
                    st["incomplete"] = True
                    break
        if st["mode"] is None:                              # way 2: sitemap + product pages
            st["mode"] = "sitemap"
            links, counts, _ = jm_product_links()
            st["links"], st["sitemaps"] = len(links), counts
            if not links:
                raise RuntimeError("no product list found (not Shopify, no product sitemap) - check /ajmal/test")
            st.update({"pages_done": 0, "pages_without_data": 0, "pages_failed": 0})
            batch = []
            with ThreadPoolExecutor(max_workers=JM_WORKERS) as pool:
                for status, kept, skipped in pool.map(jm_page, links):
                    st["pages_done"] += 1
                    if status not in (200, 404):
                        st["pages_failed"] += 1
                    elif status == 200 and not kept and not skipped:
                        st["pages_without_data"] += 1
                    st["skipped_not_perfume"] += len(skipped)
                    batch.extend(kept)
                    if len(batch) >= JM_BATCH:
                        jm_save(cur, conn, batch)
                        count(batch)
                        batch = []
                    if st["pages_done"] % 200 == 0:
                        jm_log(f"[JM] {st['pages_done']}/{len(links)} pages, {st['saved']} saved")
            if batch:
                jm_save(cur, conn, batch)
                count(batch)
            st["incomplete"] = st["pages_failed"] > len(links) * 0.05
        if not st["incomplete"] and st["saved"]:            # products gone from the site
            cur.execute("UPDATE ajmal_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            st["marked_not_on_site"] = cur.rowcount
            conn.commit()
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        jm_log(f"[JM-DONE] {st}")


def jm_worker():
    try:
        jm_run()
    except Exception as ex:
        jm_log(f"[JM-ERROR] {ex}\n{traceback.format_exc()}")
        jm_state["last"] = {**(jm_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        jm_state["running"] = False
        jm_lock.release()


# ===== SECTION 7: routes
def jm_secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


@ajmal_bp.route("/ajmal/test")
def jm_test_route():
    """Which way works (Shopify / sitemap) + the first products read - nothing is saved."""
    if not jm_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    prods, js = jm_api_page(1)
    if prods:                                               # Magento GraphQL (SECTION 5c)
        rows, skipped = [], []
        for p in prods:
            r, sk = jm_api_rows(p)
            rows += r
            skipped += sk
        result = {"ok": bool(rows), "mode": "graphql", "page1_products": len(prods), "page1_rows": len(rows),
                  "kept_without_price": sum(1 for p in prods if not jm_api_rows(p)[0] and not jm_api_rows(p)[1]),
                  "skipped_not_perfume": skipped[:30], "categories": sorted({r["product_type"] or "-" for r in rows})[:20], "brands": sorted({r["brand"] for r in rows}),
                  "first_5": rows[:5], "api_shape": jm_api_shape(js)}   # Magento list
        p2, _ = jm_api_page(2)                              # does 'page' work?
        result["page2_new_products"] = len({x.get("sku") for x in (p2 or [])} - {x.get("sku") for x in prods})
        jm_log(f"[JM-TEST] {json.dumps(result, default=str)[:2000]}")
        return jsonify(result)
    first = jm_shopify_page(1)
    if first:
        rows, skipped = jm_shopify_rows(first)
        result = {"ok": bool(rows), "mode": "shopify", "page1_products": len(first), "page1_rows": len(rows),
                  "skipped_not_perfume": skipped[:30], "product_types": sorted({r["product_type"] or "-" for r in rows})[:20],
                  "first_5": rows[:5]}
    else:
        links, counts, info = jm_product_links()
        result = {"ok": False, "mode": "sitemap", "shopify_products_json": "not working", "sitemap": info,
                  "sitemaps": counts, "product_links": len(links), "first_links": links[:3], "pages": []}
        st, html = br.br_get(JM_HOME)                       # home page: what kind of site + its API (SECTION 5b)
        result["home_en"] = {"status": st, **br.br_raw(JM_HOME, html)}
        disc = jm_discover(html) if st == 200 else {"error": f"home page HTTP {st}"}
        result["api_probe"] = jm_api_probe(disc.pop("_js", ""))   # GraphQL answer + script code (SECTION 5c)
        result["api_search"] = {k: v for k, v in disc.items() if k in ("main_js", "urls")}   # paths already known
        for url in links[:3]:
            st, html = br.br_get(url)
            rows = br.br_parse(url, html) if st == 200 else []
            page = {"url": url, "status": st,
                    "rows": [{**r, "brand": r["brand"] or JM_BRAND, "kept": jm_keep(r["name"], r["product_type"])} for r in rows]}
            if not rows:
                page["raw"] = br.br_raw(url, html)
            result["pages"].append(page)
        result["ok"] = any(p["rows"] for p in result["pages"])
    jm_log(f"[JM-TEST] {json.dumps(result, default=str)[:2000]}")
    return jsonify(result)


@ajmal_bp.route("/ajmal/run")
def jm_run_route():
    if not jm_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not jm_lock.acquire(blocking=False):
        return jsonify({"error": "Ajmal download already running"}), 409
    jm_state["running"] = True
    jm_log("[JM-RUN] Started")
    threading.Thread(target=jm_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/ajmal/status"}), 202


@ajmal_bp.route("/ajmal/status")
def jm_status_route():
    return jsonify({"running": jm_state["running"], "last": jm_state["last"]})


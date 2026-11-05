"""
Reef Perfumes catalog (ae.reefperfumes.com/en -> Postgres french_fragrance_db, table reefperfumes_catalog)
Prefix rf. Menu "Brands Store" (the brand's own shop), works like a competitor (tab, matching, least price, Run 2).
Added 2026-11-05 (user: "next shop to add in brand store Reefperfumes https://ae.reefperfumes.com/").
COPY of arabianoud.py (names RF_ / rf_). The site's links look like Salla ("/en/-/c585180698" = a category) ->
same reader: Shopify /products.json first, else /sitemap.xml -> product pages read from their JSON-LD (br_parse).
NOT checked live yet (Claude's network is blocked) -> run /reefperfumes/test FIRST and send the result to Claude.
PERFUMES + GIFT SETS only (user's choice, like the other Brands Store shops): site menu Perfumes / Reef Offers kept;
Body and hair perfumes, Reef Beauty, Home Fragrances, Digital gift cards left out (rf_keep word lists).
One page at a time + RF_PAGE_PAUSE (like Arabian Oud, polite -> no 429).

Routes (open in the browser):
  GET /reefperfumes/test?secret=XXX    which way works + first products (run this first)
  GET /reefperfumes/run?secret=XXX     download everything into reefperfumes_catalog (background)
  GET /reefperfumes/status             progress
  match to our products: /competitor/match-rf?secret=XXX

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

reefperfumes_bp = Blueprint("reefperfumes", __name__)


# ===== SECTION 1: settings
RF_BASE = "https://ae.reefperfumes.com"
RF_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
              "Accept": "application/json"}
RF_BRAND = "Reef Perfumes"        # brand when the shop does not say one (it is their own shop)
RF_LANG_PATH = "/en/"                      # English pages (the user's link: ae.reefperfumes.com/en)
RF_SITEMAP_WORD = "product"                # product sitemaps in /sitemap.xml
RF_WORKERS = 1                             # ONE page at a time: Reef Perfumes (Cloudflare) answers 429 "too many
                                           # requests" when 4 pages are asked at once (log 2026-11-02)
RF_PAGE_PAUSE = 1.5                        # seconds between 2 product pages (be polite -> no 429)
RF_BUSY_WAITS = [60, 120]                  # still 429 after br_get's own retries -> wait this long, try again
RF_BATCH = 100                             # rows saved per database write
RF_MAX_PAGES = 100                         # Shopify: max 100 pages x 250
# PERFUMES + GIFT SETS (rf_keep, in this order). WHOLE words, any capitals; "x*" = word start.
RF_ALWAYS_SKIP = ["gift card", "sample", "samples", "discovery", "giveaway*"]
RF_GIFT_WORDS = ["gift set", "gift sets", "giftset", "gift box", "coffret", "set", "collection", "bundle*"]   # -> always kept
RF_SKIP_WORDS = ["oil", "oils", "dehn", "dahn", "bakhoor", "bukhoor", "bakhour", "incense", "dokhoon", "dakhoon",
                 "mabkhara", "mubakhar", "burner", "wood", "chips", "muattar", "candle", "diffuser", "reed",
                 "room spray", "home spray", "car", "body", "lotion", "cream", "shower", "soap", "hair", "deodorant",
                 "accessories", "bag", "pouch", "box only",
                 "mist", "makeup", "make up", "lipstick", "lip", "mascara", "nail", "beauty", "skin", "hand",
                 "gift card", "digital",                      # Reef menu: Body and hair perfumes / Reef Beauty / gift cards
                 "gm", "gms", "gram", "grams", "kg"]   # sold by WEIGHT = oud wood / bakhoor ("Oud 350 - 30 gm"), not perfume
RF_PERFUME_WORDS = ["perfume*", "parfum*", "eau", "edp", "edt", "cologne", "fragrance*", "spray", "extrait"]

rf_lock = threading.Lock()
rf_state = {"running": False, "last": None}


def rf_log(*args):
    print(*args, flush=True)


# ===== SECTION 2: database
RF_CREATE = """
CREATE TABLE IF NOT EXISTS reefperfumes_catalog (
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

RF_UPSERT = """
INSERT INTO reefperfumes_catalog (variant_id, product_url, gtin, sku, brand, name, product_type,
                                price_inc_tax, price, stock, volume)
VALUES %s
ON CONFLICT (variant_id) DO UPDATE SET
    product_url = EXCLUDED.product_url, gtin = EXCLUDED.gtin, sku = EXCLUDED.sku, brand = EXCLUDED.brand,
    name = EXCLUDED.name, product_type = EXCLUDED.product_type, price_inc_tax = EXCLUDED.price_inc_tax,
    price = EXCLUDED.price, stock = EXCLUDED.stock, volume = EXCLUDED.volume, updated_at = NOW();
"""


def rf_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    rf_log("[RF-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ===== SECTION 3: filter (perfumes + gift sets)
def rf_keep(name, category):
    """Order: RF_ALWAYS_SKIP -> out | gift set word -> kept | RF_SKIP_WORDS -> out | category given -> must look like
    perfume / gift | else kept (perfume)."""
    text = f"{name or ''} {category or ''}"
    if br.br_has(RF_ALWAYS_SKIP, text):
        return False
    if br.br_has(RF_GIFT_WORDS, text):
        return True
    if br.br_has(RF_SKIP_WORDS, text):
        return False
    if category:
        return br.br_has(RF_PERFUME_WORDS + ["gift*", "oud"], category) or br.br_has(RF_PERFUME_WORDS, name)
    return True


# ===== SECTION 4: way 1 - Shopify products.json
def rf_shopify_page(page):
    """One page of /products.json -> list of products, [] at the end / not Shopify, None when it failed."""
    try:
        r = requests.get(f"{RF_BASE}/products.json", params={"limit": 250, "page": page}, headers=RF_HEADERS, timeout=60)
    except requests.RequestException as ex:
        rf_log(f"[RF] products.json page {page}: network error {str(ex)[:80]}")
        return None
    if r.status_code != 200:
        return []
    try:
        return r.json().get("products", [])
    except ValueError:
        return []


def rf_shopify_rows(products):
    """Shopify products -> rows (one per variant), perfumes + gift sets only."""
    rows, skipped = [], []
    for p in products:
        title, ptype = (p.get("title") or "").strip(), (p.get("product_type") or "").strip()
        if not rf_keep(title, ptype):
            skipped.append(title)
            continue
        variants = p.get("variants") or []
        for v in variants:
            vt = (v.get("title") or "").strip()
            name = title if vt.lower() in ("", "default title") else f"{title} - {vt}"
            url = f"{RF_BASE}/products/{p.get('handle')}" + (f"?variant={v.get('id')}" if len(variants) > 1 else "")
            price = br.br_num(v.get("price"))
            if price is not None and price <= 0:
                continue                                    # AED 0 = free gift copy
            sku = (v.get("sku") or "").strip() or None
            d = re.sub(r"\D", "", sku or "")
            rows.append({"variant_id": v.get("id"), "url": url, "gtin": d if sku and d == sku and 12 <= len(d) <= 14 else None,
                         "sku": sku, "brand": (p.get("vendor") or "").strip() or RF_BRAND, "name": name,
                         "product_type": ptype or None, "price_inc_tax": price,
                         "price": round(price / 1.05, 2) if price else None,
                         "stock": "In stock" if v.get("available") else "Out of stock",
                         "volume": br.br_volume(vt, title, *(p.get("tags") or []))})
    return rows, skipped


# ===== SECTION 5: way 2 - sitemap + product pages (JSON-LD, brandsperfume.py reader)
RF_NOT_PRODUCT = r"/page-\d+|/blog|/brands?/|/c\d+/?$|/(cart|login|account|search)\b"   # English links that are NOT products
RF_PRODUCT_LINK = r"/p/?\d{3,}(?:[/?#]|$)"  # Salla product page: .../<name>/p123456 (also /p/123456; anywhere in link)
# Salla shops can list the MAIN store links (reefperfumes.com/ar/...) in the sitemap: the same products exist on the
# UAE site -> every link is moved to OUR host (RF_BASE = ae.reefperfumes.com) -> AED prices (as for Arabian Oud)


def rf_product_links():
    """Product links from the sitemaps -> (links, {sitemap: counts}, info).
    Salla: /sitemap.xml lists ar/sitemap-1.xml, -2 ... (no "product" in the name) -> every non-blog sitemap is read,
    product links = RF_PRODUCT_LINK, Arabic links switched to English ("/ar/" -> RF_LANG_PATH; Salla finds the page
    by its p-number)."""
    status, index = br.br_get(RF_BASE + "/sitemap.xml", "*/*")
    locs = br.br_locs(index)
    xmls = [u for u in locs if u.endswith(".xml")]
    maps = [u for u in xmls if RF_SITEMAP_WORD in u.lower()] or [u for u in xmls if "blog" not in u.lower()]
    counts, links = {}, []
    if not xmls:                                        # sitemap.xml itself lists the pages
        links = [u for u in locs if re.search(RF_PRODUCT_LINK, u) or re.search(r"/products?/", u)]
        counts[RF_BASE + "/sitemap.xml"] = {"status": status, "links": len(locs), "product_links": len(links)}
    for sm in maps:
        st, xml = br.br_get(sm, "*/*")
        found = br.br_locs(xml)
        prods = (found if RF_SITEMAP_WORD in sm.lower()                    # a "products" sitemap: every link
                 else [u for u in found if re.search(RF_PRODUCT_LINK, u) or re.search(r"/products?/", u)])
        if not prods:
            # (Arabian Oud live test 2026-11-03: product links had NO p-number, e.g.
            # arabianoud.com/en/0201020036-oud-350-30-gm) ->
            # take the ENGLISH links, minus info pages ("/page-164033926") and blog; pages without product data
            # are skipped later by the JSON-LD reader
            prods = [u for u in found if RF_LANG_PATH in u and not re.search(RF_NOT_PRODUCT, u)]
        counts[sm] = {"status": st, "links": len(found), "product_links": len(prods),
                      "sample": found[:5]}                                   # shown on /reefperfumes/test
        links.extend(prods)
    # LIVE TEST 2026-11-05: sitemap-1 = info pages only (/en/p/about, /p/locations ... -> taken by the "English
    # links" fallback), sitemap-2 = 316 real products (/p2123895051), each in Arabic AND English.
    # -> when real product links (p-number) exist, ONLY those are used, and the ENGLISH copy of each is kept
    #    (nicer link + name; the Arabic one would become /en/<arabic name>/p...)
    if any(re.search(RF_PRODUCT_LINK, u) for u in links):
        before = len(links)
        links = [u for u in links if re.search(RF_PRODUCT_LINK, u)]
        rf_log(f"[RF] {before - len(links)} non-product links dropped (info pages), {len(links)} product links kept")
    links.sort(key=lambda u: RF_LANG_PATH not in u)                          # English links first (stable)
    host = urlsplit(RF_BASE).netloc                                          # ae.reefperfumes.com
    links = [urlunsplit(urlsplit(u)._replace(netloc=host, scheme="https")).replace("/ar/", RF_LANG_PATH)
             for u in links]                                                 # UAE site, English page
    pnum = lambda u: (re.search(r"/p/?(\d{3,})(?:[/?#]|$)", u) or [None, u])[1]   # same p-number twice -> once
    seen, out = set(), []
    for u in links:
        if pnum(u) not in seen:
            seen.add(pnum(u))
            out.append(u)
    rf_log(f"[RF] sitemap.xml HTTP {status}: {len(maps)} sitemap(s) read, {len(out)} product links")
    return out, counts, {"status": status, "index_sample": locs[:15]}


def rf_page(url):
    """One product page -> (status, rows kept, names skipped). Pauses RF_PAGE_PAUSE first; when the shop still
    says "too busy" (429 -> br_get gives up with status 0) it waits RF_BUSY_WAITS and tries again."""
    time.sleep(RF_PAGE_PAUSE)
    status, html = br.br_get(url)
    for wait in RF_BUSY_WAITS:
        if status != 0:
            break
        rf_log(f"[RF] {url}: still too busy -> waiting {wait}s and trying again")
        time.sleep(wait)
        status, html = br.br_get(url)
    if status != 200:
        return status, [], []
    rows = br.br_parse(url, html)
    for r in rows:
        r["brand"] = r["brand"] or RF_BRAND
    return status, [r for r in rows if rf_keep(r["name"], r["product_type"])], \
        [r["name"] for r in rows if not rf_keep(r["name"], r["product_type"])]


# ===== SECTION 6: full download
def rf_save(cur, conn, rows):
    execute_values(cur, RF_UPSERT, [(r["variant_id"], r["url"], r["gtin"], r["sku"], r["brand"], r["name"],
                                     r["product_type"], r["price_inc_tax"], r["price"], r["stock"], r["volume"])
                                    for r in rows if r["variant_id"]])
    conn.commit()


def rf_run():
    started = time.time()
    st = {"status": "running", "mode": None, "saved": 0, "skipped_not_perfume": 0, "in_stock": 0, "out_of_stock": 0,
          "incomplete": False}
    rf_state["last"] = st
    conn = rf_conn()
    cur = conn.cursor()
    try:
        cur.execute(RF_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()

        def count(rows):
            st["saved"] += len(rows)
            st["in_stock"] += sum(r["stock"] == "In stock" for r in rows)
            st["out_of_stock"] += sum(r["stock"] != "In stock" for r in rows)

        first = rf_shopify_page(1)
        if first:                                           # way 1: Shopify
            st["mode"] = "shopify"
            page, products = 1, first
            while products:
                rows, skipped = rf_shopify_rows(products)
                st["skipped_not_perfume"] += len(skipped)
                if rows:
                    rf_save(cur, conn, rows)
                count(rows)
                rf_log(f"[RF] shopify page {page}: {len(products)} products, {len(rows)} saved (total {st['saved']})")
                if len(products) < 250 or page >= RF_MAX_PAGES:
                    break
                page += 1
                time.sleep(1.5)
                products = rf_shopify_page(page)
                if products is None:
                    st["incomplete"] = True
                    break
        else:                                               # way 2: sitemap + product pages
            st["mode"] = "sitemap"
            links, counts, _ = rf_product_links()
            st["links"], st["sitemaps"] = len(links), counts
            if not links:
                raise RuntimeError("no product list found (not Shopify, no product sitemap) - check /reefperfumes/test")
            st.update({"pages_done": 0, "pages_without_data": 0, "pages_failed": 0})
            batch = []
            with ThreadPoolExecutor(max_workers=RF_WORKERS) as pool:
                for status, kept, skipped in pool.map(rf_page, links):
                    st["pages_done"] += 1
                    if status not in (200, 404):
                        st["pages_failed"] += 1
                    elif status == 200 and not kept and not skipped:
                        st["pages_without_data"] += 1
                    st["skipped_not_perfume"] += len(skipped)
                    batch.extend(kept)
                    if len(batch) >= RF_BATCH:
                        rf_save(cur, conn, batch)
                        count(batch)
                        batch = []
                    if st["pages_done"] % 200 == 0:
                        rf_log(f"[RF] {st['pages_done']}/{len(links)} pages, {st['saved']} saved")
            if batch:
                rf_save(cur, conn, batch)
                count(batch)
            st["incomplete"] = st["pages_failed"] > len(links) * 0.05
        if not st["incomplete"] and st["saved"]:            # products gone from the site
            cur.execute("UPDATE reefperfumes_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            st["marked_not_on_site"] = cur.rowcount
            conn.commit()
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        rf_log(f"[RF-DONE] {st}")


def rf_worker():
    try:
        rf_run()
    except Exception as ex:
        rf_log(f"[RF-ERROR] {ex}\n{traceback.format_exc()}")
        rf_state["last"] = {**(rf_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        rf_state["running"] = False
        rf_lock.release()


# ===== SECTION 7: routes
def rf_secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


# ===== SECTION 6b: PROBE (2026-11-05) - where can the UAE product data be read?
# LIVE TEST 2026-11-05: the sitemap (main store reefperfumes.com, Saudi) gives 158 product links, but opening them
# on the UAE site (ae.reefperfumes.com/en/<name>/p123) gives the HOME PAGE (title "Reef Perfumes", no product data).
# rf_probe() shows (nothing saved): where a UAE product link really goes (redirects), the same product on the main
# store, a UAE category page (cards / product links / prices in the HTML) and the Salla store id + Salla API answer.
RF_PROBE_CATEGORY = "/en/-/c585180698"     # a UAE category link from the user's screenshot (2026-11-05)


def rf_snip(html, word, n=3, width=160):
    """Up to n bits of html around `word` (to see how prices / cards are written)."""
    return [html[max(0, m.start() - width // 2):m.start() + width // 2] for m in re.finditer(word, html)][:n]


def rf_probe(links):
    out = {}
    hdr = {"User-Agent": br.BR_HEADERS["User-Agent"], "Accept-Language": "en"}
    if links:
        url = links[0]
        try:                                                     # 1) where does a UAE product link go?
            r = requests.get(url, headers=hdr, timeout=30, allow_redirects=True)
            out["uae_product"] = {"asked": url, "final_url": r.url, "status": r.status_code,
                                  "redirects": [f"{h.status_code} {h.headers.get('Location')}" for h in r.history],
                                  "title": (re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S) or [None, None])[1]}
        except requests.RequestException as ex:
            out["uae_product"] = {"error": str(ex)[:150]}
        main = url.replace(urlsplit(RF_BASE).netloc, "reefperfumes.com")   # 2) same product on the main store
        st, html = br.br_get(main)
        rows = br.br_parse(main, html) if st == 200 else []
        out["main_store_product"] = {"url": main, "status": st,
                                     "rows": [{k: r[k] for k in ("name", "price_inc_tax", "currency", "stock")} for r in rows],
                                     "title": (re.search(r"<title[^>]*>(.*?)</title>", html, re.S) or [None, None])[1]}
    cat = RF_BASE + RF_PROBE_CATEGORY                            # 3) UAE category page
    st, html = br.br_get(cat)
    plinks = sorted(set(re.findall(r'href="([^"]*/p\d{5,}[^"]*)"', html)))
    out["uae_category"] = {"url": cat, "status": st, "size": len(html),
                           "product_links": len(plinks), "first_links": plinks[:5],
                           "product_card_tags": len(re.findall(r"<salla-product-card|custom-salla-product-card|product-card", html)),
                           "price_snips": rf_snip(html, r"AED|price", 3),
                           "ld_types": re.findall(r'"@type"\s*:\s*"(\w+)"', html)[:10]}
    sid = re.search(r'(?:store[_-]?id|storeId|store_identifier)["\'\s:=]+["\']?(\d{5,})', html)   # 4) Salla API
    out["salla_store_id"] = sid.group(1) if sid else None
    if sid:
        try:
            r = requests.get("https://api.salla.dev/store/v1/products", params={"per_page": 3},
                             headers={**hdr, "Accept": "application/json", "Store-Identifier": sid.group(1),
                                      "currency": "AED", "country": "AE"}, timeout=30)
            out["salla_api"] = {"status": r.status_code, "answer": r.text[:1500]}
        except requests.RequestException as ex:
            out["salla_api"] = {"error": str(ex)[:150]}
    else:
        out["store_id_snips"] = rf_snip(html, r"store", 3)
    rf_log(f"[RF-PROBE] {json.dumps(out, default=str)[:1500]}")
    return out


@reefperfumes_bp.route("/reefperfumes/test")
def rf_test_route():
    """Which way works (Shopify / sitemap) + the first products read - nothing is saved."""
    if not rf_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    first = rf_shopify_page(1)
    if first:
        rows, skipped = rf_shopify_rows(first)
        result = {"ok": bool(rows), "mode": "shopify", "page1_products": len(first), "page1_rows": len(rows),
                  "skipped_not_perfume": skipped[:30], "product_types": sorted({r["product_type"] or "-" for r in rows})[:20],
                  "first_5": rows[:5]}
    else:
        links, counts, info = rf_product_links()
        result = {"ok": False, "mode": "sitemap", "shopify_products_json": "not working", "sitemap": info,
                  "sitemaps": counts, "product_links": len(links), "first_links": links[:3], "pages": []}
        if not links:                                       # nothing at all -> show the home page
            st, html = br.br_get(RF_BASE + "/en")
            result["home_en"] = {"status": st, **br.br_raw(RF_BASE + "/en", html)}
        for url in links[:3]:
            st, html = br.br_get(url)
            rows = br.br_parse(url, html) if st == 200 else []
            page = {"url": url, "status": st,
                    "rows": [{**r, "brand": r["brand"] or RF_BRAND, "kept": rf_keep(r["name"], r["product_type"])} for r in rows]}
            if not rows:
                page["raw"] = br.br_raw(url, html)
            result["pages"].append(page)
        result["ok"] = any(p["rows"] for p in result["pages"])
        if not result["ok"]:                                # nothing read -> look where the UAE data is
            result["probe"] = rf_probe(links)
    rf_log(f"[RF-TEST] {json.dumps(result, default=str)[:2000]}")
    return jsonify(result)


@reefperfumes_bp.route("/reefperfumes/run")
def rf_run_route():
    if not rf_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not rf_lock.acquire(blocking=False):
        return jsonify({"error": "Reef Perfumes download already running"}), 409
    rf_state["running"] = True
    rf_log("[RF-RUN] Started")
    threading.Thread(target=rf_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/reefperfumes/status"}), 202


@reefperfumes_bp.route("/reefperfumes/status")
def rf_status_route():
    return jsonify({"running": rf_state["running"], "last": rf_state["last"]})

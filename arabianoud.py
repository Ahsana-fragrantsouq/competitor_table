"""
Arabian Oud catalog (ae.arabianoud.com/en -> Postgres french_fragrance_db, table arabianoud_catalog)
Prefix ao. Menu "Brands Store" (the brand's own shop; copy of abdulsamad.py), works like a competitor (tab, matching, least price, Run 2).
PERFUMES + GIFT SETS only (user's choice, same as the other Brands Store shops): oils, bakhoor, incense, oud wood,
burners, home / body / hair items are left out (ao_keep).

The shop system could NOT be checked from Claude's side (network blocked), so the reader tries 2 ways (ao_mode):
  1. "shopify":  /products.json works -> whole shop list (like Khaltat / Anfasic), filtered by ao_keep
  2. "sitemap":  else /sitemap.xml -> sitemaps with "product" in the name: ALL their links; other (non-blog)
                 sitemaps: only product-looking links (Salla ".../p123456", "/product/") -> Arabic "/ar/" links
                 switched to English AO_LANG_PATH "/en/"
                 -> every product page read from its schema.org JSON-LD (same reader as brandsperfume.py: br_parse)
  /arabianoud/test shows which way works + 3 products read (+ the raw page data when nothing could be read)

Routes (open in the browser):
  GET /arabianoud/test?secret=XXX    which way works + first products (run this first)
  GET /arabianoud/run?secret=XXX     download everything into arabianoud_catalog (background)
  GET /arabianoud/status             progress

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

arabianoud_bp = Blueprint("arabianoud", __name__)


# ===== SECTION 1: settings
AO_BASE = "https://ae.arabianoud.com"
AO_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
              "Accept": "application/json"}
AO_BRAND = "Arabian Oud"        # brand when the shop does not say one (it is their own shop)
AO_LANG_PATH = "/en/"                      # English pages (the user's link: ae.arabianoud.com/en)
AO_SITEMAP_WORD = "product"                # product sitemaps in /sitemap.xml
AO_WORKERS = 4                             # product pages read at the same time (sitemap way)
AO_BATCH = 100                             # rows saved per database write
AO_MAX_PAGES = 100                         # Shopify: max 100 pages x 250
# PERFUMES + GIFT SETS (ao_keep, in this order). WHOLE words, any capitals; "x*" = word start.
AO_ALWAYS_SKIP = ["gift card", "sample", "samples", "discovery", "giveaway*"]
AO_GIFT_WORDS = ["gift set", "gift sets", "giftset", "gift box", "coffret", "set", "collection"]   # -> always kept
AO_SKIP_WORDS = ["oil", "oils", "dehn", "dahn", "bakhoor", "bukhoor", "bakhour", "incense", "dokhoon", "dakhoon",
                 "mabkhara", "mubakhar", "burner", "wood", "chips", "muattar", "candle", "diffuser", "reed",
                 "room spray", "home spray", "car", "body", "lotion", "cream", "shower", "soap", "hair", "deodorant",
                 "accessories", "bag", "pouch", "box only",
                 "gm", "gms", "gram", "grams", "kg"]   # sold by WEIGHT = oud wood / bakhoor ("Oud 350 - 30 gm"), not perfume
AO_PERFUME_WORDS = ["perfume*", "parfum*", "eau", "edp", "edt", "cologne", "fragrance*", "spray", "extrait"]

ao_lock = threading.Lock()
ao_state = {"running": False, "last": None}


def ao_log(*args):
    print(*args, flush=True)


# ===== SECTION 2: database
AO_CREATE = """
CREATE TABLE IF NOT EXISTS arabianoud_catalog (
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

AO_UPSERT = """
INSERT INTO arabianoud_catalog (variant_id, product_url, gtin, sku, brand, name, product_type,
                                price_inc_tax, price, stock, volume)
VALUES %s
ON CONFLICT (variant_id) DO UPDATE SET
    product_url = EXCLUDED.product_url, gtin = EXCLUDED.gtin, sku = EXCLUDED.sku, brand = EXCLUDED.brand,
    name = EXCLUDED.name, product_type = EXCLUDED.product_type, price_inc_tax = EXCLUDED.price_inc_tax,
    price = EXCLUDED.price, stock = EXCLUDED.stock, volume = EXCLUDED.volume, updated_at = NOW();
"""


def ao_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    ao_log("[AO-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ===== SECTION 3: filter (perfumes + gift sets)
def ao_keep(name, category):
    """Order: AO_ALWAYS_SKIP -> out | gift set word -> kept | AO_SKIP_WORDS -> out | category given -> must look like
    perfume / gift | else kept (perfume)."""
    text = f"{name or ''} {category or ''}"
    if br.br_has(AO_ALWAYS_SKIP, text):
        return False
    if br.br_has(AO_GIFT_WORDS, text):
        return True
    if br.br_has(AO_SKIP_WORDS, text):
        return False
    if category:
        return br.br_has(AO_PERFUME_WORDS + ["gift*", "oud"], category) or br.br_has(AO_PERFUME_WORDS, name)
    return True


# ===== SECTION 4: way 1 - Shopify products.json
def ao_shopify_page(page):
    """One page of /products.json -> list of products, [] at the end / not Shopify, None when it failed."""
    try:
        r = requests.get(f"{AO_BASE}/products.json", params={"limit": 250, "page": page}, headers=AO_HEADERS, timeout=60)
    except requests.RequestException as ex:
        ao_log(f"[AO] products.json page {page}: network error {str(ex)[:80]}")
        return None
    if r.status_code != 200:
        return []
    try:
        return r.json().get("products", [])
    except ValueError:
        return []


def ao_shopify_rows(products):
    """Shopify products -> rows (one per variant), perfumes + gift sets only."""
    rows, skipped = [], []
    for p in products:
        title, ptype = (p.get("title") or "").strip(), (p.get("product_type") or "").strip()
        if not ao_keep(title, ptype):
            skipped.append(title)
            continue
        variants = p.get("variants") or []
        for v in variants:
            vt = (v.get("title") or "").strip()
            name = title if vt.lower() in ("", "default title") else f"{title} - {vt}"
            url = f"{AO_BASE}/products/{p.get('handle')}" + (f"?variant={v.get('id')}" if len(variants) > 1 else "")
            price = br.br_num(v.get("price"))
            if price is not None and price <= 0:
                continue                                    # AED 0 = free gift copy
            sku = (v.get("sku") or "").strip() or None
            d = re.sub(r"\D", "", sku or "")
            rows.append({"variant_id": v.get("id"), "url": url, "gtin": d if sku and d == sku and 12 <= len(d) <= 14 else None,
                         "sku": sku, "brand": (p.get("vendor") or "").strip() or AO_BRAND, "name": name,
                         "product_type": ptype or None, "price_inc_tax": price,
                         "price": round(price / 1.05, 2) if price else None,
                         "stock": "In stock" if v.get("available") else "Out of stock",
                         "volume": br.br_volume(vt, title, *(p.get("tags") or []))})
    return rows, skipped


# ===== SECTION 5: way 2 - sitemap + product pages (JSON-LD, brandsperfume.py reader)
AO_NOT_PRODUCT = r"/page-\d+|/blog|/brands?/|/c\d+/?$|/(cart|login|account|search)\b"   # English links that are NOT products
AO_PRODUCT_LINK = r"/p/?\d{3,}(?:[/?#]|$)"  # Salla product page: .../<name>/p123456 (also /p/123456; anywhere in link)
# Arabian Oud = SALLA (live test 2026-11-03). Its sitemap lists the MAIN store links (arabianoud.com/ar/...): the same
# products exist on the UAE site -> every link is moved to OUR host (AO_BASE = ae.arabianoud.com) -> AED prices


def ao_product_links():
    """Product links from the sitemaps -> (links, {sitemap: counts}, info).
    Salla: /sitemap.xml lists ar/sitemap-1.xml, -2 ... (no "product" in the name) -> every non-blog sitemap is read,
    product links = AO_PRODUCT_LINK, Arabic links switched to English ("/ar/" -> AO_LANG_PATH; Salla finds the page
    by its p-number)."""
    status, index = br.br_get(AO_BASE + "/sitemap.xml", "*/*")
    locs = br.br_locs(index)
    xmls = [u for u in locs if u.endswith(".xml")]
    maps = [u for u in xmls if AO_SITEMAP_WORD in u.lower()] or [u for u in xmls if "blog" not in u.lower()]
    counts, links = {}, []
    if not xmls:                                        # sitemap.xml itself lists the pages
        links = [u for u in locs if re.search(AO_PRODUCT_LINK, u) or re.search(r"/products?/", u)]
        counts[AO_BASE + "/sitemap.xml"] = {"status": status, "links": len(locs), "product_links": len(links)}
    for sm in maps:
        st, xml = br.br_get(sm, "*/*")
        found = br.br_locs(xml)
        prods = (found if AO_SITEMAP_WORD in sm.lower()                    # a "products" sitemap: every link
                 else [u for u in found if re.search(AO_PRODUCT_LINK, u) or re.search(r"/products?/", u)])
        if not prods:
            # Arabian Oud (live test 2026-11-03): product links have NO p-number, e.g.
            # arabianoud.com/en/0201020036-oud-350-30-gm (same product in ar / en / ms / fr / de) ->
            # take the ENGLISH links, minus info pages ("/page-164033926") and blog; pages without product data
            # are skipped later by the JSON-LD reader
            prods = [u for u in found if AO_LANG_PATH in u and not re.search(AO_NOT_PRODUCT, u)]
        counts[sm] = {"status": st, "links": len(found), "product_links": len(prods),
                      "sample": found[:5]}                                   # shown on /arabianoud/test
        links.extend(prods)
    host = urlsplit(AO_BASE).netloc                                          # ae.arabianoud.com
    links = [urlunsplit(urlsplit(u)._replace(netloc=host, scheme="https")).replace("/ar/", AO_LANG_PATH)
             for u in links]                                                 # UAE site, English page
    pnum = lambda u: (re.search(r"/p/?(\d{3,})(?:[/?#]|$)", u) or [None, u])[1]   # same p-number twice -> once
    seen, out = set(), []
    for u in links:
        if pnum(u) not in seen:
            seen.add(pnum(u))
            out.append(u)
    ao_log(f"[AO] sitemap.xml HTTP {status}: {len(maps)} sitemap(s) read, {len(out)} product links")
    return out, counts, {"status": status, "index_sample": locs[:15]}


def ao_page(url):
    """One product page -> (status, rows kept, names skipped)."""
    status, html = br.br_get(url)
    if status != 200:
        return status, [], []
    rows = br.br_parse(url, html)
    for r in rows:
        r["brand"] = r["brand"] or AO_BRAND
    return status, [r for r in rows if ao_keep(r["name"], r["product_type"])], \
        [r["name"] for r in rows if not ao_keep(r["name"], r["product_type"])]


# ===== SECTION 6: full download
def ao_save(cur, conn, rows):
    execute_values(cur, AO_UPSERT, [(r["variant_id"], r["url"], r["gtin"], r["sku"], r["brand"], r["name"],
                                     r["product_type"], r["price_inc_tax"], r["price"], r["stock"], r["volume"])
                                    for r in rows if r["variant_id"]])
    conn.commit()


def ao_run():
    started = time.time()
    st = {"status": "running", "mode": None, "saved": 0, "skipped_not_perfume": 0, "in_stock": 0, "out_of_stock": 0,
          "incomplete": False}
    ao_state["last"] = st
    conn = ao_conn()
    cur = conn.cursor()
    try:
        cur.execute(AO_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()

        def count(rows):
            st["saved"] += len(rows)
            st["in_stock"] += sum(r["stock"] == "In stock" for r in rows)
            st["out_of_stock"] += sum(r["stock"] != "In stock" for r in rows)

        first = ao_shopify_page(1)
        if first:                                           # way 1: Shopify
            st["mode"] = "shopify"
            page, products = 1, first
            while products:
                rows, skipped = ao_shopify_rows(products)
                st["skipped_not_perfume"] += len(skipped)
                if rows:
                    ao_save(cur, conn, rows)
                count(rows)
                ao_log(f"[AO] shopify page {page}: {len(products)} products, {len(rows)} saved (total {st['saved']})")
                if len(products) < 250 or page >= AO_MAX_PAGES:
                    break
                page += 1
                time.sleep(1.5)
                products = ao_shopify_page(page)
                if products is None:
                    st["incomplete"] = True
                    break
        else:                                               # way 2: sitemap + product pages
            st["mode"] = "sitemap"
            links, counts, _ = ao_product_links()
            st["links"], st["sitemaps"] = len(links), counts
            if not links:
                raise RuntimeError("no product list found (not Shopify, no product sitemap) - check /arabianoud/test")
            st.update({"pages_done": 0, "pages_without_data": 0, "pages_failed": 0})
            batch = []
            with ThreadPoolExecutor(max_workers=AO_WORKERS) as pool:
                for status, kept, skipped in pool.map(ao_page, links):
                    st["pages_done"] += 1
                    if status not in (200, 404):
                        st["pages_failed"] += 1
                    elif status == 200 and not kept and not skipped:
                        st["pages_without_data"] += 1
                    st["skipped_not_perfume"] += len(skipped)
                    batch.extend(kept)
                    if len(batch) >= AO_BATCH:
                        ao_save(cur, conn, batch)
                        count(batch)
                        batch = []
                    if st["pages_done"] % 200 == 0:
                        ao_log(f"[AO] {st['pages_done']}/{len(links)} pages, {st['saved']} saved")
            if batch:
                ao_save(cur, conn, batch)
                count(batch)
            st["incomplete"] = st["pages_failed"] > len(links) * 0.05
        if not st["incomplete"] and st["saved"]:            # products gone from the site
            cur.execute("UPDATE arabianoud_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            st["marked_not_on_site"] = cur.rowcount
            conn.commit()
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        ao_log(f"[AO-DONE] {st}")


def ao_worker():
    try:
        ao_run()
    except Exception as ex:
        ao_log(f"[AO-ERROR] {ex}\n{traceback.format_exc()}")
        ao_state["last"] = {**(ao_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        ao_state["running"] = False
        ao_lock.release()


# ===== SECTION 7: routes
def ao_secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


@arabianoud_bp.route("/arabianoud/test")
def ao_test_route():
    """Which way works (Shopify / sitemap) + the first products read - nothing is saved."""
    if not ao_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    first = ao_shopify_page(1)
    if first:
        rows, skipped = ao_shopify_rows(first)
        result = {"ok": bool(rows), "mode": "shopify", "page1_products": len(first), "page1_rows": len(rows),
                  "skipped_not_perfume": skipped[:30], "product_types": sorted({r["product_type"] or "-" for r in rows})[:20],
                  "first_5": rows[:5]}
    else:
        links, counts, info = ao_product_links()
        result = {"ok": False, "mode": "sitemap", "shopify_products_json": "not working", "sitemap": info,
                  "sitemaps": counts, "product_links": len(links), "first_links": links[:3], "pages": []}
        if not links:                                       # nothing at all -> show the home page
            st, html = br.br_get(AO_BASE + "/en")
            result["home_en"] = {"status": st, **br.br_raw(AO_BASE + "/en", html)}
        for url in links[:3]:
            st, html = br.br_get(url)
            rows = br.br_parse(url, html) if st == 200 else []
            page = {"url": url, "status": st,
                    "rows": [{**r, "brand": r["brand"] or AO_BRAND, "kept": ao_keep(r["name"], r["product_type"])} for r in rows]}
            if not rows:
                page["raw"] = br.br_raw(url, html)
            result["pages"].append(page)
        result["ok"] = any(p["rows"] for p in result["pages"])
    ao_log(f"[AO-TEST] {json.dumps(result, default=str)[:2000]}")
    return jsonify(result)


@arabianoud_bp.route("/arabianoud/run")
def ao_run_route():
    if not ao_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not ao_lock.acquire(blocking=False):
        return jsonify({"error": "Arabian Oud download already running"}), 409
    ao_state["running"] = True
    ao_log("[AO-RUN] Started")
    threading.Thread(target=ao_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/arabianoud/status"}), 202


@arabianoud_bp.route("/arabianoud/status")
def ao_status_route():
    return jsonify({"running": ao_state["running"], "last": ao_state["last"]})

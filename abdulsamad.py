"""
Abdul Samad Al Qurashi catalog (ae.abdulsamadalqurashi.com/en -> Postgres french_fragrance_db, table abdulsamad_catalog)
Prefix aq. Menu "Brands Store" (the brand's own shop), works like a competitor (tab, matching, least price, Run 2).
PERFUMES + GIFT SETS only (user's choice, same as the other Brands Store shops): oils, bakhoor, incense, oud wood,
burners, home / body / hair items are left out (aq_keep).

The shop system could NOT be checked from Claude's side (network blocked), so the reader tries 2 ways (aq_mode):
  1. "shopify":  /products.json works -> whole shop list (like Khaltat / Anfasic), filtered by aq_keep
  2. "sitemap":  else /sitemap.xml -> product sitemaps (live: SALLA shop, ar/sitemap-1.xml ... -> product links
                 ".../p123456", switched to English AQ_LANG_PATH "/en/")
                 -> every product page read from its schema.org JSON-LD (same reader as brandsperfume.py: br_parse)
  /abdulsamad/test shows which way works + 3 products read (+ the raw page data when nothing could be read)

Routes (open in the browser):
  GET /abdulsamad/test?secret=XXX    which way works + first products (run this first)
  GET /abdulsamad/run?secret=XXX     download everything into abdulsamad_catalog (background)
  GET /abdulsamad/status             progress

Env vars (already set on Render): FF_DATABASE_URL, RUN_SECRET
"""

import os
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

abdulsamad_bp = Blueprint("abdulsamad", __name__)


# ===== SECTION 1: settings
AQ_BASE = "https://ae.abdulsamadalqurashi.com"
AQ_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
              "Accept": "application/json"}
AQ_BRAND = "Abdul Samad Al Qurashi"        # brand when the shop does not say one (it is their own shop)
AQ_LANG_PATH = "/en/"                      # English pages (the user's link: ae.abdulsamadalqurashi.com/en)
AQ_SITEMAP_WORD = "product"                # product sitemaps in /sitemap.xml
AQ_WORKERS = 4                             # product pages read at the same time (sitemap way)
AQ_BATCH = 100                             # rows saved per database write
AQ_MAX_PAGES = 100                         # Shopify: max 100 pages x 250
# PERFUMES + GIFT SETS (aq_keep, in this order). WHOLE words, any capitals; "x*" = word start.
AQ_ALWAYS_SKIP = ["gift card", "sample", "samples", "discovery", "giveaway*"]
AQ_GIFT_WORDS = ["gift set", "gift sets", "giftset", "gift box", "coffret", "set", "collection"]   # -> always kept
AQ_SKIP_WORDS = ["oil", "oils", "dehn", "dahn", "bakhoor", "bukhoor", "bakhour", "incense", "dokhoon", "dakhoon",
                 "mabkhara", "mubakhar", "burner", "wood", "chips", "muattar", "candle", "diffuser", "reed",
                 "room spray", "home spray", "car", "body", "lotion", "cream", "shower", "soap", "hair", "deodorant",
                 "accessories", "bag", "pouch", "box only"]                                       # -> left out
AQ_PERFUME_WORDS = ["perfume*", "parfum*", "eau", "edp", "edt", "cologne", "fragrance*", "spray", "extrait"]

aq_lock = threading.Lock()
aq_state = {"running": False, "last": None}


def aq_log(*args):
    print(*args, flush=True)


# ===== SECTION 2: database
AQ_CREATE = """
CREATE TABLE IF NOT EXISTS abdulsamad_catalog (
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

AQ_UPSERT = """
INSERT INTO abdulsamad_catalog (variant_id, product_url, gtin, sku, brand, name, product_type,
                                price_inc_tax, price, stock, volume)
VALUES %s
ON CONFLICT (variant_id) DO UPDATE SET
    product_url = EXCLUDED.product_url, gtin = EXCLUDED.gtin, sku = EXCLUDED.sku, brand = EXCLUDED.brand,
    name = EXCLUDED.name, product_type = EXCLUDED.product_type, price_inc_tax = EXCLUDED.price_inc_tax,
    price = EXCLUDED.price, stock = EXCLUDED.stock, volume = EXCLUDED.volume, updated_at = NOW();
"""


def aq_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    aq_log("[AQ-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ===== SECTION 3: filter (perfumes + gift sets)
def aq_keep(name, category):
    """Order: AQ_ALWAYS_SKIP -> out | gift set word -> kept | AQ_SKIP_WORDS -> out | category given -> must look like
    perfume / gift | else kept (perfume)."""
    text = f"{name or ''} {category or ''}"
    if br.br_has(AQ_ALWAYS_SKIP, text):
        return False
    if br.br_has(AQ_GIFT_WORDS, text):
        return True
    if br.br_has(AQ_SKIP_WORDS, text):
        return False
    if category:
        return br.br_has(AQ_PERFUME_WORDS + ["gift*", "oud"], category) or br.br_has(AQ_PERFUME_WORDS, name)
    return True


# ===== SECTION 4: way 1 - Shopify products.json
def aq_shopify_page(page):
    """One page of /products.json -> list of products, [] at the end / not Shopify, None when it failed."""
    try:
        r = requests.get(f"{AQ_BASE}/products.json", params={"limit": 250, "page": page}, headers=AQ_HEADERS, timeout=60)
    except requests.RequestException as ex:
        aq_log(f"[AQ] products.json page {page}: network error {str(ex)[:80]}")
        return None
    if r.status_code != 200:
        return []
    try:
        return r.json().get("products", [])
    except ValueError:
        return []


def aq_shopify_rows(products):
    """Shopify products -> rows (one per variant), perfumes + gift sets only."""
    rows, skipped = [], []
    for p in products:
        title, ptype = (p.get("title") or "").strip(), (p.get("product_type") or "").strip()
        if not aq_keep(title, ptype):
            skipped.append(title)
            continue
        variants = p.get("variants") or []
        for v in variants:
            vt = (v.get("title") or "").strip()
            name = title if vt.lower() in ("", "default title") else f"{title} - {vt}"
            url = f"{AQ_BASE}/products/{p.get('handle')}" + (f"?variant={v.get('id')}" if len(variants) > 1 else "")
            price = br.br_num(v.get("price"))
            if price is not None and price <= 0:
                continue                                    # AED 0 = free gift copy
            sku = (v.get("sku") or "").strip() or None
            d = re.sub(r"\D", "", sku or "")
            rows.append({"variant_id": v.get("id"), "url": url, "gtin": d if sku and d == sku and 12 <= len(d) <= 14 else None,
                         "sku": sku, "brand": (p.get("vendor") or "").strip() or AQ_BRAND, "name": name,
                         "product_type": ptype or None, "price_inc_tax": price,
                         "price": round(price / 1.05, 2) if price else None,
                         "stock": "In stock" if v.get("available") else "Out of stock",
                         "volume": br.br_volume(vt, title, *(p.get("tags") or []))})
    return rows, skipped


# ===== SECTION 5: way 2 - sitemap + product pages (JSON-LD, brandsperfume.py reader)
AQ_PRODUCT_LINK = r"/p\d+/?$"             # Salla product page: .../<name>/p123456 (live test 2026-11-02: Salla shop)


def aq_product_links():
    """Product links from the sitemaps -> (links, {sitemap: counts}, info).
    Salla: /sitemap.xml lists ar/sitemap-1.xml, -2 ... (no "product" in the name) -> every non-blog sitemap is read,
    product links = AQ_PRODUCT_LINK, Arabic links switched to English ("/ar/" -> AQ_LANG_PATH; Salla finds the page
    by its p-number)."""
    status, index = br.br_get(AQ_BASE + "/sitemap.xml", "*/*")
    locs = br.br_locs(index)
    xmls = [u for u in locs if u.endswith(".xml")]
    maps = [u for u in xmls if AQ_SITEMAP_WORD in u.lower()] or [u for u in xmls if "blog" not in u.lower()]
    counts, links = {}, []
    if not xmls:                                        # sitemap.xml itself lists the pages
        links = [u for u in locs if re.search(AQ_PRODUCT_LINK, u) or re.search(r"/products?/", u)]
        counts[AQ_BASE + "/sitemap.xml"] = {"status": status, "links": len(locs), "product_links": len(links)}
    for sm in maps:
        st, xml = br.br_get(sm, "*/*")
        found = br.br_locs(xml)
        prods = [u for u in found if re.search(AQ_PRODUCT_LINK, u) or re.search(r"/products?/", u)]
        counts[sm] = {"status": st, "links": len(found), "product_links": len(prods)}
        links.extend(prods)
    links = [u.replace("/ar/", AQ_LANG_PATH) for u in links]                 # English page of the same product
    pnum = lambda u: (re.search(r"/p(\d+)/?$", u) or [None, u])[1]          # same p-number twice -> once
    seen, out = set(), []
    for u in links:
        if pnum(u) not in seen:
            seen.add(pnum(u))
            out.append(u)
    aq_log(f"[AQ] sitemap.xml HTTP {status}: {len(maps)} sitemap(s) read, {len(out)} product links")
    return out, counts, {"status": status, "index_sample": locs[:15]}


def aq_page(url):
    """One product page -> (status, rows kept, names skipped)."""
    status, html = br.br_get(url)
    if status != 200:
        return status, [], []
    rows = br.br_parse(url, html)
    for r in rows:
        r["brand"] = r["brand"] or AQ_BRAND
    return status, [r for r in rows if aq_keep(r["name"], r["product_type"])], \
        [r["name"] for r in rows if not aq_keep(r["name"], r["product_type"])]


# ===== SECTION 6: full download
def aq_save(cur, conn, rows):
    execute_values(cur, AQ_UPSERT, [(r["variant_id"], r["url"], r["gtin"], r["sku"], r["brand"], r["name"],
                                     r["product_type"], r["price_inc_tax"], r["price"], r["stock"], r["volume"])
                                    for r in rows if r["variant_id"]])
    conn.commit()


def aq_run():
    started = time.time()
    st = {"status": "running", "mode": None, "saved": 0, "skipped_not_perfume": 0, "in_stock": 0, "out_of_stock": 0,
          "incomplete": False}
    aq_state["last"] = st
    conn = aq_conn()
    cur = conn.cursor()
    try:
        cur.execute(AQ_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()

        def count(rows):
            st["saved"] += len(rows)
            st["in_stock"] += sum(r["stock"] == "In stock" for r in rows)
            st["out_of_stock"] += sum(r["stock"] != "In stock" for r in rows)

        first = aq_shopify_page(1)
        if first:                                           # way 1: Shopify
            st["mode"] = "shopify"
            page, products = 1, first
            while products:
                rows, skipped = aq_shopify_rows(products)
                st["skipped_not_perfume"] += len(skipped)
                if rows:
                    aq_save(cur, conn, rows)
                count(rows)
                aq_log(f"[AQ] shopify page {page}: {len(products)} products, {len(rows)} saved (total {st['saved']})")
                if len(products) < 250 or page >= AQ_MAX_PAGES:
                    break
                page += 1
                time.sleep(1.5)
                products = aq_shopify_page(page)
                if products is None:
                    st["incomplete"] = True
                    break
        else:                                               # way 2: sitemap + product pages
            st["mode"] = "sitemap"
            links, counts, _ = aq_product_links()
            st["links"], st["sitemaps"] = len(links), counts
            if not links:
                raise RuntimeError("no product list found (not Shopify, no product sitemap) - check /abdulsamad/test")
            st.update({"pages_done": 0, "pages_without_data": 0, "pages_failed": 0})
            batch = []
            with ThreadPoolExecutor(max_workers=AQ_WORKERS) as pool:
                for status, kept, skipped in pool.map(aq_page, links):
                    st["pages_done"] += 1
                    if status not in (200, 404):
                        st["pages_failed"] += 1
                    elif status == 200 and not kept and not skipped:
                        st["pages_without_data"] += 1
                    st["skipped_not_perfume"] += len(skipped)
                    batch.extend(kept)
                    if len(batch) >= AQ_BATCH:
                        aq_save(cur, conn, batch)
                        count(batch)
                        batch = []
                    if st["pages_done"] % 200 == 0:
                        aq_log(f"[AQ] {st['pages_done']}/{len(links)} pages, {st['saved']} saved")
            if batch:
                aq_save(cur, conn, batch)
                count(batch)
            st["incomplete"] = st["pages_failed"] > len(links) * 0.05
        if not st["incomplete"] and st["saved"]:            # products gone from the site
            cur.execute("UPDATE abdulsamad_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            st["marked_not_on_site"] = cur.rowcount
            conn.commit()
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        aq_log(f"[AQ-DONE] {st}")


def aq_worker():
    try:
        aq_run()
    except Exception as ex:
        aq_log(f"[AQ-ERROR] {ex}\n{traceback.format_exc()}")
        aq_state["last"] = {**(aq_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        aq_state["running"] = False
        aq_lock.release()


# ===== SECTION 7: routes
def aq_secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


@abdulsamad_bp.route("/abdulsamad/test")
def aq_test_route():
    """Which way works (Shopify / sitemap) + the first products read - nothing is saved."""
    if not aq_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    first = aq_shopify_page(1)
    if first:
        rows, skipped = aq_shopify_rows(first)
        result = {"ok": bool(rows), "mode": "shopify", "page1_products": len(first), "page1_rows": len(rows),
                  "skipped_not_perfume": skipped[:30], "product_types": sorted({r["product_type"] or "-" for r in rows})[:20],
                  "first_5": rows[:5]}
    else:
        links, counts, info = aq_product_links()
        result = {"ok": False, "mode": "sitemap", "shopify_products_json": "not working", "sitemap": info,
                  "sitemaps": counts, "product_links": len(links), "first_links": links[:3], "pages": []}
        if not links:                                       # nothing at all -> show the home page
            st, html = br.br_get(AQ_BASE + "/en")
            result["home_en"] = {"status": st, **br.br_raw(AQ_BASE + "/en", html)}
        for url in links[:3]:
            st, html = br.br_get(url)
            rows = br.br_parse(url, html) if st == 200 else []
            page = {"url": url, "status": st,
                    "rows": [{**r, "brand": r["brand"] or AQ_BRAND, "kept": aq_keep(r["name"], r["product_type"])} for r in rows]}
            if not rows:
                page["raw"] = br.br_raw(url, html)
            result["pages"].append(page)
        result["ok"] = any(p["rows"] for p in result["pages"])
    aq_log(f"[AQ-TEST] {json.dumps(result, default=str)[:2000]}")
    return jsonify(result)


@abdulsamad_bp.route("/abdulsamad/run")
def aq_run_route():
    if not aq_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not aq_lock.acquire(blocking=False):
        return jsonify({"error": "Abdul Samad Al Qurashi download already running"}), 409
    aq_state["running"] = True
    aq_log("[AQ-RUN] Started")
    threading.Thread(target=aq_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/abdulsamad/status"}), 202


@abdulsamad_bp.route("/abdulsamad/status")
def aq_status_route():
    return jsonify({"running": aq_state["running"], "last": aq_state["last"]})

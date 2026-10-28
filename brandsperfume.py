"""
Brands Perfume catalog (brandsperfume.com -> Postgres french_fragrance_db, table brandsperfume_catalog), prefix br
(bp = Branded Perfume). Menu Competitors, in Run 2.

Brands Perfume is NOT Shopify / WooCommerce: a custom web shop, one copy per country + language
(brandsperfume.com/ae/en/..., /ae/ar/..., /gb/en/...); the bare home page answers "Not Found".
How it is read (live test 2026-10-28: products.json = 404, sitemap works):
  1. https://brandsperfume.com/sitemap.xml -> the UAE product sitemaps (name has BR_SITEMAP_WORD "AE-products":
     AE-products-1.xml, -2, -3 ...) -> every product link of the UAE ENGLISH shop (BR_COUNTRY_PATH "/ae/en/")
  2. every product page is opened (BR_WORKERS at the same time) and read from its built-in product data
     (schema.org JSON-LD "Product": name, brand, sku, barcode, price, currency, availability; one row per offer/size)
     - no JSON-LD -> page meta tags (og:title, product:price:amount, availability)
  3. PERFUMES ONLY: names / categories with a word of BR_SKIP_WORDS are left out (gift set, body mist ...)
  /brandsperfume/test reads 3 product pages and SHOWS what was found (+ the raw page data when nothing could be read)
Row id (variant_id) = number made from link + SKU (the site has no numeric variant id).

Routes (open in the browser):
  GET /brandsperfume/test?secret=XXX    sitemap counts + 3 product pages read (run this first)
  GET /brandsperfume/run?secret=XXX     download everything into brandsperfume_catalog (background, ~15-30 min)
  GET /brandsperfume/status             progress

Env vars (already set on Render): FF_DATABASE_URL, RUN_SECRET
"""

import os
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
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
BR_RETRY_WAITS = [5, 20, 60]
BR_SITEMAP_WORD = "AE-products"           # UAE product sitemaps in /sitemap.xml (AE-products-1.xml ...)
BR_COUNTRY_PATH = "/ae/en/"               # UAE shop, English pages (prices in AED)
BR_WORKERS = 4                            # product pages read at the same time (be polite: small number)
BR_BATCH = 100                            # rows saved per database write
# ONLY PERFUMES - change this list if /brandsperfume/test shows wrong products:
# always left out: title or product type has one of these WHOLE words (any capitals). "moisturi*" = word start.
BR_SKIP_WORDS = ["gift set", "giftset", "coffret", "set", "body mist", "hair mist", "body spray", "deodorant",
                 "deo", "body lotion", "body cream", "shower gel", "bath", "soap", "candle", "diffuser",
                 "lipstick", "mascara", "foundation", "serum", "moisturi*", "cleanser", "bag", "pouch",
                 "gift card", "sample"]

br_lock = threading.Lock()
br_state = {"running": False, "last": None}


def br_log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- database
BR_CREATE = """
CREATE TABLE IF NOT EXISTS brandsperfume_catalog (
    id            SERIAL PRIMARY KEY,
    variant_id    BIGINT UNIQUE NOT NULL,   -- number made from link + SKU (one row per size)
    product_url   TEXT,                     -- https://brandsperfume.com/ae/en/...
    gtin          TEXT,                     -- barcode (gtin13 ... in the page data, or a 12-14 digit SKU)
    sku           TEXT,                     -- their SKU
    brand         TEXT,                     -- e.g. "Tom Ford"
    name          TEXT,                     -- "Oud Wood EDP 50 ml"
    product_type  TEXT,                     -- category from the page data (when given)
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




def br_has(words, text):
    """True when text has one of the WHOLE words ("moisturi*" = word start; any capitals, "Gift-Set" = "gift set")."""
    text = " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()) + " "
    return any(re.search(rf"\b{re.escape(w.rstrip('*'))}" + ("" if w.endswith("*") else r"\b"), text) for w in words)


def br_get(url, accept="text/html"):
    """GET with retries when busy. Returns (status, text); status 0 = network error after all retries."""
    for attempt, wait in enumerate([0] + BR_RETRY_WAITS):
        if wait:
            time.sleep(wait)
        try:
            r = requests.get(url, headers={**BR_HEADERS, "Accept": accept}, timeout=40)
        except requests.RequestException as ex:
            br_log(f"[BR] {url}: network error {str(ex)[:80]} (try {attempt + 1})")
            continue
        if r.status_code == 429 or r.status_code >= 500:
            br_log(f"[BR] {url}: HTTP {r.status_code} busy (try {attempt + 1})")
            continue
        return r.status_code, r.text or ""
    return 0, ""


def br_locs(xml):
    """All <loc> links of a sitemap (index or list)."""
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml or "")


def br_product_links():
    """Every UAE English product link from the AE-products sitemaps -> (links, {sitemap: count})."""
    status, index = br_get(BR_BASE + "/sitemap.xml", "*/*")
    maps = [u for u in br_locs(index) if BR_SITEMAP_WORD.lower() in u.lower()]
    br_log(f"[BR] sitemap.xml HTTP {status}: {len(maps)} product sitemap(s) with '{BR_SITEMAP_WORD}'")
    links, counts = [], {}
    for sm in maps:
        st, xml = br_get(sm, "*/*")
        found = br_locs(xml)
        mine = [u for u in found if BR_COUNTRY_PATH in u] or [u for u in found if "/ae/" in u]
        counts[sm] = {"status": st, "links": len(found), "uae_english": len(mine)}
        links.extend(mine)
    links = list(dict.fromkeys(links))                         # same link twice -> once
    br_log(f"[BR] {len(links)} UAE product links")
    return links, counts


def br_ld_objects(html):
    """Every JSON-LD object of the page (lists and @graph opened up)."""
    out = []
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop(0)
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                out.append(d)
                stack.extend(d.get("@graph") or [])
                for v in d.get("hasVariant") or []:                    # ProductGroup -> its sizes
                    if isinstance(v, dict):                            # size without brand / category -> group's
                        stack.append({**{k: d[k] for k in ("brand", "category", "gtin13") if d.get(k)}, **v})
    return out


def br_meta(html, name):
    """<meta property|name="name" content="..."> -> content"""
    m = re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']*)', html, re.I) \
        or re.search(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{re.escape(name)}["\']', html, re.I)
    return m.group(1).strip() if m else None


def br_num(v):
    try:
        return round(float(str(v).replace(",", "")), 2)
    except (TypeError, ValueError):
        return None


def br_row(url, name, brand, sku, gtin, price, currency, avail, category):
    """One product row (dict) - same fields as the other shop tables."""
    sku = (str(sku).strip() or None) if sku not in (None, "") else None
    digits = re.sub(r"\D", "", str(gtin or ""))
    if not (8 <= len(digits) <= 14):                            # no barcode given -> SKU when it is a barcode
        d2 = re.sub(r"\D", "", sku or "")
        digits = d2 if sku and d2 == sku and 12 <= len(d2) <= 14 else ""
    avail = str(avail or "")
    stock = ("In stock" if re.search(r"InStock|LimitedAvailability|in.?stock", avail, re.I)
             else "Out of stock" if avail else ("In stock" if price else "Out of stock"))   # no availability: price = buyable
    key = f"{url}|{sku or name}"
    return {"variant_id": int(hashlib.md5(key.encode()).hexdigest()[:15], 16), "url": url, "gtin": digits or None,
            "sku": sku, "brand": (brand or "").strip() or None, "name": (name or "").strip() or None,
            "product_type": (category or "").strip() or None, "price_inc_tax": price,
            "price": round(price / 1.05, 2) if price else None, "stock": stock,
            "volume": br_volume(name, sku), "currency": currency}


def br_parse(url, html):
    """Product page -> list of rows (one per offer / size). [] when no product data was found."""
    rows = []
    for d in br_ld_objects(html):
        types = d.get("@type") if isinstance(d.get("@type"), list) else [d.get("@type")]
        if not any(t in ("Product", "ProductModel") for t in types):
            continue
        brand = d.get("brand")
        brand = brand.get("name") if isinstance(brand, dict) else brand
        gtin = d.get("gtin13") or d.get("gtin") or d.get("gtin12") or d.get("gtin14") or d.get("gtin8")
        offers = d.get("offers") or []
        offers = offers if isinstance(offers, list) else [offers]
        flat = []
        for o in offers:                                          # AggregateOffer -> its offers
            if isinstance(o, dict) and isinstance(o.get("offers"), list):
                flat.extend(o["offers"])
            elif isinstance(o, dict):
                flat.append(o)
        if not flat:
            flat = [{}]
        for o in flat:
            price = br_num(o.get("price") or o.get("lowPrice") or (o.get("priceSpecification") or {}).get("price"))
            name = o.get("name") if o.get("name") and o.get("name") != d.get("name") else d.get("name")
            rows.append(br_row(o.get("url") or url, name, brand, o.get("sku") or d.get("sku"),
                               o.get("gtin13") or o.get("gtin") or gtin, price,
                               o.get("priceCurrency"), o.get("availability"), d.get("category")))
    if not rows:                                                  # no JSON-LD product -> meta tags
        name = br_meta(html, "og:title")
        price = br_num(br_meta(html, "product:price:amount") or br_meta(html, "og:price:amount"))
        if name and price:
            rows.append(br_row(url, name, br_meta(html, "product:brand"), None, None, price,
                               br_meta(html, "product:price:currency"),
                               br_meta(html, "product:availability") or br_meta(html, "og:availability"), None))
    seen, out = set(), []
    for r in rows:                                                # same offer twice -> once
        if r["variant_id"] not in seen:
            seen.add(r["variant_id"])
            out.append(r)
    return out


def br_keep(r):
    """PERFUMES ONLY: False when name / category has a word of BR_SKIP_WORDS."""
    return not br_has(BR_SKIP_WORDS, f"{r['name'] or ''} {r['product_type'] or ''}")


def br_page(url):
    """Read ONE product page -> (status, rows kept, rows skipped)."""
    status, html = br_get(url)
    if status != 200:
        return status, [], []
    rows = br_parse(url, html)
    return status, [r for r in rows if br_keep(r)], [r for r in rows if not br_keep(r)]


# ---------------------------------------------------------------- full download
def br_run():
    started = time.time()
    st = {"status": "running", "links": 0, "pages_done": 0, "pages_without_data": 0, "pages_not_found": 0,
          "pages_failed": 0, "saved": 0, "skipped_not_perfume": 0, "in_stock": 0, "out_of_stock": 0,
          "not_aed": 0, "incomplete": False}
    br_state["last"] = st
    conn = br_conn()
    cur = conn.cursor()
    try:
        cur.execute(BR_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()
        links, counts = br_product_links()
        st["links"], st["sitemaps"] = len(links), counts
        if not links:
            raise RuntimeError("no UAE product links in the sitemap - check /brandsperfume/test")
        batch = []

        def save(rows):
            execute_values(cur, BR_UPSERT, [(r["variant_id"], r["url"], r["gtin"], r["sku"], r["brand"], r["name"],
                                             r["product_type"], r["price_inc_tax"], r["price"], r["stock"],
                                             r["volume"]) for r in rows])
            conn.commit()

        with ThreadPoolExecutor(max_workers=BR_WORKERS) as pool:
            for status, kept, skipped in pool.map(br_page, links):
                st["pages_done"] += 1
                if status == 404:
                    st["pages_not_found"] += 1
                elif status != 200:
                    st["pages_failed"] += 1
                elif not kept and not skipped:
                    st["pages_without_data"] += 1
                st["skipped_not_perfume"] += len(skipped)
                st["not_aed"] += sum(1 for r in kept if r["currency"] and r["currency"].upper() != "AED")
                batch.extend(kept)
                if len(batch) >= BR_BATCH:
                    save(batch)
                    st["saved"] += len(batch)
                    st["in_stock"] += sum(r["stock"] == "In stock" for r in batch)
                    st["out_of_stock"] += sum(r["stock"] != "In stock" for r in batch)
                    batch = []
                if st["pages_done"] % 200 == 0:
                    st["seconds"] = round(time.time() - started, 1)
                    br_log(f"[BR] {st['pages_done']}/{len(links)} pages, {st['saved']} saved")
        if batch:
            save(batch)
            st["saved"] += len(batch)
            st["in_stock"] += sum(r["stock"] == "In stock" for r in batch)
            st["out_of_stock"] += sum(r["stock"] != "In stock" for r in batch)
        st["incomplete"] = st["pages_failed"] > len(links) * 0.05      # many failures -> do not mark "Not on site"
        if not st["incomplete"] and st["saved"]:                         # products gone from the site
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


def br_raw(url, html):
    """What a product page holds when nothing could be read (sent to Claude to fix the reader)."""
    ld = re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I)
    return {"title": (re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I) or [None, None])[1],
            "size": len(html), "ld_json_blocks": len(ld), "ld_json_start": [b.strip()[:500] for b in ld[:2]],
            "og_title": br_meta(html, "og:title"), "price_meta": br_meta(html, "product:price:amount"),
            "next_data": "__NEXT_DATA__" in html, "nuxt": "__NUXT__" in html,
            "aed_snippets": [html[max(0, m.start() - 80):m.start() + 40] for m in re.finditer(r"AED", html)][:3],
            "scripts": re.findall(r'<script[^>]+src="([^"]+)"', html)[:6]}


@brandsperfume_bp.route("/brandsperfume/test")
def br_test_route():
    """Sitemap counts + 3 product pages read - shows whether Render can read Brands Perfume and what is read."""
    if not br_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    links, counts = br_product_links()
    result = {"ok": False, "sitemaps": counts, "uae_links": len(links), "first_links": links[:3], "pages": []}
    for url in links[:3]:
        status, html = br_get(url)
        rows = br_parse(url, html) if status == 200 else []
        page = {"url": url, "status": status, "rows": [{**r, "kept": br_keep(r)} for r in rows]}
        if not rows:
            page["raw"] = br_raw(url, html)
        result["pages"].append(page)
    result["ok"] = any(p["rows"] for p in result["pages"])
    br_log(f"[BR-TEST] {json.dumps(result, default=str)[:2000]}")
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
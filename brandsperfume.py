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
  3. PERFUMES + GIFT SETS only (user's choice; the site's menu also has Makeup, Hair / Skin Care, Bath & Body,
     Home Fragrances, Accessories, Discovery Scents, Nail Care -> left out), see br_keep()
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
import datetime
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
# PERFUMES + GIFT SETS ONLY (br_keep, in this order) - change these lists if /brandsperfume/test shows wrong products.
# Words are WHOLE words, any capitals; "moisturi*" = word start. Checked in name + category (page category / breadcrumb).
BR_ALWAYS_SKIP = ["gift card", "discovery", "sample", "samples", "vial", "tester strip"]   # never (Discovery Scents ...)
BR_GIFT_WORDS = ["gift set", "gift sets", "giftset", "gift box", "coffret", "set"]          # gift set -> ALWAYS kept
BR_SKIP_WORDS = ["makeup", "make up", "hair care", "hair mist", "hair perfume", "skin care", "skincare",
                 "bath", "body", "shower", "deodorant", "deo", "soap", "lotion", "cream", "home fragrance",
                 "home fragrances", "candle", "diffuser", "reed", "room spray", "incense", "bakhoor", "accessories",
                 "accessory", "bag", "pouch", "nail", "polish", "lipstick", "mascara", "foundation", "serum",
                 "moisturi*", "cleanser", "shampoo", "conditioner"]                        # not perfume -> dropped
# when the page gives a category / breadcrumb: it must have one of these (else dropped)
BR_KEEP_CATEGORIES = ["perfume*", "parfum*", "fragrance*", "eau", "cologne", "attar", "oud", "gift*"]


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


def br_crumbs(objs):
    """BreadcrumbList names (e.g. "Home > Perfumes > Men") -> "Perfumes Men" (used as category when none given)."""
    for d in objs:
        if d.get("@type") == "BreadcrumbList":
            names = []
            for it in d.get("itemListElement") or []:
                item = it.get("item") if isinstance(it, dict) else None
                n = (it.get("name") or (item.get("name") if isinstance(item, dict) else None)) if isinstance(it, dict) else None
                if n and str(n).strip().lower() not in ("home", "brands perfume"):
                    names.append(str(n).strip())
            return " ".join(names[:-1] if len(names) > 1 else names)   # last crumb = the product itself
    return ""


def br_parse(url, html):
    """Product page -> list of rows (one per offer / size). [] when no product data was found."""
    rows = []
    objs = br_ld_objects(html)
    crumbs = br_crumbs(objs)
    for d in objs:
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
                               o.get("priceCurrency"), o.get("availability"), d.get("category") or crumbs))
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
    """PERFUMES + GIFT SETS only. Order: BR_ALWAYS_SKIP -> dropped | gift set word -> kept (also "EDP + Body Lotion")
    | BR_SKIP_WORDS -> dropped | page category given -> must have a BR_KEEP_CATEGORIES word | else kept (perfume)."""
    name, cat = r["name"] or "", r["product_type"] or ""
    text = f"{name} {cat}"
    if br_has(BR_ALWAYS_SKIP, text):
        return False
    if br_has(BR_GIFT_WORDS, text):
        return True
    if br_has(BR_SKIP_WORDS, text):
        return False
    if cat:                                                     # e.g. "Home Fragrances" already dropped above
        return br_has(BR_KEEP_CATEGORIES, cat)
    return True


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


# ============================================================ SECTION 6: DAILY QUICK CHECK of LINKED products (2026-11-05)
# User: Brands Perfume prices / stock on the site differ from ours (Fiamma 100ml site 189.42, ours 144.90 from the
# 04 Nov download). The full download runs only once per Run 2 round (~14 days, 13 shops one a day).
# Quick check = EVERY DAY at BR_QUICK_TIME India time: only the Brands Perfume pages LINKED to our products
# (competitor_table.br_link, ~1,400) are read again -> price + stock updated in brandsperfume_catalog
# (TRIGGER A copies them to the Competitor table, TRIGGER B recalculates least / suggested price).
# Page gone (404) -> 'Not on site'. Runs before Run 2 (07:00), whose update-least then sends automatic brands.
#   GET /brandsperfume-quick/run?secret=XXX   run it now (background)     GET /brandsperfume-quick/status
#   Render env BR_QUICK_CHECK=off switches the daily run off.
BR_QUICK_TIME = "06:15"                     # "HH:MM" India time, every day
BR_QUICK_KEY = "br_quick_check"             # app_settings: date of the last quick check (no 2nd run after a restart)
brq_state = {"running": False, "last": None}

BR_LINKED_SQL = """
SELECT DISTINCT rtrim(c.product_url, '/') FROM brandsperfume_catalog c
 WHERE c.product_url IS NOT NULL
   AND rtrim(c.product_url, '/') IN (SELECT rtrim(br_link, '/') FROM competitor_table
                                      WHERE br_link IS NOT NULL AND br_link <> '')
"""


def br_india_now():
    """India time (UTC + 5:30) without importing daily_update."""
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)


def br_quick_run():
    """Read every linked Brands Perfume page again -> price + stock saved. Called with br_lock held."""
    started = time.time()
    st = {"status": "running", "started": br_india_now().strftime("%Y-%m-%d %H:%M"), "links": 0, "pages_done": 0,
          "rows_updated": 0, "price_changed": 0, "stock_changed": 0, "not_on_site": 0, "pages_failed": 0,
          "pages_without_data": 0}
    brq_state["last"] = st
    conn = br_conn()
    cur = conn.cursor()
    try:
        cur.execute(BR_LINKED_SQL)
        links = [r[0] for r in cur.fetchall()]
        st["links"] = len(links)
        br_log(f"[BR-QUICK] {len(links)} Brands Perfume pages linked to our products -> reading them again")

        def read(url):
            status, html = br_get(url)
            return url, status, (br_parse(url, html) if status == 200 else [])

        with ThreadPoolExecutor(max_workers=BR_WORKERS) as pool:
            for url, status, rows in pool.map(read, links):
                st["pages_done"] += 1
                if status == 404:                                   # product removed from the site
                    cur.execute("UPDATE brandsperfume_catalog SET stock = 'Not on site', updated_at = NOW() "
                                "WHERE rtrim(product_url, '/') = %s AND stock <> 'Not on site'", (url,))
                    st["not_on_site"] += cur.rowcount
                elif status != 200:
                    st["pages_failed"] += 1                          # kept as it was, tried again tomorrow
                elif not rows:
                    st["pages_without_data"] += 1
                for r in rows:
                    # same row as the full download (variant_id), else same link + same name
                    cur.execute("SELECT id, price_inc_tax, stock FROM brandsperfume_catalog WHERE variant_id = %s",
                                (r["variant_id"],))
                    old = cur.fetchall()
                    if not old:
                        cur.execute("SELECT id, price_inc_tax, stock FROM brandsperfume_catalog "
                                    "WHERE rtrim(product_url, '/') = %s AND name = %s", (url, r["name"]))
                        old = cur.fetchall()
                    for rid, op, ost in old:
                        if op is not None and r["price_inc_tax"] is not None and float(op) == float(r["price_inc_tax"]) \
                                and ost == r["stock"]:
                            continue                                 # nothing changed -> no write, no trigger
                        cur.execute("UPDATE brandsperfume_catalog SET price_inc_tax = %s, price = %s, stock = %s, "
                                    "updated_at = NOW() WHERE id = %s",
                                    (r["price_inc_tax"], r["price"], r["stock"], rid))
                        st["rows_updated"] += 1
                        st["price_changed"] += (op is None or r["price_inc_tax"] is None
                                                or float(op) != float(r["price_inc_tax"]))
                        st["stock_changed"] += ost != r["stock"]
                        if st["rows_updated"] <= 20:
                            br_log(f"[BR-QUICK] {r['name']}: {op} {ost} -> {r['price_inc_tax']} {r['stock']}")
                conn.commit()
                if st["pages_done"] % 200 == 0:
                    br_log(f"[BR-QUICK] {st['pages_done']}/{len(links)} pages, {st['rows_updated']} rows changed")
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        br_log(f"[BR-QUICK-DONE] {st}")


def br_quick_worker():
    try:
        br_quick_run()
    except Exception as ex:
        br_log(f"[BR-QUICK-ERROR] {ex}\n{traceback.format_exc()}")
        brq_state["last"] = {**(brq_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        brq_state["running"] = False
        br_lock.release()


def br_quick_start():
    """Start the quick check in the background. False when Brands Perfume is already busy (full download)."""
    if not br_lock.acquire(blocking=False):
        return False
    brq_state["running"] = True
    threading.Thread(target=br_quick_worker, daemon=True, name="br-quick").start()
    return True


def br_quick_scheduler(check_every=60):
    """Started by app.py: every minute -> at/after BR_QUICK_TIME India time, once a day (date in app_settings)."""
    br_log(f"[BR-QUICK] Scheduler started: linked Brands Perfume products re-read daily at {BR_QUICK_TIME} India time")
    while True:
        try:
            now = br_india_now()
            today = now.date().isoformat()
            if now.strftime("%H:%M") >= BR_QUICK_TIME:
                conn = br_conn()
                cur = conn.cursor()
                cur.execute("SELECT value FROM app_settings WHERE key = %s", (BR_QUICK_KEY,))
                row = cur.fetchone()
                if not row or row[0] != today:
                    if br_quick_start():
                        br_log(f"[BR-QUICK] {now:%H:%M} India - daily quick check started")
                        cur.execute("INSERT INTO app_settings (key, value, updated_by) VALUES (%s, %s, 'brandsperfume.py') "
                                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
                                    (BR_QUICK_KEY, today))
                        conn.commit()
                    else:
                        br_log("[BR-QUICK] Brands Perfume download busy - quick check tried again in a minute")
                conn.close()
        except Exception as ex:                                   # never stops the website
            br_log(f"[BR-QUICK] ERROR: {str(ex)[:200]} - trying again in {check_every}s")
        time.sleep(check_every)


@brandsperfume_bp.route("/brandsperfume-quick/run")
def br_quick_route():
    if not br_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not br_quick_start():
        return jsonify({"error": "Brands Perfume download / quick check already running"}), 409
    br_log("[BR-QUICK] Started by hand")
    return jsonify({"started": True, "check_status": "/brandsperfume-quick/status"}), 202


@brandsperfume_bp.route("/brandsperfume-quick/status")
def br_quick_status_route():
    return jsonify({"running": brq_state["running"], "daily_at_india_time": BR_QUICK_TIME, "last": brq_state["last"]})


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


# ---------------------------------------------------------------- check ONE product now (2026-11-05)
# User: prices on brandsperfume.com differ from ours (Fiamma 100ml site AED 189.42 / ours 144.90) and some products
# out of stock on the site show In stock here. Our rows are from the last full download (Run 2, every 14 days).
#   /brandsperfume/check?url=<product link>          (logged in) -> what the page says NOW vs our table:
#       ld_products = the page's hidden product data (JSON-LD: price / availability per offer, as the shop wrote it)
#       aed_on_page = AED amounts written in the page itself (what the shopper sees), parsed = what our reader takes,
#       in_our_table = our saved row(s) for that link
#   ... &save=1  -> the price + stock read NOW are saved for that link (the Competitor table follows at once)
def br_aed_on_page(html):
    """AED amounts in the page text (first 12) - e.g. ['189.42', '215.25', '25.83']."""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    return [m.group(1) for m in re.finditer(r"AED\s*([0-9][0-9,]*\.?[0-9]*)", re.sub(r"<[^>]+>", " ", text))][:12]


@brandsperfume_bp.route("/brandsperfume/check")
def br_check_route():
    url = (request.args.get("url") or "").strip().split("?")[0]
    if not url.startswith(BR_BASE):
        return jsonify({"error": f"give ?url= a {BR_BASE}/ae/en/product/... link"}), 400
    st, html = br_get(url)
    if st != 200:
        return jsonify({"url": url, "status": st, "error": "page not read"}), 502
    parsed = br_parse(url, html)
    lds = [{k: d.get(k) for k in ("@type", "name", "sku", "gtin13", "offers") if k in d}
           for d in br_ld_objects(html) if "Product" in str(d.get("@type"))]
    saved, ours = 0, []
    conn = br_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name, price_inc_tax, stock, updated_at FROM brandsperfume_catalog "
                    "WHERE rtrim(product_url, '/') = rtrim(%s, '/')", (url,))
        ours = [{"name": n, "price": float(p) if p is not None else None, "stock": s_, "updated": str(u)}
                for n, p, s_, u in cur.fetchall()]
        if request.args.get("save") == "1" and parsed:
            for r in parsed:                                       # same link + same name -> price / stock now
                cur.execute("UPDATE brandsperfume_catalog SET price_inc_tax = %s, price = %s, stock = %s, "
                            "updated_at = NOW() WHERE rtrim(product_url, '/') = rtrim(%s, '/') AND name = %s",
                            (r["price_inc_tax"], r["price"], r["stock"], url, r["name"]))
                saved += cur.rowcount
            conn.commit()
    finally:
        conn.close()
    br_log(f"[BR-CHECK] {url}: page AED {br_aed_on_page(html)[:3]} | read {[(r['price_inc_tax'], r['stock']) for r in parsed]} "
           f"| ours {[(o['price'], o['stock']) for o in ours]} | saved {saved}")
    return jsonify({"url": url, "aed_on_page": br_aed_on_page(html),
                    "parsed": [{k: r[k] for k in ("name", "price_inc_tax", "stock", "currency", "gtin")} for r in parsed],
                    "in_our_table": ours, "saved_now": saved, "ld_products": lds[:4]})


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
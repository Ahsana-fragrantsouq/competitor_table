"""
Daily competitor price check (runs on RENDER only - no PC needed)
=================================================================
Every day: take 50 of OUR products that are on fragrantsouq.com (competitor_table.our_link is filled),
open every competitor link they ALREADY have, read the current price + stock, and update the existing rows.

Rules (nothing else is changed):
  - never adds products, never creates / changes matches, links, titles or suggestions, never inserts catalog rows
  - never touches our price (uae_price) and never writes to Airtable (Automate is NOT triggered)
  - verified price/stock -> competitor_table.<shop>_price / <shop>_stock + the same row in the shop's own page
    (catalog table, e.g. Competitors -> Samawa), with "Updated" set to now
  - page shows "Out of stock" with no price (e.g. "Contact us for a price") -> stock = Out of stock, last price kept
  - page gone (404 / redirect / variant removed) -> <shop>_stock = FALSE, price kept, logged as not_found
  - blocked / error / price or stock not readable -> NOTHING changed, logged as failed
  - least price + suggested price are recalculated for the checked products only (same rules as app.py)
  - products are taken oldest-checked first, so every run continues where the last one stopped

Shops and how they are read from Render:
  Samawa, Amberiya       Shopify  /products/<handle>.js  (price + available per variant)
  French Fragrance,
  Branded Perfume,
  Essenzi                CS-Cart page (same reader as branded_perfume.py) - often blocked by Cloudflare
  Al Injaz, Niche Gall.  WooCommerce Store API ?slug=... (same reader as alinjaz.py / nichegallerie.py)
  V Perfumes, Ounass     product page Google data (JSON-LD) - Ounass often answers 429 to Render

Tables created on the first LIVE run (dry run creates nothing):
  price_check_runs       one row per run (counts + summary)
  price_check_progress   one row per product: when it was last checked -> rotation / resume
  price_check_log        one row per link checked: status, old -> new price / stock, error (kept 90 days)

DAILY TIME: set in THIS file -> AUTO_RUN_TIME_IST below (India time, 24-hour). No Render Cron Job needed.
  The web service checks the clock every minute and starts the run once a day at that time.
  If the service was restarted / deployed after that time, it runs as soon as it is back (once per day).

Run it:
  Automatic       :  every day at AUTO_RUN_TIME_IST (inside the web service)
  By hand         :  python price_check.py                      (50 products)
  Test (no writes):  python price_check.py --dry-run --limit 3
  One product    :  python price_check.py --product recXXXX
  In the browser :  /price-check/run?secret=XXX&dry=1&limit=3   (also &product=recXXX)
                    /price-check/status

Env vars: FF_DATABASE_URL (required), RUN_SECRET (for the browser link),
          PRICE_CHECK_LIMIT (default 50), PRICE_CHECK_DELAY (seconds between pages, default 1.5),
          PRICE_CHECK_KEEP_DAYS (log history, default 90)
"""

import os
import re
import json
import time
import uuid
import argparse
import threading
import traceback
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs

import requests
import psycopg2
from flask import Blueprint, request, jsonify

from branded_perfume import bp_parse          # CS-Cart page reader (French Fragrance / Branded Perfume / Essenzi)
import alinjaz                                # aj_item() + AJ_API (WooCommerce)
import nichegallerie                          # ng_item() + NG_API + NG_CURRENCY (WooCommerce, AED)

price_check_bp = Blueprint("price_check", __name__)

# ================================================================ DAILY TIME  <- change here
AUTO_RUN_TIME_IST = "17:00"       # India time, 24-hour: "17:00" = 5:00 PM, "08:30" = 8:30 AM, "21:00" = 9:00 PM
AUTO_RUN_ENABLED = False          # True = run automatically every day. Keep False until the dry run test is OK

AUTO_RUN_DAYS = [0, 1, 2, 3, 4, 5, 6]   # 0 = Monday ... 6 = Sunday. Monday-Friday only: [0, 1, 2, 3, 4]
# ================================================================
IST = timezone(timedelta(hours=5, minutes=30))

PC_LIMIT = int(os.environ.get("PRICE_CHECK_LIMIT", "50"))
PC_DELAY = float(os.environ.get("PRICE_CHECK_DELAY", "1.5"))
PC_KEEP_DAYS = int(os.environ.get("PRICE_CHECK_KEEP_DAYS", "90"))
PC_BLOCK_SKIP = 3                 # a shop blocked 3 times in a row -> skipped for the rest of this run
PC_LOCK_KEY = 2026101201          # Postgres lock: only ONE price check at a time (cron job + web service)
PC_RETRY_WAITS = [10, 30]         # waits when a site answers "busy" (429 / 5xx)
PC_MAX_PRICE = 100000             # a price this high is a wrong number -> not saved
SUGGEST_DECIMALS = 0              # same rounding as app.py (97.5 -> 98)
PC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# (prefix in competitor_table, label, how to read the page). Same labels / order as app.py LEAST_ORDER.
SHOPS = [
    ("samawa", "Samawa", "shopify"),
    ("ff", "French Fragrance", "cscart"),
    ("bp", "Branded Perfume", "cscart"),
    ("es", "Essenzi", "cscart"),
    ("vp", "V Perfumes", "jsonld"),
    ("aj", "Al Injaz", "woo_aj"),
    ("on", "Ounass", "jsonld"),
    ("am", "Amberiya", "shopify"),
    ("ng", "Niche Gallerie", "woo_ng"),
]
LABEL = {p: label for p, label, _ in SHOPS}

# Shop catalog table that holds the same link: (table, url column, VAT-incl price col, price without VAT col,
# stock col, extra SET). Updated too, otherwise the next match run would copy the OLD values back.
CATALOG = {
    "samawa": ("samawa_catalog", "product_url", "price", None, "stock", ""),
    "ff": ("french_fragrance_catalog", "product_url", "price_inc_tax", "price", "stock", ""),
    "bp": ("branded_perfume_catalog", "bp_url", "bp_price_inc_tax", "bp_price", "bp_stock", ", checked_at = NOW()"),
    "es": ("essenzi_catalog", "es_url", "es_price_inc_tax", "es_price", "es_stock", ", checked_at = NOW()"),
    "vp": ("vperfumes_catalog", "product_url", "price_inc_tax", "price", "stock", ""),
    "aj": ("alinjaz_catalog", "product_url", "price_inc_tax", "price", "stock", ""),
    "on": ("ounass_catalog", "product_url", "price_inc_tax", "price", "stock", ""),
    "am": ("amberiya_catalog", "product_url", "price_inc_tax", "price", "stock", ""),
    "ng": ("nichegallerie_catalog", "product_url", "price_inc_tax", "price", "stock", ""),
}

PC_CREATE = """
CREATE TABLE IF NOT EXISTS price_check_runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TIMESTAMP DEFAULT NOW(),
    finished_at  TIMESTAMP,
    status       TEXT,                     -- running / done / error
    trigger      TEXT,                     -- cron / browser / manual
    products     INT DEFAULT 0,
    links        INT DEFAULT 0,
    verified     INT DEFAULT 0,
    changed      INT DEFAULT 0,
    failed       INT DEFAULT 0,
    summary      JSONB
);
CREATE TABLE IF NOT EXISTS price_check_progress (
    fi_record_id     TEXT PRIMARY KEY,     -- competitor_table.fi_record_id
    last_checked_at  TIMESTAMP,
    last_run_id      TEXT,
    links_checked    INT,
    links_failed     INT
);
CREATE TABLE IF NOT EXISTS price_check_log (
    id           BIGSERIAL PRIMARY KEY,
    run_id       TEXT,
    fi_record_id TEXT,
    shop         TEXT,
    url          TEXT,
    status       TEXT,                     -- ok / not_found / blocked / http_error / no_price / no_stock ...
    old_price    NUMERIC(10,2),
    new_price    NUMERIC(10,2),
    old_stock    BOOLEAN,
    new_stock    BOOLEAN,
    error        TEXT,
    checked_at   TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pcl_run ON price_check_log (run_id);
CREATE INDEX IF NOT EXISTS idx_pcl_fi ON price_check_log (fi_record_id, checked_at);
"""

pc_lock = threading.Lock()
pc_state = {"running": False, "last": None}


def log(*args):
    print(*args, flush=True)


def pc_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    log("[PC-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


def table_exists(cur, name):
    cur.execute("SELECT to_regclass(%s)", (name,))
    return cur.fetchone()[0] is not None


# ---------------------------------------------------------------- small helpers
def to_num(v):
    """'1,216.50' / 450 / Decimal -> 1216.5 (None if no number)."""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(v if v is not None else ""))
    return round(float(m.group(0).replace(",", "")), 2) if m else None


def parse_size(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", str(text or "").lower())
    return float(m.group(1)) if m else None


def fail(status, error):
    return {"status": status, "error": error}


def pc_get(session, url, params=None):
    """GET with retries on 429 / 5xx. Returns (response, status, error).
    status: ok / not_found / blocked / http_error / network_error"""
    last = None
    for attempt, wait in enumerate([0] + PC_RETRY_WAITS):
        if wait:
            log(f"   [PC-HTTP] busy ({last}) -> retry {attempt}/{len(PC_RETRY_WAITS)} in {wait}s")
            time.sleep(wait)
        try:
            r = session.get(url, params=params, headers=PC_HEADERS, timeout=40, allow_redirects=True)
        except requests.RequestException as ex:
            last = f"network: {str(ex)[:120]}"
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            continue
        head = r.text[:4000].lower()
        if (r.status_code in (401, 403) or "sgcaptcha" in r.url or "sgcaptcha" in head
                or "just a moment" in head or "cf-challenge" in head or "verifying you are human" in head):
            return r, "blocked", f"bot check / HTTP {r.status_code}"
        if r.status_code == 404:
            return r, "not_found", "HTTP 404"
        if r.status_code != 200:
            return r, "http_error", f"HTTP {r.status_code}"
        return r, "ok", None
    if last == "HTTP 429":
        return None, "blocked", "HTTP 429 (site refuses this server)"
    return None, "network_error" if (last or "").startswith("network") else "http_error", last


# ---------------------------------------------------------------- page readers
# Each returns {"status": "ok", "price": VAT-incl, "price_ex": without VAT, "stock": True/False}
# or {"status": <reason>, "error": "..."} when the price / stock could not be verified.
def read_shopify(session, url, size_hint):
    """Samawa / Amberiya: /products/<handle>.js -> the variant in the link (?variant=), or the only one, or by size."""
    u = urlparse(url)
    handle = u.path.rstrip("/").split("/products/")[-1]
    vid = parse_qs(u.query).get("variant", [None])[0]
    r, st, err = pc_get(session, f"{u.scheme}://{u.netloc}/products/{handle}.js")
    if st != "ok":
        return fail(st, err)
    try:
        p = r.json()
    except ValueError:
        return fail("error", "answer is not JSON")
    variants = p.get("variants") or []
    if vid:
        v = next((x for x in variants if str(x.get("id")) == str(vid)), None)
        if v is None:
            return fail("not_found", f"variant {vid} no longer exists")
    elif len(variants) == 1:
        v = variants[0]
    else:
        sized = [x for x in variants if size_hint and parse_size(x.get("title") or x.get("public_title"))
                 and abs(parse_size(x.get("title") or x.get("public_title")) - size_hint) <= 0.5]
        if len(sized) != 1:
            return fail("unclear_variant", f"{len(variants)} sizes, cannot tell which one is ours")
        v = sized[0]
    if v.get("price") in (None, ""):
        return fail("no_price", "variant has no price")
    price = round(int(v["price"]) / 100, 2)               # Shopify .js price is in fils / cents
    if v.get("available") is None:
        return fail("no_stock", "variant has no 'available' value")
    return {"status": "ok", "price": price, "price_ex": round(price / 1.05, 2), "stock": bool(v["available"])}


def read_cscart(session, url, size_hint):
    """French Fragrance / Branded Perfume / Essenzi product page (same reader as branded_perfume.py)."""
    r, st, err = pc_get(session, url)
    if st != "ok":
        return fail(st, err)
    if urlparse(r.url).path.rstrip("/") != urlparse(url).path.rstrip("/"):
        return fail("not_found", f"redirected to {r.url[:120]}")      # CS-Cart sends missing products elsewhere
    d = bp_parse(r.text)
    ex, inc = d.get("price"), d.get("price_inc_tax")
    if not inc and ex:
        inc = round(ex * 1.05, 2)                                     # page shows price without 5% VAT only
    # "Out of stock" shown on the page (e.g. under "Contact us for a price")
    page_text = re.sub(r"<[^>]+>", " ", r.text)
    out_on_page = "ty-qty-out-of-stock" in r.text or bool(re.search(r"\bout of stock\b", page_text, re.I))
    if not inc:
        if out_on_page:
            log("      [PC-CSCART] no price on the page but it says 'Out of stock' -> stock = out, last price kept")
            return {"status": "ok", "price": None, "price_ex": None, "stock": False}
        return fail("no_price", "no price on the page")
    return {"status": "ok", "price": inc, "price_ex": ex or round(inc / 1.05, 2), "stock": d["stock"] == "In stock"}


def find_product_ld(html):
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else (data.get("@graph", [data]) if isinstance(data, dict) else [])
        for it in items:
            if isinstance(it, dict) and "product" in str(it.get("@type", "")).lower():
                return it
    return None


def read_jsonld(session, url, size_hint):
    """V Perfumes / Ounass product page: Google product data (JSON-LD), else <meta product:price:amount>."""
    r, st, err = pc_get(session, url)
    if st != "ok":
        return fail(st, err)
    html = r.text
    jd = find_product_ld(html) or {}
    offers = jd.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    inner = offers.get("offers") or {}
    if isinstance(inner, list):
        inner = inner[0] if inner else {}
    price = to_num(offers.get("price") or offers.get("lowPrice") or inner.get("price"))
    currency = str(offers.get("priceCurrency") or inner.get("priceCurrency") or "").upper()
    if price is None:
        m = re.search(r'property="product:price:amount"\s+content="([\d.,]+)"', html)
        price = to_num(m.group(1)) if m else None
        m = re.search(r'property="product:price:currency"\s+content="([A-Za-z]{3})"', html)
        currency = currency or (m.group(1).upper() if m else "")
    avail = str(offers.get("availability") or inner.get("availability") or "").lower()
    if not avail:
        m = re.search(r'property="product:availability"\s+content="([^"]+)"', html)
        avail = m.group(1).lower() if m else ""
    if "outofstock" in avail.replace(" ", "") or "soldout" in avail.replace(" ", ""):
        stock = False
    elif "instock" in avail.replace(" ", ""):
        stock = True
    else:
        stock = None
    if price is None:
        if stock is False:
            log("      [PC-JSONLD] no price but product data says out of stock -> stock = out, last price kept")
            return {"status": "ok", "price": None, "price_ex": None, "stock": False}
        return fail("no_price", "no product price data on the page")
    if currency and currency != "AED":
        return fail("wrong_currency", f"price is in {currency}, not AED")
    if stock is None:
        return fail("no_stock", "stock not in the product data")
    return {"status": "ok", "price": price, "price_ex": round(price / 1.05, 2), "stock": stock}


def read_woo(session, url, mod):
    """Al Injaz / Niche Gallerie: WooCommerce Store API ?slug=<last part of the link>."""
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    if mod is nichegallerie:
        api, item_fn = f"{nichegallerie.NG_API}?{nichegallerie.NG_CURRENCY}", nichegallerie.ng_item   # AED prices
    else:
        api, item_fn = alinjaz.AJ_API, alinjaz.aj_item
    r, st, err = pc_get(session, api, params={"slug": slug})
    if st != "ok":
        return fail(st, err)
    try:
        data = r.json()
    except ValueError:
        return fail("blocked", "answer is not JSON (bot check?)")
    if not isinstance(data, list):
        return fail("error", f"unexpected answer: {str(data)[:120]}")
    if not data:
        return fail("not_found", f"slug '{slug}' not in the product list")
    raw = next((x for x in data if (x.get("permalink") or "").rstrip("/") == url.rstrip("/")), data[0])
    it = item_fn(raw)
    if not it["price_inc_tax"]:
        if it["stock"] != "In stock":
            log("      [PC-WOO] no price but product is out of stock -> stock = out, last price kept")
            return {"status": "ok", "price": None, "price_ex": None, "stock": False}
        return fail("no_price", "no price (or price not in AED)")
    return {"status": "ok", "price": it["price_inc_tax"], "price_ex": it["price"], "stock": it["stock"] == "In stock"}


def read_link(session, kind, url, size_hint):
    try:
        if kind == "shopify":
            return read_shopify(session, url, size_hint)
        if kind == "cscart":
            return read_cscart(session, url, size_hint)
        if kind == "jsonld":
            return read_jsonld(session, url, size_hint)
        if kind == "woo_aj":
            return read_woo(session, url, alinjaz)
        if kind == "woo_ng":
            return read_woo(session, url, nichegallerie)
        return fail("error", f"no reader for {kind}")
    except Exception as ex:                                   # one bad page never stops the run
        return fail("error", f"{type(ex).__name__}: {str(ex)[:150]}")


# ---------------------------------------------------------------- writing (live run only)
def write_result(cur, fid, p, url, res, catalogs_ok):
    """Update the EXISTING competitor_table row + the shop's own page (catalog row). Only for the same link.
    price None = page shows no price but says out of stock -> only stock changes, last price kept."""
    if res["status"] == "ok" and res["price"] is not None:
        cur.execute(f"UPDATE competitor_table SET {p}_price = %s, {p}_stock = %s, updated_at = NOW() "
                    f"WHERE fi_record_id = %s AND {p}_link = %s", (res["price"], res["stock"], fid, url))
    else:                                                     # out of stock without price / not_found -> stock only
        cur.execute(f"UPDATE competitor_table SET {p}_stock = FALSE, updated_at = NOW() "
                    f"WHERE fi_record_id = %s AND {p}_link = %s", (fid, url))
    ct_rows = cur.rowcount
    table, url_col, inc_col, ex_col, stock_col, extra = CATALOG[p]
    extra += ", updated_at = NOW()"                           # shop page shows "Updated <today>"
    cat_rows = 0
    if table in catalogs_ok:
        if res["status"] == "ok" and res["price"] is None:
            cur.execute(f"UPDATE {table} SET {stock_col} = 'Out of stock'{extra} WHERE {url_col} = %s", (url,))
        elif res["status"] == "ok":
            sets, params = [f"{inc_col} = %s"], [res["price"]]
            if ex_col:
                sets.append(f"{ex_col} = %s")
                params.append(res["price_ex"])
            sets.append(f"{stock_col} = %s")
            params.append("In stock" if res["stock"] else "Out of stock")
            cur.execute(f"UPDATE {table} SET {', '.join(sets)}{extra} WHERE {url_col} = %s", params + [url])
        else:
            cur.execute(f"UPDATE {table} SET {stock_col} = 'Out of stock'{extra} WHERE {url_col} = %s "
                        f"AND COALESCE({stock_col}, '') <> 'Not on site'", (url,))
        cat_rows = cur.rowcount
    log(f"      [PC-SAVE] competitor_table rows {ct_rows} | {table} rows {cat_rows}")
    return ct_rows


def get_discount_rate(cur):
    """Settings -> Auto discount rate (same table as competitors_page.py). None = not saved."""
    if not table_exists(cur, "app_settings"):
        return None
    cur.execute("SELECT value FROM app_settings WHERE key = 'discount_rate'")
    row = cur.fetchone()
    try:
        return float(row[0]) if row and row[0] not in (None, "") else None
    except ValueError:
        return None


def recalc_least(cur, fid, rate):
    """Least price / least priced website / suggested price for ONE product - same rules as app.py least_for().
    Never writes to Airtable and never changes uae_price."""
    cols = ", ".join(f"{p}_price, {p}_stock, {p}_link" for p, _, _ in SHOPS)
    cur.execute(f"SELECT uae_price, {cols} FROM competitor_table WHERE fi_record_id = %s", (fid,))
    row = cur.fetchone()
    if not row:
        return
    offers = [("Fragrant Souq", float(row[0]))] if row[0] and row[0] > 0 else []
    comp = []
    for i, (p, label, _) in enumerate(SHOPS):
        price, stock, link = row[1 + i * 3: 4 + i * 3]
        if stock and link and price and price > 0:
            comp.append((label, float(price)))
    offers += comp
    least = min((pr for _, pr in offers), default=None)
    sites = ", ".join(s for s, pr in offers if least is not None and abs(pr - least) < 0.005) or None
    if rate is None:
        cur.execute("UPDATE competitor_table SET least_price = %s, least_priced_website = %s WHERE fi_record_id = %s",
                    (least, sites, fid))
        log(f"      [PC-LEAST] {least} ({sites}) | suggested not changed (no Auto discount rate saved)")
        return
    best = min((pr for _, pr in comp), default=None)
    suggested = (float(Decimal(str(best * (1 - rate / 100))).quantize(Decimal(1).scaleb(-SUGGEST_DECIMALS),
                                                                      rounding=ROUND_HALF_UP)) if best else None)
    cur.execute("UPDATE competitor_table SET least_price = %s, least_priced_website = %s, suggested_price = %s "
                "WHERE fi_record_id = %s", (least, sites, suggested, fid))
    log(f"      [PC-LEAST] {least} ({sites}) | suggested {suggested} ({rate}% below {best})")


# ---------------------------------------------------------------- choosing the products
def select_products(cur, limit, product, have_progress):
    """Our products on fragrantsouq.com with at least one competitor link. Oldest-checked first (never checked first)."""
    sel = ", ".join(f"c.{p}_link, c.{p}_price, c.{p}_stock, c.{p}_title" for p, _, _ in SHOPS)
    any_link = " OR ".join(f"(c.{p}_link IS NOT NULL AND c.{p}_link <> '')" for p, _, _ in SHOPS)
    join = "LEFT JOIN price_check_progress pr ON pr.fi_record_id = c.fi_record_id" if have_progress else ""
    order = "pr.last_checked_at ASC NULLS FIRST, c.id" if have_progress else "c.id"
    where = f"c.our_link IS NOT NULL AND c.our_link <> '' AND ({any_link})"
    params = []
    if product:
        where += " AND c.fi_record_id = %s"
        params.append(product)
    cur.execute(f"SELECT COUNT(*) FROM competitor_table c WHERE {where}", params)
    eligible = cur.fetchone()[0]
    cur.execute(f"SELECT c.fi_record_id, c.product_name, {sel} FROM competitor_table c {join} "
                f"WHERE {where} ORDER BY {order} LIMIT %s", params + [limit])
    out = []
    for row in cur.fetchall():
        item = {"fi": row[0], "name": row[1] or row[0], "links": []}
        for i, (p, label, kind) in enumerate(SHOPS):
            link, price, stock, title = row[2 + i * 4: 6 + i * 4]
            if link:
                item["links"].append({"p": p, "kind": kind, "url": link,
                                      "old_price": float(price) if price is not None else None,
                                      "old_stock": stock, "title": title})
        out.append(item)
    log(f"[PC] {eligible} products on fragrantsouq.com with competitor links | {len(out)} taken in this run")
    return out, eligible


# ---------------------------------------------------------------- the run
def pc_run(limit=PC_LIMIT, dry_run=False, product=None, trigger="manual"):
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    mode = "DRY RUN (nothing is saved)" if dry_run else "LIVE"
    log(f"\n[PC] ===== Price check {run_id} | {mode} | limit {limit} | product {product or '-'} | {trigger} =====")
    st = {"run_id": run_id, "dry_run": dry_run, "status": "running", "trigger": trigger, "products": 0, "links": 0,
          "verified": 0, "changed": 0, "failed": 0, "gone": 0, "shops": {}, "preview": []}
    pc_state["last"] = st
    started = time.time()

    conn = pc_conn()
    cur = conn.cursor()
    cur.execute("SELECT pg_try_advisory_lock(%s)", (PC_LOCK_KEY,))
    if not cur.fetchone()[0]:
        log("[PC] Another price check is running (cron job or web service) -> stopping")
        st["status"] = "busy"
        cur.close()
        conn.close()
        return st
    try:
        if not dry_run:
            log("[PC] Creating price check tables (if not exists)")
            cur.execute(PC_CREATE)
            cur.execute("INSERT INTO price_check_runs (run_id, status, trigger) VALUES (%s, 'running', %s)",
                        (run_id, trigger))
            conn.commit()
        have_progress = table_exists(cur, "price_check_progress")
        catalogs_ok = {t for t, *_ in CATALOG.values() if table_exists(cur, t)}
        log(f"[PC] Catalog tables found: {sorted(catalogs_ok)}")
        rate = get_discount_rate(cur)
        log(f"[PC] Auto discount rate: {rate if rate is not None else 'NOT SET (suggested price left as it is)'}")
        products, st["eligible"] = select_products(cur, limit, product, have_progress)
        conn.commit()

        session = requests.Session()
        blocked_in_row = {}
        for n, prod in enumerate(products, 1):
            fid = prod["fi"]
            log(f"\n[PC] {n}/{len(products)} {prod['name'][:70]} ({fid}) | {len(prod['links'])} competitor links")
            size_hint = parse_size(prod["name"])
            checked, failed, changed_here = 0, 0, False
            for ln in prod["links"]:
                p, url = ln["p"], ln["url"]
                shop = st["shops"].setdefault(LABEL[p], {"ok": 0, "changed": 0, "not_found": 0, "failed": 0,
                                                         "blocked": 0, "skipped": 0})
                if blocked_in_row.get(p, 0) >= PC_BLOCK_SKIP:
                    res = fail("skipped_blocked", f"{LABEL[p]} blocked {PC_BLOCK_SKIP}x in a row in this run")
                else:
                    res = read_link(session, ln["kind"], url, parse_size(ln["title"]) or size_hint)
                    time.sleep(PC_DELAY)
                if res["status"] == "ok" and res["price"] is not None and not (0 < res["price"] < PC_MAX_PRICE):
                    res = fail("bad_price", f"price {res['price']} is not a real price")
                status = res["status"]
                blocked_in_row[p] = blocked_in_row.get(p, 0) + 1 if status == "blocked" else (
                    blocked_in_row.get(p, 0) if status == "skipped_blocked" else 0)

                checked += 1
                st["links"] += 1
                new_price = res.get("price") if status == "ok" else None
                new_stock = res.get("stock") if status == "ok" else (False if status == "not_found" else None)
                diff = False
                if status == "ok":
                    st["verified"] += 1
                    shop["ok"] += 1
                    price_changed = new_price is not None and (ln["old_price"] is None
                                                               or abs(new_price - ln["old_price"]) >= 0.005)
                    diff = price_changed or bool(ln["old_stock"]) != bool(new_stock)
                elif status == "not_found":
                    st["gone"] += 1
                    shop["not_found"] += 1
                    diff = bool(ln["old_stock"])
                else:
                    failed += 1
                    st["failed"] += 1
                    shop["skipped" if status == "skipped_blocked" else ("blocked" if status == "blocked" else "failed")] += 1
                if diff:
                    st["changed"] += 1
                    shop["changed"] += 1
                log(f"   [PC-{LABEL[p]}] {status.upper()} | AED {ln['old_price']} -> {new_price if new_price is not None else 'kept'} | "
                    f"stock {ln['old_stock']} -> {new_stock if new_stock is not None else 'kept'}"
                    + (f" | {res.get('error')}" if res.get("error") else "") + f" | {url[:90]}")
                if len(st["preview"]) < 200:
                    st["preview"].append({"fi": fid, "shop": LABEL[p], "status": status, "old_price": ln["old_price"],
                                          "new_price": new_price, "old_stock": ln["old_stock"], "new_stock": new_stock,
                                          "error": res.get("error"), "url": url})
                if dry_run:
                    continue
                # each link in its own savepoint: one bad write never loses the other results
                cur.execute("SAVEPOINT pc_link")
                try:
                    if status in ("ok", "not_found"):
                        if write_result(cur, fid, p, url, res, catalogs_ok) and diff:
                            changed_here = True
                    cur.execute("""INSERT INTO price_check_log (run_id, fi_record_id, shop, url, status, old_price,
                                   new_price, old_stock, new_stock, error) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                                (run_id, fid, LABEL[p], url, status, ln["old_price"], new_price, ln["old_stock"],
                                 new_stock, (res.get("error") or "")[:500] or None))
                    cur.execute("RELEASE SAVEPOINT pc_link")
                except Exception as ex:
                    cur.execute("ROLLBACK TO SAVEPOINT pc_link")
                    log(f"   [PC-SAVE-ERROR] {LABEL[p]}: {ex} -> nothing changed for this link")
            st["products"] += 1
            if dry_run:
                continue
            if changed_here:
                recalc_least(cur, fid, rate)
            cur.execute("""INSERT INTO price_check_progress (fi_record_id, last_checked_at, last_run_id, links_checked,
                           links_failed) VALUES (%s, NOW(), %s, %s, %s)
                           ON CONFLICT (fi_record_id) DO UPDATE SET last_checked_at = NOW(),
                           last_run_id = EXCLUDED.last_run_id, links_checked = EXCLUDED.links_checked,
                           links_failed = EXCLUDED.links_failed""", (fid, run_id, checked, failed))
            conn.commit()                                     # saved after every product -> safe to stop anytime
            log(f"[PC-PROGRESS] product saved | so far {st['verified']} verified, {st['changed']} changed, "
                f"{st['failed']} failed")

        st["status"] = "done"
    except Exception as ex:
        conn.rollback()
        st["status"] = "error"
        st["error"] = str(ex)
        log(f"[PC-ERROR] {ex}\n{traceback.format_exc()}")
    finally:
        st["seconds"] = round(time.time() - started, 1)
        if not dry_run:
            try:
                summary = {k: v for k, v in st.items() if k != "preview"}
                cur.execute("""UPDATE price_check_runs SET finished_at = NOW(), status = %s, products = %s, links = %s,
                               verified = %s, changed = %s, failed = %s, summary = %s WHERE run_id = %s""",
                            (st["status"], st["products"], st["links"], st["verified"], st["changed"], st["failed"],
                             json.dumps(summary, default=str), run_id))
                cur.execute("DELETE FROM price_check_log WHERE checked_at < NOW() - %s * INTERVAL '1 day'",
                            (PC_KEEP_DAYS,))
                log(f"[PC] {cur.rowcount} log rows older than {PC_KEEP_DAYS} days removed")
                conn.commit()
            except Exception as ex:
                conn.rollback()
                log(f"[PC] Could not save the run summary: {ex}")
        cur.execute("SELECT pg_advisory_unlock(%s)", (PC_LOCK_KEY,))
        cur.close()
        conn.close()
        log(f"[PC-DONE] {mode} | {st['products']} products, {st['links']} links | verified {st['verified']} | "
            f"changed {st['changed']} | gone {st['gone']} | failed {st['failed']} | {st['seconds']}s")
        log(f"[PC-DONE] Per shop: {json.dumps(st['shops'])}")
    return st


# ---------------------------------------------------------------- browser links (web service)
def pc_worker(limit, dry_run, product):
    try:
        pc_run(limit, dry_run, product, trigger="browser")
    except Exception as ex:
        log(f"[PC-ERROR] {ex}\n{traceback.format_exc()}")
        pc_state["last"] = {**(pc_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        pc_state["running"] = False
        pc_lock.release()


@price_check_bp.route("/price-check/run")
def pc_run_route():
    """/price-check/run?secret=XXX&dry=1&limit=3&product=recXXX  (dry=1 -> nothing saved)"""
    secret = os.environ.get("RUN_SECRET", "")
    if secret and request.args.get("secret") != secret:
        log("[PC-RUN] Unauthorized attempt")
        return jsonify({"error": "unauthorized"}), 401
    try:
        limit = max(1, min(int(request.args.get("limit", PC_LIMIT)), 500))
    except ValueError:
        limit = PC_LIMIT
    dry = request.args.get("dry") == "1"
    product = (request.args.get("product") or "").strip() or None
    if not pc_lock.acquire(blocking=False):
        return jsonify({"error": "a price check is already running"}), 409
    pc_state["running"] = True
    log(f"[PC-RUN] Started from browser: dry={dry} limit={limit} product={product}")
    threading.Thread(target=pc_worker, args=(limit, dry, product), daemon=True).start()
    return jsonify({"started": True, "dry_run": dry, "limit": limit, "product": product,
                    "check_status": "/price-check/status"}), 202


@price_check_bp.route("/price-check/status")
def pc_status_route():
    """Current / last run in this web service + the last 7 saved runs (cron job included)."""
    runs = []
    try:
        conn = pc_conn()
        cur = conn.cursor()
        if table_exists(cur, "price_check_runs"):
            cur.execute("SELECT run_id, trigger, status, started_at, finished_at, products, links, verified, changed, "
                        "failed FROM price_check_runs ORDER BY started_at DESC LIMIT 7")
            keys = ["run_id", "trigger", "status", "started_at", "finished_at", "products", "links", "verified",
                    "changed", "failed"]
            runs = [{k: (str(v) if k.endswith("_at") and v else v) for k, v in zip(keys, r)} for r in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception as ex:
        log(f"[PC-STATUS] Could not read saved runs: {ex}")
    return jsonify({"running": pc_state["running"], "last": pc_state["last"], "saved_runs": runs})


# ---------------------------------------------------------------- daily timer inside the web service
CLAIM_SQL = """
CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_by  TEXT,
    updated_at  TIMESTAMP DEFAULT NOW()
);
INSERT INTO app_settings (key, value, updated_by) VALUES ('price_check_last_day', %(day)s, 'timer')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = 'timer', updated_at = NOW()
WHERE app_settings.value IS DISTINCT FROM EXCLUDED.value
RETURNING key;
"""


def claim_today(day):
    """Mark today's automatic run as taken. True only for the FIRST caller of the day
    (several gunicorn workers / a restart can never start it twice)."""
    conn = pc_conn()
    cur = conn.cursor()
    try:
        cur.execute(CLAIM_SQL, {"day": day})
        got = cur.fetchone() is not None
        conn.commit()
        return got
    finally:
        cur.close()
        conn.close()


def timer_loop():
    hh, mm = (int(x) for x in AUTO_RUN_TIME_IST.split(":"))
    log(f"[PC-TIMER] Started: daily price check at {AUTO_RUN_TIME_IST} IST on days {AUTO_RUN_DAYS} (0 = Monday)")
    while True:
        try:
            now = datetime.now(IST)
            due = now.weekday() in AUTO_RUN_DAYS and (now.hour, now.minute) >= (hh, mm)
            if due and pc_lock.acquire(blocking=False):
                try:
                    day = now.strftime("%Y-%m-%d")
                    if claim_today(day):
                        log(f"[PC-TIMER] {now:%Y-%m-%d %H:%M} IST -> starting today's price check")
                        pc_state["running"] = True
                        pc_run(PC_LIMIT, False, None, trigger="timer")
                    # else: already done today (by this or another worker) -> wait for tomorrow
                finally:
                    pc_state["running"] = False
                    pc_lock.release()
        except Exception as ex:
            log(f"[PC-TIMER] Error (will try again in 1 minute): {ex}")
        time.sleep(60)


@price_check_bp.record_once
def start_timer(state):
    """Runs once when app.py registers this blueprint -> starts the daily timer in the web service only
    (python price_check.py by hand does NOT start it)."""
    if not AUTO_RUN_ENABLED:
        log("[PC-TIMER] AUTO_RUN_ENABLED = False -> no automatic daily run")
        return
    threading.Thread(target=timer_loop, daemon=True, name="price-check-timer").start()


# ---------------------------------------------------------------- command line (by hand)
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Daily competitor price check")
    ap.add_argument("--dry-run", action="store_true", help="read pages and print, save nothing")
    ap.add_argument("--limit", type=int, default=PC_LIMIT, help="number of products (default 50)")
    ap.add_argument("--product", default="", help="check one product (fi_record_id, e.g. recXXXX)")
    a = ap.parse_args()
    result = pc_run(a.limit, a.dry_run, a.product.strip() or None, trigger="manual")
    raise SystemExit(0 if result["status"] in ("done", "busy") else 1)
"""
Branded Perfume catalog (brandedperfume.com -> Postgres french_fragrance_db, table branded_perfume_catalog)
Plugged into app.py as a Blueprint.

Branded Perfume and French Fragrance are the SAME company with the SAME products and the same
website system (CS-Cart). So instead of searching, we take every French Fragrance product and
open the same page on brandedperfume.com:
    https://frenchfragrance.com/perfumes/xyz-100ml/  ->  https://brandedperfume.com/perfumes/xyz-100ml/

Routes (open in the browser):
  GET /branded-perfume/test?secret=XXX      check 3 products first: can Render open the site? are price/stock read?
  GET /branded-perfume/load?secret=XXX      STEP 1: copy all French Fragrance products into branded_perfume_catalog
  GET /branded-perfume/run?secret=XXX       STEP 2: open every Branded Perfume page -> price, inc-tax price, stock
      &resume=1                             only products not checked yet (use after a restart / redeploy)
  GET /branded-perfume/status               progress of step 1 / step 2

Env vars (already set on Render):
  FF_DATABASE_URL = Postgres URL ending in /french_fragrance_db
  RUN_SECRET      = same secret as the other /run URLs
"""

import os
import re
import json
import html as htmllib
import time
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
import psycopg2
from psycopg2.extras import execute_values
from flask import Blueprint, request, jsonify

branded_perfume_bp = Blueprint("branded_perfume", __name__)

BP_DOMAIN = "brandedperfume.com"
FF_DOMAIN = "frenchfragrance.com"
BP_WORKERS = int(os.environ.get("BP_WORKERS", "3"))       # pages opened at the same time (be gentle)
BP_DELAY = float(os.environ.get("BP_DELAY", "0.5"))       # seconds each worker waits between pages
BP_SAVE_EVERY = 100                                       # save to Postgres every 100 pages
BP_RETRY_WAITS = [10, 30, 90]                             # waits when the site says "busy" (429 / 5xx)
BP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

bp_lock = threading.Lock()                                 # only one step 1 / step 2 at a time
bp_state = {"running": False, "last": None}


def bp_log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- database
BP_CREATE = """
CREATE TABLE IF NOT EXISTS branded_perfume_catalog (
    id                SERIAL PRIMARY KEY,
    ff_id             INTEGER UNIQUE NOT NULL,     -- id of the product in french_fragrance_catalog
    ff_url            TEXT,                        -- French Fragrance link (source)
    gtin              TEXT,
    name              TEXT,
    volume            TEXT,
    bp_url            TEXT,                        -- same page on brandedperfume.com
    bp_price          NUMERIC(10,2),               -- price without VAT
    bp_price_inc_tax  NUMERIC(10,2),               -- price with 5% VAT (the price customers pay)
    bp_stock          TEXT,                        -- In stock / Out of stock / Not on site
    bp_status         TEXT,                        -- ok / not_found / blocked / error ... (why)
    checked_at        TIMESTAMP,                   -- when the Branded Perfume page was last opened
    updated_at        TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bp_gtin ON branded_perfume_catalog (gtin);
ALTER TABLE branded_perfume_catalog ADD COLUMN IF NOT EXISTS bp_title TEXT;   -- product name on Branded Perfume
ALTER TABLE branded_perfume_catalog ADD COLUMN IF NOT EXISTS bp_size TEXT;    -- e.g. "100 ml" read from that name
"""

# STEP 1: copy French Fragrance products (new -> insert, existing -> update details, keep BP results)
BP_LOAD = f"""
INSERT INTO branded_perfume_catalog (ff_id, ff_url, gtin, name, volume, bp_url)
SELECT id, product_url, gtin, name, volume, REPLACE(product_url, '{FF_DOMAIN}', '{BP_DOMAIN}')
FROM french_fragrance_catalog
WHERE product_url IS NOT NULL
ON CONFLICT (ff_id) DO UPDATE SET
    ff_url     = EXCLUDED.ff_url,
    gtin       = EXCLUDED.gtin,
    name       = EXCLUDED.name,
    volume     = EXCLUDED.volume,
    bp_url     = EXCLUDED.bp_url,
    updated_at = NOW();
"""

# STEP 2: save results of many pages in one query
BP_SAVE = """
UPDATE branded_perfume_catalog AS b SET
    bp_price         = v.price::numeric,
    bp_price_inc_tax = v.price_inc::numeric,
    bp_stock         = v.stock,
    bp_status        = v.status,
    bp_title         = v.title,
    bp_size          = v.size,
    checked_at       = NOW(),
    updated_at       = NOW()
FROM (VALUES %s) AS v(id, price, price_inc, stock, status, title, size)
WHERE b.id = v.id;
"""


def bp_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    bp_log("[BP-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ---------------------------------------------------------------- reading one product page
def to_number(text):
    """'1,216' / '397.50' -> 1216.0 / 397.5 (None if no number)."""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(text or ""))
    return round(float(m.group(0).replace(",", "")), 2) if m else None


def from_json_ld(html):
    """Product data many shops put in <script type="application/ld+json"> (Google product info).
    Returns (price, in_stock, name) - any of them can be None."""
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for it in items:
            if not isinstance(it, dict) or "Product" not in str(it.get("@type")):
                continue
            offers = it.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            avail = str(offers.get("availability", ""))
            in_stock = None
            if "InStock" in avail:
                in_stock = True
            elif "OutOfStock" in avail or "SoldOut" in avail:
                in_stock = False
            return to_number(offers.get("price") or offers.get("lowPrice")), in_stock, it.get("name")
    return None, None, None


def bp_parse(html):
    """Read price, inc-tax price and stock from a Branded Perfume (CS-Cart) product page."""
    ld_price, ld_stock, ld_name = from_json_ld(html)

    # CS-Cart: <span id="sec_discounted_price_123" class="ty-price-num">378</span>
    m = re.search(r'id="sec_discounted_price_\d+"[^>]*>\s*([\d,\.]+)', html)
    price = to_number(m.group(1)) if m else ld_price

    # inc-tax price: CS-Cart <span id="sec_price_with_tax_123">397</span>, else the text "(AED 397 inc tax)"
    m = re.search(r'id="sec_price_with_tax_\d+"[^>]*>\s*([\d,\.]+)', html)
    if m:
        price_inc = to_number(m.group(1))
    else:
        text = htmllib.unescape(re.sub(r"<[^>]+>", " ", html))   # remove tags, turn &zwj; etc. into characters
        m = re.search(r"AED\W*([\d,]+(?:\.\d+)?)\W*inc\.?\s*tax", text, re.I)
        price_inc = to_number(m.group(1)) if m else None

    # stock: Google data first, then CS-Cart stock labels, then "Add to cart" button
    if ld_stock is not None:
        in_stock = ld_stock
    elif "ty-qty-out-of-stock" in html:
        in_stock = False
    elif "ty-qty-in-stock" in html:
        in_stock = True
    else:
        in_stock = "checkout.add" in html            # add-to-cart form exists -> can be bought

    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
    title = re.sub(r"<[^>]+>|\s+", " ", h1.group(1)).strip() if h1 else ld_name
    title = htmllib.unescape(title) if title else None

    # size from the product name: "... Eau de Parfum 100ml" -> "100 ml"
    m = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", title or "", re.I)
    size = f"{m.group(1)} ml" if m else None
    return {"title": title, "size": size, "price": price, "price_inc_tax": price_inc,
            "stock": "In stock" if in_stock else "Out of stock"}


def bp_fetch(session, url):
    """Open one Branded Perfume page. Returns (result dict, status).
    status: ok / not_found / blocked / error"""
    for attempt, wait in enumerate([0] + BP_RETRY_WAITS):
        if wait:
            bp_log(f"[BP] busy, retry {attempt}/{len(BP_RETRY_WAITS)} in {wait}s: {url}")
            time.sleep(wait)
        try:
            r = session.get(url, headers=BP_HEADERS, timeout=40, allow_redirects=True)
        except requests.RequestException as ex:
            bp_log(f"[BP] network error {ex} | {url}")
            continue
        if r.status_code == 429 or r.status_code >= 500:
            continue
        body = r.text
        if r.status_code == 403 or "cf-challenge" in body or "Just a moment" in body[:3000]:
            return None, "blocked"                   # Cloudflare / firewall stopped us
        if r.status_code == 404:
            return None, "not_found"
        # product missing -> CS-Cart may redirect to another page (home / category)
        if urlparse(r.url).path.rstrip("/") != urlparse(url).path.rstrip("/"):
            return None, "not_found"
        data = bp_parse(body)
        if data["price"] is None and data["price_inc_tax"] is None:
            return data, "no_price"                  # page opened but no price found -> check parser
        return data, "ok"
    return None, "error"


# ---------------------------------------------------------------- step 1
def bp_load():
    started = time.time()
    st = {"step": "load", "status": "running"}
    bp_state["last"] = st
    conn = bp_conn()
    cur = conn.cursor()
    try:
        bp_log("[BP-LOAD] Creating table branded_perfume_catalog (if not exists)")
        cur.execute(BP_CREATE)
        bp_log("[BP-LOAD] Copying French Fragrance products ...")
        cur.execute(BP_LOAD)
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM branded_perfume_catalog")
        st["products"] = cur.fetchone()[0]
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        bp_log(f"[BP-LOAD DONE] {st}")


# ---------------------------------------------------------------- step 2
def bp_run(resume):
    started = time.time()
    st = {"step": "run", "status": "running", "resume": resume, "total": 0, "done": 0,
          "in_stock": 0, "out_of_stock": 0, "not_found": 0, "no_price": 0, "errors": 0}
    bp_state["last"] = st

    conn = bp_conn()
    cur = conn.cursor()
    try:
        cur.execute(BP_CREATE)
        conn.commit()
        where = "WHERE checked_at IS NULL" if resume else ""
        cur.execute(f"SELECT id, bp_url FROM branded_perfume_catalog {where} ORDER BY id")
        todo = cur.fetchall()
        st["total"] = len(todo)
        bp_log(f"[BP-RUN] {len(todo)} Branded Perfume pages to open ({BP_WORKERS} at a time)")
        if not todo:
            st["status"] = "done"
            return

        session = requests.Session()
        stop = threading.Event()                     # set when the site blocks us -> stop everything

        def work(row):
            rid, url = row
            if stop.is_set():
                return None
            data, status = bp_fetch(session, url)
            time.sleep(BP_DELAY)
            if status == "blocked":
                stop.set()
                return None
            if status == "ok" or status == "no_price":
                return (rid, data["price"], data["price_inc_tax"], data["stock"], status, data["title"], data["size"])
            if status == "not_found":
                return (rid, None, None, "Not on site", status, None, None)
            return (rid, None, None, None, status, None, None)   # error -> checked again next run

        pending = []
        with ThreadPoolExecutor(max_workers=BP_WORKERS) as pool:
            for res in pool.map(work, todo):
                if res is None:
                    continue
                pending.append(res)
                st["done"] += 1
                stock, status = res[3], res[4]
                if status == "not_found":
                    st["not_found"] += 1
                elif status == "error":
                    st["errors"] += 1
                else:
                    st["in_stock" if stock == "In stock" else "out_of_stock"] += 1
                    if status == "no_price":
                        st["no_price"] += 1
                if len(pending) >= BP_SAVE_EVERY:
                    execute_values(cur, BP_SAVE, pending)
                    conn.commit()
                    pending = []
                    st["seconds"] = round(time.time() - started, 1)
                    bp_log(f"[BP-RUN] {st['done']}/{st['total']} | in stock {st['in_stock']} | "
                           f"out {st['out_of_stock']} | not on site {st['not_found']} | errors {st['errors']}")
        if pending:
            execute_values(cur, BP_SAVE, pending)
            conn.commit()

        if stop.is_set():
            st["status"] = "blocked"
            bp_log("[BP-RUN] STOPPED: brandedperfume.com is blocking this server (Cloudflare / 403)")
        else:
            st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        bp_log(f"[BP-RUN DONE] {st}")


def bp_worker(fn, *args):
    try:
        fn(*args)
    except Exception as ex:
        bp_log(f"[BP-ERROR] {ex}\n{traceback.format_exc()}")
        bp_state["last"] = {**(bp_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        bp_state["running"] = False
        bp_lock.release()


# ---------------------------------------------------------------- routes
def bp_secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


def bp_start(fn, *args):
    if not bp_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not bp_lock.acquire(blocking=False):
        return jsonify({"error": "a Branded Perfume job is already running"}), 409
    bp_state["running"] = True
    threading.Thread(target=bp_worker, args=(fn, *args), daemon=True).start()
    return jsonify({"started": True, "check_status": "/branded-perfume/status"}), 202


@branded_perfume_bp.route("/branded-perfume/test")
def bp_test_route():
    """Open 3 products (or ?url=...) right now and show what was read. Run this BEFORE step 2."""
    if not bp_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if request.args.get("url"):
        urls = [request.args["url"]]
    else:
        conn = bp_conn()
        cur = conn.cursor()
        try:
            cur.execute("SELECT product_url FROM french_fragrance_catalog "
                        "WHERE product_url IS NOT NULL ORDER BY id LIMIT 3")
            urls = [u.replace(FF_DOMAIN, BP_DOMAIN) for (u,) in cur.fetchall()]
        finally:
            cur.close()
            conn.close()
    session = requests.Session()
    results = []
    for u in urls:
        data, status = bp_fetch(session, u)
        bp_log(f"[BP-TEST] {status} | {u} | {data}")
        results.append({"url": u, "status": status, "read": data})
    return jsonify(results)


@branded_perfume_bp.route("/branded-perfume/load")
def bp_load_route():
    return bp_start(bp_load)


@branded_perfume_bp.route("/branded-perfume/run")
def bp_run_route():
    return bp_start(bp_run, request.args.get("resume") == "1")


@branded_perfume_bp.route("/branded-perfume/status")
def bp_status_route():
    return jsonify({"running": bp_state["running"], "last": bp_state["last"]})
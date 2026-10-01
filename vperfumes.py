"""
V Perfumes catalog (vperfumes.com UAE -> Postgres french_fragrance_db, table vperfumes_catalog)
Plugged into app.py as a Blueprint.

V Perfumes is NOT Shopify (custom website by Webcastle), so there is no products.json.
But the "Perfumes" category page lists every perfume with name, link and price:
    https://www.vperfumes.com/ae-en/category/perfumes?page=1, 2, 3 ...   (24 perfumes per page)
Only the Perfumes category is read (gift sets are a separate category), and any name with
"Set" / "Gift" is skipped as well -> perfumes only.

Routes (open in the browser):
  GET /vperfumes/test?secret=XXX    read pages 1 and 2 only and SHOW what was found (run this first)
  GET /vperfumes/run?secret=XXX     download all perfume pages into vperfumes_catalog (background)
  GET /vperfumes/status             progress

Env vars (already set on Render):
  FF_DATABASE_URL = Postgres URL ending in /french_fragrance_db
  RUN_SECRET      = same secret as the other /run URLs
"""

import os
import re
import html as htmllib
import time
import threading
import traceback

import requests
import psycopg2
from psycopg2.extras import execute_values
from flask import Blueprint, request, jsonify

vperfumes_bp = Blueprint("vperfumes", __name__)

VP_SITE = "https://www.vperfumes.com"
VP_LIST = VP_SITE + "/ae-en/category/perfumes"      # UAE, English, Perfumes category only
VP_MAX_PAGES = 600                                   # 8,500 perfumes / 24 per page = ~355 pages
VP_DELAY = 1.5                                       # seconds between pages (be gentle)
VP_SKIP = re.compile(r"\b(set|gift)\b", re.I)        # not perfumes-only -> skipped
VP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

vp_lock = threading.Lock()                           # only one V Perfumes job at a time
vp_state = {"running": False, "last": None}          # shown at /vperfumes/status


def vp_log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- database
# Same fields as the Airtable "FF Catalog": Product URL, GTIN, Name, Price Inc Tax, Price, Stock, Volume
VP_CREATE = """
CREATE TABLE IF NOT EXISTS vperfumes_catalog (
    id            SERIAL PRIMARY KEY,
    product_url   TEXT UNIQUE NOT NULL,     -- https://www.vperfumes.com/ae-en/product/...
    gtin          TEXT,                     -- barcode: NOT shown on V Perfumes category pages -> empty
    name          TEXT,                     -- e.g. "Burberry EDP For Women 100ML"
    price_inc_tax NUMERIC(10,2),            -- price shown on the site (UAE prices include 5% VAT)
    price         NUMERIC(10,2),            -- same price without VAT (price_inc_tax / 1.05)
    stock         TEXT,                     -- In stock / Out of stock / Not on site
    volume        TEXT,                     -- e.g. "100 ml" (read from the name)
    updated_at    TIMESTAMP DEFAULT NOW()
);
ALTER TABLE vperfumes_catalog ADD COLUMN IF NOT EXISTS gtin TEXT;
ALTER TABLE vperfumes_catalog ADD COLUMN IF NOT EXISTS price_inc_tax NUMERIC(10,2);
ALTER TABLE vperfumes_catalog DROP COLUMN IF EXISTS old_price;      -- not needed (only if an older version made them)
ALTER TABLE vperfumes_catalog DROP COLUMN IF EXISTS discount_pct;
"""

VP_UPSERT = """
INSERT INTO vperfumes_catalog (product_url, name, price_inc_tax, price, stock, volume)
VALUES %s
ON CONFLICT (product_url) DO UPDATE SET
    name          = EXCLUDED.name,
    price_inc_tax = EXCLUDED.price_inc_tax,
    price         = EXCLUDED.price,
    stock         = EXCLUDED.stock,
    volume        = EXCLUDED.volume,
    updated_at    = NOW();
"""


def vp_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    vp_log("[VP-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ---------------------------------------------------------------- reading one category page
def clean(text):
    """HTML piece -> plain text (tags removed, &amp; etc. turned into characters, spaces tidied)."""
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def to_number(s):
    try:
        return round(float(str(s).replace(",", "")), 2)
    except ValueError:
        return None


def vp_parse(page_html):
    """Find every product on one category page.
    Each product has links to /ae-en/product/<slug>; one of them holds the text
    "Burberry EDP For Women 100ML AED 116AED 410"  -> name, price now (116; 410 = old price, not saved)."""
    links = [(m.start(), m.group(1), clean(m.group(2))) for m in re.finditer(
        r'<a[^>]+href="(?:https://www\.vperfumes\.com)?(/ae-en/product/[^"?#]+)"[^>]*>(.*?)</a>',
        page_html, re.S | re.I)]

    products, order = {}, []
    for pos, path, text in links:
        p = products.get(path)
        if p is None:
            p = products[path] = {"url": VP_SITE + path, "pos": pos, "texts": []}
            order.append(path)
        p["texts"].append(text)

    results = []
    for i, path in enumerate(order):
        p = products[path]
        # the link text that contains the prices
        priced = next((t for t in p["texts"] if "AED" in t), "")
        prices = [to_number(x) for x in re.findall(r"AED\s*([\d,]+(?:\.\d+)?)", priced)]
        name = (priced.split("AED")[0] if priced else max(p["texts"], key=len)).strip()
        name = re.sub(r"\s*\d+\s*%\s*Off\s*$", "", name, flags=re.I).strip()
        if not name:
            continue

        # stock: look in this product's part of the page (until the next product starts)
        end = products[order[i + 1]]["pos"] if i + 1 < len(order) else p["pos"] + 4000
        block = clean(page_html[p["pos"]:end]).lower()
        if re.search(r"out of stock|sold out|notify me", block):
            stock = "Out of stock"
        elif "add to cart" in block:
            stock = "In stock"
        else:
            stock = "Out of stock"

        m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*ml\b", name, re.I)        # "100ML" / "1,000ML"
        price_inc = prices[0] if prices else None          # first AED amount = price now (2nd = crossed-out old price)
        price_ex = round(price_inc / 1.05, 2) if price_inc else None
        results.append({"url": p["url"], "name": name, "price_inc_tax": price_inc, "price": price_ex,
                        "stock": stock, "volume": f"{m.group(1).replace(',', '')} ml" if m else None})
    return results


def vp_get(page):
    """Download one category page (retries when the site is busy)."""
    for attempt, wait in enumerate([0, 10, 30, 90]):
        if wait:
            vp_log(f"[VP] page {page}: retry {attempt}/3 in {wait}s")
            time.sleep(wait)
        try:
            r = requests.get(VP_LIST, params={"page": page}, headers=VP_HEADERS, timeout=60)
        except requests.RequestException as ex:
            vp_log(f"[VP] page {page}: network error {ex}")
            continue
        if r.status_code == 429 or r.status_code >= 500:
            vp_log(f"[VP] page {page}: HTTP {r.status_code} (busy)")
            continue
        if r.status_code == 403 or "cf-challenge" in r.text or "Just a moment" in r.text[:3000]:
            raise RuntimeError(f"V Perfumes is blocking this server (HTTP {r.status_code})")
        return r.text
    return None


def total_items(page_html):
    """'8494 items in Perfumes' -> 8494 (None if not found)."""
    m = re.search(r"([\d,]+)\s+items\s+in\s+Perfumes", clean(page_html), re.I)
    return int(m.group(1).replace(",", "")) if m else None


# ---------------------------------------------------------------- full download
def vp_run():
    started = time.time()
    st = {"status": "running", "pages": 0, "saved": 0, "skipped_sets": 0, "in_stock": 0,
          "out_of_stock": 0, "site_total": None, "incomplete": False}
    vp_state["last"] = st

    conn = vp_conn()
    cur = conn.cursor()
    try:
        cur.execute(VP_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()

        seen = set()
        for page in range(1, VP_MAX_PAGES + 1):
            body = vp_get(page)
            if body is None:
                st["incomplete"] = True
                vp_log(f"[VP] page {page}: gave up -> stopping (run is incomplete)")
                break
            if page == 1:
                st["site_total"] = total_items(body)
                vp_log(f"[VP] Site says {st['site_total']} perfumes")
            items = [it for it in vp_parse(body) if it["url"] not in seen]
            if not items:
                vp_log(f"[VP] page {page}: no new products -> end of list")
                break

            rows = []
            for it in items:
                seen.add(it["url"])
                if VP_SKIP.search(it["name"]):
                    st["skipped_sets"] += 1
                    continue
                rows.append((it["url"], it["name"], it["price_inc_tax"], it["price"], it["stock"], it["volume"]))
                st["in_stock" if it["stock"] == "In stock" else "out_of_stock"] += 1
            if rows:
                execute_values(cur, VP_UPSERT, rows)
                conn.commit()
            st["pages"] = page
            st["saved"] += len(rows)
            st["seconds"] = round(time.time() - started, 1)
            vp_log(f"[VP] page {page}: {len(items)} products, {len(rows)} saved "
                   f"(total {st['saved']}, sets skipped {st['skipped_sets']})")
            time.sleep(VP_DELAY)

        # perfumes not seen in a COMPLETE run are no longer on the site
        if not st["incomplete"] and st["saved"] > 0:
            cur.execute("UPDATE vperfumes_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            st["marked_not_on_site"] = cur.rowcount
            conn.commit()
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        vp_log(f"[VP-DONE] {st}")


def vp_worker():
    try:
        vp_run()
    except Exception as ex:
        vp_log(f"[VP-ERROR] {ex}\n{traceback.format_exc()}")
        vp_state["last"] = {**(vp_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        vp_state["running"] = False
        vp_lock.release()


# ---------------------------------------------------------------- routes
def vp_secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


@vperfumes_bp.route("/vperfumes/test")
def vp_test_route():
    """Read pages 1 and 2 only. Shows whether pages work, and what is read from each product."""
    if not vp_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    try:
        p1, p2 = vp_get(1) or "", vp_get(2) or ""
    except RuntimeError as ex:
        return jsonify({"blocked": True, "error": str(ex)})
    a, b = vp_parse(p1), vp_parse(p2)
    urls_a = {x["url"] for x in a}
    result = {
        "site_total": total_items(p1),
        "page1_products": len(a),
        "page2_products": len(b),
        "page2_new_products": len([x for x in b if x["url"] not in urls_a]),   # 0 = ?page= does not work
        "page1_first_3": a[:3],
        "page2_first_3": b[:3],
        "sets_on_page1": [x["name"] for x in a if VP_SKIP.search(x["name"])],
    }
    vp_log(f"[VP-TEST] {result}")
    return jsonify(result)


@vperfumes_bp.route("/vperfumes/run")
def vp_run_route():
    if not vp_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not vp_lock.acquire(blocking=False):
        return jsonify({"error": "V Perfumes download already running"}), 409
    vp_state["running"] = True
    vp_log("[VP-RUN] Started")
    threading.Thread(target=vp_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/vperfumes/status"}), 202


@vperfumes_bp.route("/vperfumes/status")
def vp_status_route():
    return jsonify({"running": vp_state["running"], "last": vp_state["last"]})
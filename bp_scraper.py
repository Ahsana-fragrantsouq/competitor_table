r"""
Branded Perfume scraper (runs on YOUR PC with YOUR Chrome - Cloudflare lets real browsers in)
--------------------------------------------------------------------------------------------
Same method as ff_scraper.py. Branded Perfume = same company as French Fragrance, so every product
already has its Branded Perfume link in Postgres table branded_perfume_catalog
(filled by https://competitor-table.onrender.com/branded-perfume/load).

What it does:
  1. Reads the Branded Perfume links from Postgres (branded_perfume_catalog)
  2. Opens every page in your Chrome -> price, price inc tax, stock, size, name
  3. Saves the result straight into Postgres (every 50 products)
     -> visible on https://competitor-table.onrender.com/competitors?tab=bp

BEFORE running: start Chrome yourself (normal, NOT automated -> Cloudflare accepts it):
  "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir=C:\ff-scraper\chrome-profile
  -> open brandedperfume.com in that window, pass "Verify you are human" once, leave the window open.

And set the database (EXTERNAL url, ending in /french_fragrance_db):
  set "FF_DATABASE_URL=postgresql://...oregon-postgres.render.com/french_fragrance_db"

Usage (Windows, in the folder of this file). --shop bp = Branded Perfume (default), --shop es = Essenzi:
  python bp_scraper.py --shop es --test 5      -> scrape 5 products, only PRINT (nothing saved)
  python bp_scraper.py --shop es --url URL     -> test one product page
  python bp_scraper.py --shop es               -> full run, only products not checked yet (so it also RESUMES)
  python bp_scraper.py --shop es --refresh     -> full run, re-checks everything (fresh prices / stock)
For Essenzi: open essenzi.com in the Chrome window first, and fill the table once on Render:
  https://competitor-table.onrender.com/essenzi/load?secret=...
"""

import os
import re
import json
import time
import argparse

import psycopg2
from psycopg2.extras import execute_values
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- config
# Sister shops of French Fragrance (same company + same links, other domain). Same list as branded_perfume.py.
SHOPS = {
    "bp": {"label": "Branded Perfume", "site": "https://brandedperfume.com", "table": "branded_perfume_catalog", "prefix": "bp"},
    "es": {"label": "Essenzi", "site": "https://essenzi.com", "table": "essenzi_catalog", "prefix": "es"},
}
SHOP = SHOPS["bp"]                   # set from --shop in main()
DELAY = 1.5                          # seconds between product pages (be gentle, avoid blocks)
SAVE_EVERY = 50                      # save to Postgres every 50 products
CDP_URL = "http://localhost:9222"    # Chrome started by you with --remote-debugging-port=9222
NEW_TAB_EVERY = 200                  # open a fresh tab every 200 products (one tab for hours = out of memory)
DEBUG = False


def save_sql():
    """Save query for the chosen shop (columns start with its prefix: bp_price / es_price ...)."""
    t, p = SHOP["table"], SHOP["prefix"]
    return f"""
UPDATE {t} AS b SET
    {p}_price         = v.price::numeric,
    {p}_price_inc_tax = v.price_inc::numeric,
    {p}_stock         = v.stock,
    {p}_status        = v.status,
    {p}_title         = v.title,
    {p}_size          = v.size,
    checked_at        = NOW(),
    updated_at        = NOW()
FROM (VALUES %s) AS v(id, price, price_inc, stock, status, title, size)
WHERE b.id = v.id;
"""


def add_columns_sql():
    """Makes sure the size / title columns exist even if Render was not updated yet."""
    t, p = SHOP["table"], SHOP["prefix"]
    return f"""
ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {p}_title TEXT;
ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {p}_size TEXT;
"""


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- Postgres
def db():
    url = os.environ.get("FF_DATABASE_URL", "")
    if not url or "..." in url:
        raise SystemExit('Set the database first:  set "FF_DATABASE_URL=postgresql://...render.com/french_fragrance_db"')
    return psycopg2.connect(url, sslmode="require")


def load_todo(refresh):
    """Branded Perfume links to open. Normal run = only not-checked-yet (so a stopped run continues)."""
    conn = db()
    cur = conn.cursor()
    t, p = SHOP["table"], SHOP["prefix"]
    cur.execute("SELECT to_regclass(%s)", (t,))
    if cur.fetchone()[0] is None:
        load_url = "/essenzi/load" if p == "es" else "/branded-perfume/load"
        raise SystemExit(f"[DB] Table {t} does not exist yet - open {load_url}?secret=... on Render first")
    cur.execute(add_columns_sql())
    conn.commit()
    where = "" if refresh else "WHERE checked_at IS NULL"
    cur.execute(f"SELECT id, {p}_url FROM {t} {where} ORDER BY id")
    rows = cur.fetchall()
    cur.execute(f"SELECT COUNT(*) FROM {t}")
    total = cur.fetchone()[0]
    cur.close()
    conn.close()
    log(f"[DB] {SHOP['label']}: {total} products in {t} | {len(rows)} to scrape now")
    return rows


def save(batch):
    """Write results to Postgres. Keeps retrying if the internet is down (data stays in memory)."""
    while True:
        try:
            conn = db()
            cur = conn.cursor()
            execute_values(cur, save_sql(), batch)
            conn.commit()
            cur.close()
            conn.close()
            log(f"[DB] saved {len(batch)} products")
            return
        except psycopg2.OperationalError as ex:
            log(f"[DB] No connection ({str(ex).splitlines()[0][:80]}) - waiting 60s, data is kept in memory")
            time.sleep(60)


# ---------------------------------------------------------------- Cloudflare (same as ff_scraper.py)
def wait_cloudflare(page, max_wait=180):
    """If Cloudflare 'Just a moment' shows, wait (click the checkbox in the Chrome window if asked)."""
    start = time.time()
    while True:
        try:
            title = (page.title() or "").lower()
            body = page.content()[:5000].lower()
        except Exception:
            time.sleep(2)            # page still navigating
            continue
        if "just a moment" not in title and "security verification" not in body and "verifying you are human" not in body:
            return True
        if time.time() - start > max_wait:
            log("[CLOUDFLARE] Still blocked after waiting - stopping.")
            return False
        log("[CLOUDFLARE] Verification page... waiting (click the checkbox in Chrome if you see one)")
        time.sleep(5)


# ---------------------------------------------------------------- product page
def find_product_jsonld(blocks):
    for raw in blocks:
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items:
            if isinstance(it, dict) and "product" in str(it.get("@type", "")).lower():
                return it
    return None


def to_number(s):
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(s or ""))
    return round(float(m.group(0).replace(",", "")), 2) if m else None


def scrape_product(page, url):
    """Open one Branded Perfume page -> dict with status ok / not_found (+ price, stock, size ...)."""
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=90000)
    except Exception as ex:
        if any(k in str(ex) for k in ("ERR_INTERNET", "ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION", "ERR_NETWORK", "Timeout")):
            raise ConnectionError(str(ex).splitlines()[0][:120])   # internet problem -> wait & retry
        raise
    if not wait_cloudflare(page):
        raise RuntimeError("Cloudflare block")

    # page does not exist on Branded Perfume -> 404, or the shop redirects to another page
    if (resp and resp.status == 404) or page.url.split("?")[0].rstrip("/") != url.split("?")[0].rstrip("/"):
        return {"status": "not_found", "stock": "Not on site"}

    blocks = page.eval_on_selector_all('script[type="application/ld+json"]', "els => els.map(e => e.textContent)")
    text = page.inner_text("body")
    jd = find_product_jsonld(blocks) or {}
    if not jd:
        if "you are offline" in text[:500].lower():
            raise ConnectionError("page not loaded (offline) - not saved")
        if re.search(r"page not found|404", text[:3000], re.I):
            return {"status": "not_found", "stock": "Not on site"}
        raise RuntimeError("no product data on page")               # counted as error, checked again next run

    offers = jd.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    # gift sets: {"@type": "AggregateOffer", "lowPrice": 198, "offers": [{... "availability": ...}]}
    inner = offers.get("offers") or []
    inner = inner[0] if isinstance(inner, list) and inner else (inner if isinstance(inner, dict) else {})

    name = jd.get("name") or (page.title() or "").split("|")[0].strip()

    # price without VAT (Google data), inc-tax price shown as "(AED 397 inc tax)", else +5% VAT
    price = to_number(offers.get("price") or offers.get("lowPrice") or inner.get("price"))
    if price is None:
        m = re.search(r"AED\W*([\d,]+(?:\.\d+)?)", text)
        price = to_number(m.group(1)) if m else None
    m = re.search(r"AED\W*([\d,]+(?:\.\d+)?)\W*inc\.?\s*tax", text, re.I)
    price_inc = to_number(m.group(1)) if m else (round(price * 1.05, 2) if price else None)

    # stock: the product's own data first (page text can mention OTHER products' stock)
    avail = str(offers.get("availability") or inner.get("availability") or "").lower()
    if "outofstock" in avail or "soldout" in avail:
        stock = "Out of stock"
    elif "instock" in avail:
        stock = "In stock"
    elif re.search(r"add to cart", text, re.I):
        stock = "In stock"
    else:
        stock = "Out of stock"

    m = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", name or "", re.I)
    size = f"{m.group(1)} ml" if m else None

    if DEBUG:
        log(f"   [DEBUG] JSON-LD offers: {json.dumps(offers)[:200]}")
    return {"status": "ok", "title": name, "size": size, "price": price, "price_inc_tax": price_inc, "stock": stock}


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", type=int, default=0, help="scrape N products and only print")
    ap.add_argument("--refresh", action="store_true", help="re-check products already checked")
    ap.add_argument("--url", default="", help="test one product URL")
    ap.add_argument("--shop", default="bp", choices=sorted(SHOPS), help="bp = Branded Perfume, es = Essenzi")
    args = ap.parse_args()
    global SHOP
    SHOP = SHOPS[args.shop]
    log(f"[SHOP] {SHOP['label']} ({SHOP['site']}) -> table {SHOP['table']}")

    with sync_playwright() as p:
        # Connect to the Chrome YOU started with --remote-debugging-port=9222 (not flagged as automated)
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as ex:
            log(f"[CHROME] Could not connect to Chrome at {CDP_URL}: {ex}")
            log("[CHROME] Start Chrome first with the command at the top of this file, then run again.")
            return
        page = browser.contexts[0].new_page()
        log("[CHROME] Connected to your Chrome - a new tab is used for scraping")

        log(f"[START] Opening {SHOP['label']} ...")
        page.goto(SHOP["site"], wait_until="domcontentloaded", timeout=90000)
        if not wait_cloudflare(page):
            return

        global DEBUG
        if args.url:
            DEBUG = True
            log(f"[TEST-URL] {json.dumps(scrape_product(page, args.url), ensure_ascii=False)}")
            return

        todo = load_todo(args.refresh)
        if args.test:
            DEBUG = True
            for rid, u in todo[:args.test]:
                log(f"[TEST] {u}")
                log(f"[TEST] {json.dumps(scrape_product(page, u), ensure_ascii=False)}")
                time.sleep(DELAY)
            log("[TEST] Done - nothing was saved.")
            return

        batch, started, errors_in_row, driver_lost = [], time.time(), 0, False
        counts = {"In stock": 0, "Out of stock": 0, "Not on site": 0, "errors": 0}
        for i, (rid, u) in enumerate(todo, 1):
            row = None
            for attempt in range(1, 31):
                try:
                    row = scrape_product(page, u)
                    break
                except ConnectionError as ex:
                    log(f"[{i}/{len(todo)}] OFFLINE ({ex}) - waiting 60s, retry {attempt}/30")
                    time.sleep(60)
                except Exception as ex:
                    msg = str(ex)
                    if "Connection closed" in msg or "has been closed" in msg:
                        driver_lost = True           # Chrome / Playwright connection is gone
                    log(f"[{i}/{len(todo)}] ERROR {u}: {msg.splitlines()[0][:150]}")
                    break
            if driver_lost:
                log("[STOP] Lost the connection to Chrome (memory / Chrome closed). Saving and stopping.")
                log("[STOP] Just run the same command again - it continues with the products not saved yet.")
                break
            if row is None:
                counts["errors"] += 1
                errors_in_row += 1
                if errors_in_row >= 20:
                    log("[STOP] 20 errors in a row - probably blocked. Saving and stopping; run again later to resume.")
                    break
            else:
                errors_in_row = 0
                counts[row["stock"]] = counts.get(row["stock"], 0) + 1
                batch.append((rid, row.get("price"), row.get("price_inc_tax"), row["stock"], row["status"],
                              row.get("title"), row.get("size")))
                log(f"[{i}/{len(todo)}] {(row.get('title') or u)[:60]} | {row.get('size') or '-'} | "
                    f"AED {row.get('price_inc_tax') or '-'} | {row['stock']}")
            if len(batch) >= SAVE_EVERY:
                save(batch)
                batch = []
                mins = (time.time() - started) / 60
                log(f"[PROGRESS] {i}/{len(todo)} done in {mins:.0f} min | {counts}")
            if i % NEW_TAB_EVERY == 0:
                # a tab that opened thousands of pages keeps growing in memory -> replace it with a fresh one
                try:
                    page.close()
                except Exception:
                    pass
                page = browser.contexts[0].new_page()
                log(f"[TAB] Opened a fresh tab after {i} products (keeps memory low)")
            time.sleep(DELAY)
        if batch:
            save(batch)
        log(f"[DONE] {counts} | {(time.time() - started) / 60:.0f} min")
        try:
            page.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
r"""
Ounass scraper (runs on YOUR PC with YOUR Chrome - Ounass refuses Render's server with HTTP 429)
-------------------------------------------------------------------------------------------------
Opens the Ounass fragrance pages in your Chrome, reads every product card (same reading code as
ounass.py on Render) and saves them into Postgres table ounass_catalog.
After it finishes, match on Render:  https://competitor-table.onrender.com/competitor/match-on?secret=...

Pages read (Fragrances only):
  https://www.ounass.ae/women/beauty/fragrance
  https://www.ounass.ae/men/grooming/fragrance
Next pages: tries ?p=2 / ?page=2 / ?fh_start_index=24; if none works it scrolls down in Chrome
until all products are loaded (Ounass loads more while you scroll).

BEFORE running (same Chrome window as the other scrapers is fine):
  "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir=C:\ff-scraper\chrome-profile
  set "FF_DATABASE_URL=postgresql://...oregon-postgres.render.com/french_fragrance_db"

Put this file next to ounass.py, then:
  python on_scraper.py --test      -> page 1 + page 2 of women's fragrance, PRINT only (nothing saved)
  python on_scraper.py             -> download women + men fragrance into ounass_catalog
"""

import os
import time
import argparse

import psycopg2
from psycopg2.extras import execute_values
from playwright.sync_api import sync_playwright

import ounass as on          # same table + same card reading as on Render

CDP_URL = "http://localhost:9222"
DELAY = 2.0                  # seconds between pages (gentle - Ounass limits fast visitors)
NEW_TAB_EVERY = 150          # fresh tab now and then (keeps Chrome memory low)


def log(*a):
    print(*a, flush=True)


def db():
    url = os.environ.get("FF_DATABASE_URL", "")
    if not url or "..." in url:
        raise SystemExit('Set the database first:  set "FF_DATABASE_URL=postgresql://...render.com/french_fragrance_db"')
    return psycopg2.connect(url, sslmode="require")


def open_html(page, url):
    """Open a page in Chrome and return its HTML (after products are drawn)."""
    for attempt in range(1, 4):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            try:
                page.wait_for_selector('a[href*="/shop-"]', timeout=20000)     # product cards visible
            except Exception:
                pass
            return page.content()
        except Exception as ex:
            log(f"[ON] {url}: try {attempt}/3 failed ({str(ex).splitlines()[0][:120]})")
            time.sleep(15 * attempt)
    return None


def page_url(path, pname=None, pval=None, n=1):
    return on.ON_SITE + path + (f"?{pname}={pval(n)}" if pname and n > 1 else "")


def detect_paging(page, path, first_urls):
    """Which 'next page' link works in the browser: ?p=2, ?page=2 or ?fh_start_index=24 (None = scroll)."""
    for name, value in on.ON_PAGE_PARAMS:
        body = open_html(page, page_url(path, name, value, 2)) or ""
        new = [x for x in on.on_parse(body) if x["url"] not in first_urls]
        log(f"[ON] paging test ?{name}={value(2)}: {len(new)} new products")
        # a real page 2 has (almost) all NEW products; a few new ones = page 1 again + rotating/sponsored items
        if len(new) >= on.ON_PER_PAGE // 2:
            return name, value
        time.sleep(DELAY)
    return None, None


def scroll_all(page, path, total):
    """Fallback: open the category and keep scrolling until no more products appear."""
    page.goto(on.ON_SITE + path, wait_until="domcontentloaded", timeout=90000)
    last, same = 0, 0
    while True:
        page.mouse.wheel(0, 6000)
        time.sleep(DELAY)
        # unique product links (one card can have 2 links to the same product)
        count = page.evaluate("""() => new Set(Array.from(document.querySelectorAll('a[href*="/shop-"]'))
                                     .map(a => a.href.split('?')[0])).size""")
        log(f"[ON] scrolling {path}: {count} products loaded" + (f" (site says {total})" if total else ""))
        same = same + 1 if count == last else 0
        if same >= 6 or (total and count >= total):          # stop: everything loaded, or nothing new 6x in a row
            break
        last = count
    return on.on_parse(page.content())


def save(rows):
    conn = db()
    cur = conn.cursor()
    execute_values(cur, on.ON_UPSERT, rows)
    conn.commit()
    cur.close()
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="page 1 + 2 of women's fragrance, print only")
    args = ap.parse_args()

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as ex:
            log(f"[CHROME] Could not connect to Chrome at {CDP_URL}: {ex}")
            log("[CHROME] Start Chrome first with the command at the top of this file.")
            return
        ctx = browser.contexts[0]
        page = ctx.new_page()
        log("[CHROME] Connected - a new tab is used for Ounass")

        if args.test:
            path = on.ON_CATEGORIES[0]
            first = open_html(page, page_url(path)) or ""
            items = on.on_parse(first)
            log(f"[TEST] page 1: {len(items)} products | site says {on.total_items(first)}")
            for it in items[:3]:
                log(f"[TEST] {it}")
            pname, pval = detect_paging(page, path, {x['url'] for x in items})
            log(f"[TEST] next-page method: {pname or 'none -> will scroll instead'}")
            log("[TEST] Done - nothing was saved.")
            page.close()
            return

        conn = db()
        cur = conn.cursor()
        cur.execute(on.ON_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        seen, saved, complete, started, opened = set(), 0, True, time.time(), 0
        for path in on.ON_CATEGORIES:
            first = open_html(page, page_url(path))
            if first is None:
                complete = False
                continue
            total = on.total_items(first)
            items = on.on_parse(first)
            log(f"[ON] {path}: site says {total} products, page 1 has {len(items)}")
            pname, pval = (None, None)
            if not (total and total <= len(items)):
                pname, pval = detect_paging(page, path, {x["url"] for x in items})
            pages = [items] if pname or (total and total <= len(items)) else [scroll_all(page, path, total)]

            n = 1
            while True:
                batch = pages.pop(0) if pages else []
                rows = []
                for it in batch:
                    if it["url"] in seen or not it["name"]:
                        continue
                    seen.add(it["url"])
                    rows.append((it["url"], None, it["sku"], it["brand"], it["name"], it["price_inc_tax"],
                                 it["price"], it["stock"], it["volume"]))
                if rows:
                    save(rows)
                saved += len(rows)
                log(f"[ON] {path} page {n}: {len(batch)} products, {len(rows)} new saved (total {saved})")
                if not pname or not batch or (total and n * on.ON_PER_PAGE >= total) or n >= on.ON_MAX_PAGES:
                    break
                n += 1
                opened += 1
                if opened % NEW_TAB_EVERY == 0:
                    page.close()
                    page = ctx.new_page()
                    log("[TAB] Opened a fresh tab (keeps memory low)")
                time.sleep(DELAY)
                body = open_html(page, page_url(path, pname, pval, n))
                if body is None:
                    complete = False
                    log(f"[ON] {path} page {n}: gave up -> stopping this category (run again later)")
                    break
                pages.append(on.on_parse(body))

        if complete and saved:
            conn = db()
            cur = conn.cursor()
            cur.execute("UPDATE ounass_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            log(f"[ON] {cur.rowcount} old products marked 'Not on site'")
            conn.commit()
            cur.close()
            conn.close()
        log(f"[DONE] {saved} Ounass products saved in {(time.time() - started) / 60:.1f} min. "
            f"Now open /competitor/match-on?secret=... on Render.")
        page.close()


if __name__ == "__main__":
    main()
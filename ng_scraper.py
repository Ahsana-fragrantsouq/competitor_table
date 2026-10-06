r"""
Niche Gallerie scraper (runs on YOUR PC with YOUR Chrome - only needed if Render is blocked)
-------------------------------------------------------------------------------------------------------
Reads the WooCommerce product list in your Chrome (100 products per page) and saves it into
Postgres table nichegallerie_catalog (same code / fields as nichegallerie.py on Render).
After it finishes, match on Render:  https://competitor-table.onrender.com/competitor/match-ng?secret=...

BEFORE running (same Chrome window as the other scrapers is fine):
  "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir=C:\ff-scraper\chrome-profile
  set "FF_DATABASE_URL=postgresql://...oregon-postgres.render.com/french_fragrance_db"

Put this file next to nichegallerie.py, then:
  python ng_scraper.py --test      -> read page 1 only and PRINT 3 products (nothing saved)
  python ng_scraper.py             -> download everything into nichegallerie_catalog
"""

import os
import json
import time
import argparse

import psycopg2
from psycopg2.extras import execute_values
from playwright.sync_api import sync_playwright

import nichegallerie as aj        # same table, same field reading as on Render

CDP_URL = "http://localhost:9222"
DELAY = 1.0                 # seconds between pages


def log(*a):
    print(*a, flush=True)


def db():
    url = os.environ.get("FF_DATABASE_URL", "")
    if not url or "..." in url:
        raise SystemExit('Set the database first:  set "FF_DATABASE_URL=postgresql://...render.com/french_fragrance_db"')
    return psycopg2.connect(url, sslmode="require")


def read_page(page, n):
    """Open one page of the product list in Chrome and return the products (list), or None on failure.
    If SiteGround shows its bot check, Chrome solves it by itself - we just wait until the list appears."""
    url = f"{aj.NG_API}?{aj.NG_CURRENCY}&per_page={aj.NG_PER_PAGE}&page={n}"       # AED prices
    for attempt in range(1, 4):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            if "sgcaptcha" in page.url:
                log(f"[NG] page {n}: SiteGround check... waiting (solve it in Chrome if it asks)")
            page.wait_for_url(lambda u: "sgcaptcha" not in u, timeout=90000)
            text = page.inner_text("body").strip()
            data = json.loads(text)
            if isinstance(data, dict):                      # error message, e.g. page past the end
                if data.get("code") in ("rest_invalid_param", "woocommerce_rest_invalid_page"):
                    return []
                log(f"[NG] page {n}: answer {str(data)[:150]}")
                return []
            return data
        except Exception as ex:
            log(f"[NG] page {n}: try {attempt}/3 failed ({str(ex).splitlines()[0][:120]})")
            time.sleep(10 * attempt)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="read page 1 only and print (nothing saved)")
    args = ap.parse_args()

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as ex:
            log(f"[CHROME] Could not connect to Chrome at {CDP_URL}: {ex}")
            log("[CHROME] Start Chrome first with the command at the top of this file.")
            return
        page = browser.contexts[0].new_page()
        log("[CHROME] Connected - opening nichegallerie.com first (passes the bot check once)")
        page.goto(aj.NG_SITE, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_url(lambda u: "sgcaptcha" not in u, timeout=90000)

        if args.test:
            items = read_page(page, 1) or []
            log(f"[TEST] page 1: {len(items)} products")
            for it in items[:3]:
                log(f"[TEST] {json.dumps(aj.ng_item(it), ensure_ascii=False)}")
            log("[TEST] Done - nothing was saved.")
            page.close()
            return

        conn = db()
        cur = conn.cursor()
        cur.execute(aj.NG_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        saved, complete, started = 0, False, time.time()
        for n in range(1, aj.NG_MAX_PAGES + 1):
            items = read_page(page, n)
            if items is None:
                log(f"[NG] page {n}: gave up -> stopping (not complete, run again later)")
                break
            rows = []
            for raw in items:
                it = aj.ng_item(raw)
                if it["url"] and it["name"]:
                    rows.append((it["url"], it["gtin"], it["sku"], it["name"], it["price_inc_tax"], it["price"],
                                 it["stock"], it["volume"]))
            if rows:
                conn = db()
                cur = conn.cursor()
                execute_values(cur, aj.NG_UPSERT, rows)
                conn.commit()
                cur.close()
                conn.close()
            saved += len(rows)
            log(f"[NG] page {n}: {len(rows)} saved (total {saved})")
            if len(items) < aj.NG_PER_PAGE:                 # last page
                complete = True
                break
            time.sleep(DELAY)

        if complete and saved:
            conn = db()
            cur = conn.cursor()
            cur.execute("UPDATE nichegallerie_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            log(f"[NG] {cur.rowcount} old products marked 'Not on site'")
            conn.commit()
            cur.close()
            conn.close()
        log(f"[DONE] {saved} products saved in {(time.time() - started) / 60:.1f} min. "
            f"Now open /competitor/match-ng?secret=... on Render.")
        page.close()


if __name__ == "__main__":
    main()
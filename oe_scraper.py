r"""
Oud Elite scraper (runs on YOUR PC with YOUR Chrome) -> Postgres table oudelite_catalog
========================================================================================
Oud Elite (ae.oudelite.com) is a SALLA shop (store 1934736234) that refuses plain downloads (HTTP 403),
so - like Ounass - it is read in your Chrome. Sections read (everything in them, as asked):
  Fragrances           https://ae.oudelite.com/en/fragrances/c435056374
  Gifts & Accessories  https://ae.oudelite.com/en/gift-sets/c218966268
Two ways, the first that gives products wins (per section):
  A  Salla's product API, called from inside the shop page (exact price + stock)    -> oe_api()
  B  the product cards on the page (scrolls / clicks "load more" until all are shown) -> oe_cards()
After it finishes daily_update.py starts the matching on Render (/competitor/match-oe).

BEFORE running (same Chrome window as the other scrapers is fine; the shop must be in ENGLISH in that Chrome):
  "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir=C:\ff-scraper\chrome-profile
  FF_DATABASE_URL: from the environment (daily_update.bat sets it), else read from daily_update.bat

  python oe_scraper.py --test   -> reads both sections, PRINTS what each way found (nothing saved)
  python oe_scraper.py          -> saves into oudelite_catalog
then:  https://competitor-table.onrender.com/competitor/match-oe?secret=XXX

Where things are:
  SECTION 1  settings          -> SECTIONS, STORE_ID, API_PER_PAGE
  SECTION 2  way A: API        -> JS_API + oe_api()
  SECTION 3  way B: cards      -> JS_CARDS + oe_cards()
  SECTION 4  save              -> save()   (rows not seen in a COMPLETE read -> 'Not on site')
  SECTION 5  run               -> main()
"""

import os
import re
import sys
import time
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import psycopg2
from psycopg2.extras import execute_values
from playwright.sync_api import sync_playwright

import oudelite as oe                  # table oudelite_catalog (OE_CREATE / OE_UPSERT) + oe_volume()


def log(*a):
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
SITE = "https://ae.oudelite.com"
SECTIONS = [                            # (label, category page, Salla category id)
    ("Fragrances", f"{SITE}/en/fragrances/c435056374", "435056374"),
    ("Gifts & Accessories", f"{SITE}/en/gift-sets/c218966268", "218966268"),
]
STORE_ID = "1934736234"                 # salla.config store.id (from the shop page)
API_PER_PAGE = 50
CDP_URL = "http://localhost:9222"


# ============================================================ SECTION 2: way A - Salla API from inside the page
# runs IN the shop page (same cookies / origin as the shop itself) -> {ok, products: [...], pages, first}
JS_API = """async ([cat, store, perPage]) => {
  const api = (window.salla && salla.config && salla.config.get('store.api')) || 'https://api.salla.dev/store/v1/';
  const headers = {'Store-Identifier': String(store), 'Accept': 'application/json', 'Accept-Language': 'en', 'currency': 'AED'};
  let url = api + 'products?source=categories&source_value[]=' + cat + '&per_page=' + perPage;
  const all = []; let pages = 0, first = null;
  while (url && pages < 60) {
    const r = await fetch(url, {headers});
    const txt = await r.text();
    if (!first) first = r.status + ' ' + txt.slice(0, 600);
    if (!r.ok) return {ok: false, products: all, pages, first};
    let j; try { j = JSON.parse(txt); } catch (e) { return {ok: false, products: all, pages, first}; }
    const data = j.data || [];
    all.push(...data); pages++;
    const next = (j.cursor && j.cursor.next) || (j.links && j.links.next) || null;
    url = data.length ? next : null;
  }
  return {ok: true, products: all, pages, first};
}"""


def _amount(v):
    """Salla price: 170.01 / "170.01" / {"amount": 170.01, "currency": "AED"} -> 170.01 (or None)."""
    if isinstance(v, dict):
        v = v.get("amount")
    try:
        return round(float(v), 2) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def from_api(p):
    """One Salla API product -> our row dict (None when it has no id / link)."""
    pid, url = p.get("id"), p.get("url")
    if not pid or not url:
        return None
    price = _amount(p.get("sale_price")) or _amount(p.get("price"))
    out = p.get("is_out_of_stock")
    available = (not out) if out is not None else bool(p.get("is_available", True))
    name = (p.get("name") or "").strip()
    return {"id": int(pid), "url": url.split("?")[0], "name": name, "sku": (p.get("sku") or "").strip() or None,
            "price": price, "available": available, "type": None}


def oe_api(page, cat):
    res = page.evaluate(JS_API, [cat, STORE_ID, API_PER_PAGE])
    log(f"[OE-API] category {cat}: ok={res['ok']} pages={res['pages']} products={len(res['products'])} | first answer: {res['first'][:300]}")
    rows = [r for r in (from_api(p) for p in res["products"]) if r]
    return rows, res["ok"] and bool(rows)


# ============================================================ SECTION 3: way B - the product cards on the page
JS_CARDS = """() => Array.from(document.querySelectorAll('custom-salla-product-card, salla-product-card')).map(c => {
  const a = c.querySelector('a[href]');
  const img = c.querySelector('img[alt]');
  const title = c.querySelector('.product-card__title, h3, h2');
  const txt = c.innerText || '';
  const prices = (txt.match(/[0-9][0-9,]*(?:\\.[0-9]+)?(?=\\s*(?:AED|د\\.إ|\\u0631\\.\\u0633))/g) || []).map(x => x.replace(/,/g, ''));
  return {id: (c.id || '').replace('product-', ''), url: a ? a.href : null,
          name: (title && title.innerText.trim()) || (img && img.alt) || txt.split('\\n')[0],
          price: prices.length ? prices[0] : null,
          out: /out of stock|sold out|غير متوفر|نفذت|نفدت/i.test(txt)};
})"""
JS_MORE = """() => {
  window.scrollTo(0, document.body.scrollHeight);
  const b = Array.from(document.querySelectorAll('button, a')).find(x => /load more|show more|المزيد/i.test(x.innerText || ''));
  if (b) { b.click(); return true; } return false;
}"""


def oe_cards(page):
    """Scroll / click 'load more' until the number of cards stops growing, then read every card."""
    last, same = -1, 0
    for _ in range(80):
        n = page.evaluate("() => document.querySelectorAll('custom-salla-product-card, salla-product-card').length")
        same = same + 1 if n == last else 0
        if same >= 3:
            break
        last = n
        page.evaluate(JS_MORE)
        page.wait_for_timeout(2500)
    rows = []
    for c in page.evaluate(JS_CARDS):
        if not c["url"] or not c["id"].isdigit():
            continue
        rows.append({"id": int(c["id"]), "url": c["url"].split("?")[0].split("#")[0], "name": (c["name"] or "").strip(),
                     "sku": None, "price": _amount(c["price"]), "available": not c["out"], "type": None})
    log(f"[OE-CARDS] {len(rows)} product cards read")
    return rows, bool(rows)


# ============================================================ SECTION 4: save
def db():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        try:
            with open(os.path.join(HERE, "daily_update.bat"), encoding="utf-8", errors="replace") as f:
                m = re.search(r'set "FF_DATABASE_URL=([^"]+)"', f.read())
            url = m.group(1) if m else None
        except OSError:
            url = None
    if not url:
        raise SystemExit("[OE] FF_DATABASE_URL not set and not found in daily_update.bat")
    return psycopg2.connect(url, sslmode="require")


def save(rows, complete):
    """Rows -> oudelite_catalog (one row per Salla product). complete=True -> products not seen = 'Not on site'."""
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(oe.OE_CREATE)
        cur.execute("SELECT NOW()")
        started = cur.fetchone()[0]
        vals = []
        for r in rows:
            p = r["price"]
            vals.append((r["id"], r["url"], None, r["sku"], "Oud Elite", r["name"], r["type"], p,
                         round(p / 1.05, 2) if p else None, "In stock" if r["available"] else "Out of stock",
                         oe.oe_volume(r["name"], r["url"].replace("-", " "))))
        execute_values(cur, oe.OE_UPSERT, vals)
        gone = 0
        if complete and vals:
            cur.execute("UPDATE oudelite_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (started,))
            gone = cur.rowcount
        conn.commit()
        log(f"[OE-SAVE] {len(vals)} products saved, {gone} marked 'Not on site'")
    finally:
        conn.close()


# ============================================================ SECTION 5: run
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="read + print only, save nothing")
    args = ap.parse_args()
    found, complete = {}, True
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        page = browser.contexts[0].new_page()
        for label, url, cat in SECTIONS:
            log(f"\n[OE] ===== {label}: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(5000)
            rows, ok = [], False
            try:
                rows, ok = oe_api(page, cat)                     # way A
            except Exception as ex:
                log(f"[OE-API] error {str(ex)[:200]}")
            if args.test or not ok:
                cards, ok_b = oe_cards(page)                     # way B (also shown in --test)
                if not ok and ok_b:
                    rows, ok = cards, True
                if args.test:
                    for r in cards[:3]:
                        log(f"[OE-TEST cards] {r}")
            for r in rows[:3]:
                log(f"[OE-TEST used] {r}")
            log(f"[OE] {label}: {len(rows)} products ({'OK' if ok else 'NOTHING READ'})")
            complete = complete and ok
            for r in rows:
                found[r["id"]] = r                               # a product in both sections is saved once
        page.close()
    log(f"\n[OE] total {len(found)} products | complete read: {complete}")
    if args.test:
        log("[OE] --test: nothing saved")
        return
    if not found:
        log("[OE] nothing read -> nothing saved")
        sys.exit(2)
    save(list(found.values()), complete)
    log("[OE] Done")


if __name__ == "__main__":
    main()

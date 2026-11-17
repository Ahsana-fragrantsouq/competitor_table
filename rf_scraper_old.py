r"""
Reef Perfumes scraper (runs on YOUR PC with YOUR Chrome) -> Postgres table reefperfumes_catalog (prefix rf)
=========================================================================================================
Reef Perfumes (ae.reefperfumes.com) is a SALLA shop (store 254895921). LIVE TESTS 2026-11-05 from Render:
  - the sitemap's product links are the SAUDI store's -> on the UAE site they 302 to the home page
  - Salla's product API gives ONLY the Saudi store (sa.reefperfumes.com, scope 939196354), whatever we ask
  - UAE category pages build their product cards with JavaScript (no links / prices in the HTML)
-> exactly like Oud Elite: read the UAE product CARDS in your Chrome (copy of oe_scraper.py).
Sections: found on the UAE home page menu (links '/c123456' whose text has RF_MENU_WORDS: Perfumes, Offers ...,
not RF_MENU_SKIP: body / hair / beauty / home / gift cards) + RF_EXTRA_SECTIONS (c585180698 from the user's screenshot).
PERFUMES + GIFT SETS only: every card name goes through the same word filter as reefperfumes.py (rf_keep copy below).
After it finishes daily_update.py starts the matching on Render (/competitor/match-rf).

BEFORE running (same Chrome window as the other scrapers is fine; the shop must be in ENGLISH in that Chrome):
  "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir=C:\ff-scraper\chrome-profile
  FF_DATABASE_URL: from the environment (daily_update.bat sets it), else read from daily_update.bat

  python rf_scraper.py --test   -> reads the sections, PRINTS what was found (nothing saved)
  python rf_scraper.py          -> saves into reefperfumes_catalog
then:  https://competitor-table.onrender.com/competitor/match-rf?secret=XXX

Where things are:
  SECTION 1  settings          -> SITE, RF_EXTRA_SECTIONS, RF_MENU_WORDS / RF_MENU_SKIP, keep-word lists
  SECTION 2  sections          -> JS_MENU + rf_sections()
  SECTION 3  cards             -> JS_CARDS + rf_cards()   (scrolls / "load more" until all are shown)
  SECTION 4  save              -> save()   (rows not seen in a COMPLETE read -> 'Not on site')
  SECTION 5  run               -> main()
"""

import os
import re
import sys
import time
import hashlib
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import psycopg2
from psycopg2.extras import execute_values
from playwright.sync_api import sync_playwright


def log(*a):
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
SITE = "https://ae.reefperfumes.com"
RF_EXTRA_SECTIONS = [("Perfumes (screenshot link)", f"{SITE}/en/-/c585180698")]   # always read
RF_MENU_WORDS = ["perfume", "perfumes", "offer", "offers", "gift", "set", "bundle", "collection", "men", "women",
                 "unisex", "oud", "musk", "best seller", "new"]
RF_MENU_SKIP = ["body", "hair", "beauty", "home", "gift card", "digital", "makeup", "candle", "incense", "bakhoor"]
CDP_URL = "http://localhost:9222"
# PERFUMES + GIFT SETS (same lists as reefperfumes.py - copied so this PC file needs no Flask)
RF_ALWAYS_SKIP = ['gift card', 'sample', 'samples', 'discovery', 'giveaway*']
RF_GIFT_WORDS = ['gift set', 'gift sets', 'giftset', 'gift box', 'coffret', 'set', 'collection', 'bundle*']
RF_SKIP_WORDS = ['oil', 'oils', 'dehn', 'dahn', 'bakhoor', 'bukhoor', 'bakhour', 'incense', 'dokhoon', 'dakhoon', 'mabkhara', 'mubakhar', 'burner', 'wood', 'chips', 'muattar', 'candle', 'diffuser', 'reed', 'room spray', 'home spray', 'car', 'body', 'lotion', 'cream', 'shower', 'soap', 'hair', 'deodorant', 'accessories', 'bag', 'pouch', 'box only', 'mist', 'makeup', 'make up', 'lipstick', 'lip', 'mascara', 'nail', 'beauty', 'skin', 'hand', 'gift card', 'digital', 'gm', 'gms', 'gram', 'grams', 'kg']
RF_CREATE = """CREATE TABLE IF NOT EXISTS reefperfumes_catalog (
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
RF_UPSERT = """INSERT INTO reefperfumes_catalog (variant_id, product_url, gtin, sku, brand, name, product_type,
                                price_inc_tax, price, stock, volume)
VALUES %s
ON CONFLICT (variant_id) DO UPDATE SET
    product_url = EXCLUDED.product_url, gtin = EXCLUDED.gtin, sku = EXCLUDED.sku, brand = EXCLUDED.brand,
    name = EXCLUDED.name, product_type = EXCLUDED.product_type, price_inc_tax = EXCLUDED.price_inc_tax,
    price = EXCLUDED.price, stock = EXCLUDED.stock, volume = EXCLUDED.volume, updated_at = NOW();
"""


def has_word(words, text):
    """True when text has one of the WHOLE words ("x*" = word start), any capitals (same as brandsperfume.br_has)."""
    text = " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()) + " "
    return any(re.search(rf"\b{re.escape(w.rstrip('*'))}" + ("" if w.endswith("*") else r"\b"), text) for w in words)


def rf_keep(name):
    """Perfume or gift set? (name only - cards have no category)"""
    if has_word(RF_ALWAYS_SKIP, name):
        return False
    if has_word(RF_GIFT_WORDS, name):
        return True
    return not has_word(RF_SKIP_WORDS, name)


def rf_volume(*texts):
    """'Zahwah 100ml' -> '100 ml'"""
    for t in texts:
        m = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", str(t or ""), re.I)
        if m:
            return f"{m.group(1)} ml"
    return None


# ============================================================ SECTION 2: sections (UAE home page menu)
def safe_eval(page, js, tries=6):
    """page.evaluate that survives a page reload. LIVE 2026-11-05 on the PC: the Reef home page navigated again while
    its menu was read -> "Execution context was destroyed, most likely because of a navigation". -> wait until the
    page has settled, then try again (up to `tries` times)."""
    for i in range(tries):
        try:
            return page.evaluate(js)
        except Exception as ex:
            if "context was destroyed" not in str(ex) and "navigat" not in str(ex).lower() or i == tries - 1:
                raise
            log(f"[RF] page reloaded while reading ({page.url}) -> waiting and trying again ({i + 1}/{tries - 1})")
            try:
                page.wait_for_load_state("load", timeout=30000)
            except Exception:
                pass
            page.wait_for_timeout(3000)


def goto(page, url):
    """Open a page and let it settle (Salla shops redirect / reload once for the country / language)."""
    page.goto(url, wait_until="domcontentloaded", timeout=90000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass                                             # a shop that never goes quiet -> just continue
    page.wait_for_timeout(3000)
    log(f"[RF] opened {url} -> now at {page.url}")


JS_MENU = """() => Array.from(document.querySelectorAll('a[href]'))
  .filter(a => /\\/c\\d{5,}/.test(a.getAttribute('href') || ''))
  .map(a => ({text: (a.innerText || a.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' '), url: a.href}))"""


def rf_sections(page):
    """Category links of the UAE menu that look like perfumes / offers / gift sets -> [(label, url)]."""
    goto(page, SITE + "/en")
    seen, out = set(), list(RF_EXTRA_SECTIONS)
    seen.update(re.search(r"c\d+", u).group(0) for _, u in out)
    for a in safe_eval(page, JS_MENU):
        cid = (re.search(r"/c(\d{5,})", a["url"]) or [None, None])[1]
        text = a["text"]
        if not cid or "c" + cid in seen or not text:
            continue
        if has_word(RF_MENU_SKIP, text) or not has_word(RF_MENU_WORDS, text):
            log(f"[RF-MENU] skipped section '{text}' {a['url']}")
            continue
        seen.add("c" + cid)
        out.append((text, a["url"].split("?")[0]))
    log(f"[RF-MENU] {len(out)} sections: {[t for t, _ in out]}")
    return out


def _amount(v):
    """Salla price: 170.01 / "170.01" / {"amount": 170.01, "currency": "AED"} -> 170.01 (or None)."""
    if isinstance(v, dict):
        v = v.get("amount")
    try:
        return round(float(str(v).replace(",", "")), 2) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ============================================================ SECTION 3: the product cards on the UAE page
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


def rf_cards(page):
    """Scroll / click 'load more' until the number of cards stops growing, then read every card."""
    last, same = -1, 0
    for _ in range(80):
        n = safe_eval(page, "() => document.querySelectorAll('custom-salla-product-card, salla-product-card').length")
        same = same + 1 if n == last else 0
        if same >= 3:
            break
        last = n
        safe_eval(page, JS_MORE)
        page.wait_for_timeout(2500)
    rows = []
    for c in safe_eval(page, JS_CARDS):
        if not c["url"]:
            continue
        url = c["url"].split("?")[0].split("#")[0]
        pid = int(c["id"]) if c["id"].isdigit() else int(hashlib.md5(url.encode()).hexdigest()[:15], 16)
        rows.append({"id": pid, "url": url, "name": (c["name"] or "").strip(),
                     "sku": None, "price": _amount(c["price"]), "available": not c["out"], "type": None})
    log(f"[RF-CARDS] {len(rows)} product cards read")
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
        raise SystemExit("[RF] FF_DATABASE_URL not set and not found in daily_update.bat")
    return psycopg2.connect(url, sslmode="require")


def save(rows, complete):
    """Rows -> reefperfumes_catalog (one row per Salla product). complete=True -> products not seen = 'Not on site'."""
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(RF_CREATE)
        cur.execute("SELECT NOW()")
        started = cur.fetchone()[0]
        vals = []
        for r in rows:
            p = r["price"]
            vals.append((r["id"], r["url"], None, r["sku"], "Reef Perfumes", r["name"], r["type"], p,
                         round(p / 1.05, 2) if p else None, "In stock" if r["available"] else "Out of stock",
                         rf_volume(r["name"], r["url"].replace("-", " "))))
        execute_values(cur, RF_UPSERT, vals)
        gone = 0
        if complete and vals:
            cur.execute("UPDATE reefperfumes_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (started,))
            gone = cur.rowcount
        conn.commit()
        log(f"[RF-SAVE] {len(vals)} products saved, {gone} marked 'Not on site'")
    finally:
        conn.close()


# ============================================================ SECTION 5: run
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="read + print only, save nothing")
    args = ap.parse_args()
    found, complete, skipped = {}, True, []
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        page = browser.contexts[0].new_page()
        for label, url in rf_sections(page):
            log(f"\n[RF] ===== {label}: {url}")
            goto(page, url)
            rows, ok = rf_cards(page)                            # UAE product cards (AED, ae.reefperfumes.com links)
            kept = [r for r in rows if rf_keep(r["name"])]
            skipped += [r["name"] for r in rows if not rf_keep(r["name"])]
            for r in kept[:3]:
                log(f"[RF-TEST used] {r}")
            log(f"[RF] {label}: {len(rows)} cards, {len(kept)} perfumes / gift sets ({'OK' if ok else 'NOTHING READ'})")
            complete = complete and ok
            for r in kept:
                found[r["id"]] = r                               # a product in several sections is saved once
        page.close()
    log(f"\n[RF] skipped (not perfume): {sorted(set(skipped))[:40]}")
    log(f"[RF] total {len(found)} products | complete read: {complete}")
    if args.test:
        log("[RF] --test: nothing saved")
        return
    if not found:
        log("[RF] nothing read -> nothing saved")
        sys.exit(2)
    save(list(found.values()), complete)
    log("[RF] Done")


if __name__ == "__main__":
    main()

"""
Ounass catalog (ounass.ae fragrance -> Postgres french_fragrance_db, table ounass_catalog)
Plugged into app.py as a Blueprint.

Ounass is a custom website (Al Tayer). Its fragrance category pages already contain every product
(brand, name with size, price, link), 24 per page:
    https://www.ounass.ae/women/beauty/fragrance
    https://www.ounass.ae/men/grooming/fragrance
The way to the next 24 is detected automatically (?p=2  /  ?page=2  /  ?fh_start_index=24).
Unisex perfumes appear in both categories -> saved once (same link).

Same fields as the other catalogs: Product URL, Name, Price Inc Tax, Price, Stock, Volume (+ Brand, SKU).
No barcode on Ounass -> matching is by brand + name + size.

Routes (open in the browser):
  GET /ounass/test?secret=XXX    page 1 + page 2 of women's fragrance, shows what was read (run first)
  GET /ounass/run?secret=XXX     download women + men fragrance into ounass_catalog (background)
  GET /ounass/status             progress
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

ounass_bp = Blueprint("ounass", __name__)

ON_SITE = "https://www.ounass.ae"
ON_CATEGORIES = ["/women/beauty/fragrance", "/men/grooming/fragrance"]
ON_PER_PAGE = 24
ON_MAX_PAGES = 400
ON_DELAY = 1.5                                       # seconds between pages (be gentle)
ON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
# labels Ounass prints above the brand on product cards (removed so only the brand is left)
ON_BADGES = ["GIFT WITH PURCHASE", "MOST DESIRED", "NEW DESIGNER", "ONLINE EXCLUSIVE", "VALUE SET",
             "SUSTAINABLE STEPS", "BEST SELLER", "EXCLUSIVE", "SPONSORED", "LIMITED EDITION", "NEW"]
# ways Ounass might use for "next page" - the first one that returns NEW products is used
ON_PAGE_PARAMS = [("p", lambda n: n), ("page", lambda n: n), ("fh_start_index", lambda n: (n - 1) * ON_PER_PAGE)]

on_lock = threading.Lock()
on_state = {"running": False, "last": None, "last_http": None}


def on_log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- database
ON_CREATE = """
CREATE TABLE IF NOT EXISTS ounass_catalog (
    id            SERIAL PRIMARY KEY,
    product_url   TEXT UNIQUE NOT NULL,     -- https://www.ounass.ae/shop-...-220074852_242.html
    gtin          TEXT,                     -- Ounass shows no barcode -> empty
    sku           TEXT,                     -- Ounass product number from the link, e.g. 220074852
    brand         TEXT,                     -- e.g. "TOM FORD BEAUTY" (as written on Ounass)
    name          TEXT,                     -- e.g. "Ombré Leather Reserve, 100ml"
    price_inc_tax NUMERIC(10,2),            -- price shown (UAE prices include 5% VAT)
    price         NUMERIC(10,2),            -- without VAT (price_inc_tax / 1.05)
    stock         TEXT,                     -- In stock / Out of stock / Not on site
    volume        TEXT,                     -- e.g. "100 ml"
    updated_at    TIMESTAMP DEFAULT NOW()
);
"""

ON_UPSERT = """
INSERT INTO ounass_catalog (product_url, gtin, sku, brand, name, price_inc_tax, price, stock, volume)
VALUES %s
ON CONFLICT (product_url) DO UPDATE SET
    sku = EXCLUDED.sku, brand = EXCLUDED.brand, name = EXCLUDED.name,
    price_inc_tax = EXCLUDED.price_inc_tax, price = EXCLUDED.price,
    stock = EXCLUDED.stock, volume = EXCLUDED.volume, updated_at = NOW();
"""


def on_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    on_log("[ON-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ---------------------------------------------------------------- reading one category page
def clean(text):
    """HTML piece -> plain text (tags removed, &amp; etc. decoded, spaces tidied)."""
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def to_number(s):
    try:
        return round(float(str(s).replace(",", "")), 2)
    except ValueError:
        return None


def on_parse(page_html):
    """Every product card on one page. A card is a link to /shop-...-<number>_242.html;
    its picture's alt text is the product name, the card text is '[badge] Brand Name, size 1,850 AED'."""
    out = {}
    for m in re.finditer(r'<a[^>]+href="((?:https://www\.ounass\.ae)?/shop-[^"]+?-(\d+)_\d+\.html)"[^>]*>(.*?)</a>',
                         page_html, re.S | re.I):
        href, sku, inner = m.group(1), m.group(2), m.group(3)
        url = href if href.startswith("http") else ON_SITE + href
        text = clean(inner)
        if "AED" not in text or url in out:
            continue
        alt = re.search(r'<img[^>]+alt="([^"]+)"', inner)
        name = htmllib.unescape(alt.group(1)).strip() if alt else ""
        pm = re.search(r"([\d,]+(?:\.\d+)?)\s*AED", text)                  # first price = price now
        price_inc = to_number(pm.group(1)) if pm else None

        # brand = text before the name, without the badge ("EXCLUSIVE Amouage" -> "Amouage")
        before = text.split(name)[0].strip() if name and name in text else ""
        for b in ON_BADGES:
            if before.upper().startswith(b + " ") or before.upper() == b:
                before = before[len(b):].strip()
                break
        if not name:                                                        # no picture: "Brand Name 99 AED"
            name = text[:pm.start()].strip() if pm else text
        vol = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*ml\b", name, re.I)
        stock = "Out of stock" if re.search(r"out of stock|sold out", text, re.I) else "In stock"
        out[url] = {"url": url, "sku": sku, "brand": before or None, "name": name,
                    "price_inc_tax": price_inc, "price": round(price_inc / 1.05, 2) if price_inc else None,
                    "stock": stock, "volume": f"{vol.group(1).replace(',', '')} ml" if vol else None}
    return list(out.values())


def total_items(page_html):
    m = re.search(r"Showing\s+[\d,]+\s+of\s+([\d,]+)", clean(page_html), re.I)
    return int(m.group(1).replace(",", "")) if m else None


def on_get(path, params=None):
    """Download one page (retries when busy). Returns HTML or raises when blocked."""
    for attempt, wait in enumerate([0, 10, 30, 90]):
        if wait:
            on_log(f"[ON] {path} {params}: retry {attempt}/3 in {wait}s")
            time.sleep(wait)
        try:
            r = requests.get(ON_SITE + path, params=params or {}, headers=ON_HEADERS, timeout=60)
        except requests.RequestException as ex:
            on_log(f"[ON] network error {ex}")
            continue
        if r.status_code == 429 or r.status_code >= 500:
            on_log(f"[ON] HTTP {r.status_code} (busy)")
            continue
        # remember what came back, so /ounass/test can show it (blocked page? empty page?)
        title = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
        on_state["last_http"] = {"url": r.url, "status": r.status_code, "size": len(r.text),
                                 "title": clean(title.group(1))[:120] if title else None,
                                 "shop_links": len(re.findall(r"/shop-[^\"']+?_\d+\.html", r.text)),
                                 "has_showing": "Showing" in r.text,
                                 "server": r.headers.get("server"), "start": clean(r.text[:1500])[:400]}
        on_log(f"[ON] answer: {on_state['last_http']}")
        if r.status_code in (403, 401) or "captcha" in r.text[:3000].lower():
            raise RuntimeError(f"Ounass is blocking this server (HTTP {r.status_code})")
        return r.text
    return None


def detect_paging(path, first_urls):
    """Try ?p=2, ?page=2, ?fh_start_index=24 - return the first that gives NEW products."""
    for name, value in ON_PAGE_PARAMS:
        body = on_get(path, {name: value(2)}) or ""
        new = [x for x in on_parse(body) if x["url"] not in first_urls]
        on_log(f"[ON] paging test ?{name}={value(2)}: {len(new)} new products")
        # a real page 2 has (almost) all NEW products; a few new ones = page 1 again + rotating/sponsored items
        if len(new) >= ON_PER_PAGE // 2:
            return name, value
        time.sleep(1)
    return None, None


# ---------------------------------------------------------------- full download
def on_run():
    started = time.time()
    st = {"status": "running", "saved": 0, "pages": 0, "categories": {}, "incomplete": False}
    on_state["last"] = st
    conn = on_conn()
    cur = conn.cursor()
    try:
        cur.execute(ON_CREATE)
        cur.execute("SELECT NOW()")
        run_start = cur.fetchone()[0]
        conn.commit()
        seen = set()
        for path in ON_CATEGORIES:
            first = on_get(path)
            if first is None:
                st["incomplete"] = True
                continue
            items = on_parse(first)
            total = total_items(first)
            on_log(f"[ON] {path}: site says {total} products")
            if total and total <= len(items):
                pname, pval = None, None                     # everything is already on page 1
            else:
                pname, pval = detect_paging(path, {x["url"] for x in items})
            if not pname and not (total and total <= len(items)):
                on_log(f"[ON] {path}: could not find how to open page 2 -> only page 1 saved")
                st["incomplete"] = True
            page, cat_saved = 1, 0
            while True:
                rows = []
                for it in items:
                    if it["url"] in seen or not it["name"]:
                        continue
                    seen.add(it["url"])
                    rows.append((it["url"], None, it["sku"], it["brand"], it["name"], it["price_inc_tax"],
                                 it["price"], it["stock"], it["volume"]))
                if rows:
                    execute_values(cur, ON_UPSERT, rows)
                    conn.commit()
                cat_saved += len(rows)
                st["saved"] += len(rows)
                st["pages"] += 1
                st["seconds"] = round(time.time() - started, 1)
                on_log(f"[ON] {path} page {page}: {len(items)} products, {len(rows)} new saved (total {st['saved']})")
                if not pname or not items or page >= ON_MAX_PAGES or (total and page * ON_PER_PAGE >= total):
                    break
                page += 1
                time.sleep(ON_DELAY)
                body = on_get(path, {pname: pval(page)})
                if body is None:
                    st["incomplete"] = True
                    on_log(f"[ON] {path} page {page}: gave up -> stopping this category")
                    break
                items = on_parse(body)
            st["categories"][path] = {"site_total": total, "saved": cat_saved, "paging": pname}
        if not st["incomplete"] and st["saved"]:
            cur.execute("UPDATE ounass_catalog SET stock = 'Not on site', updated_at = NOW() "
                        "WHERE updated_at < %s AND stock <> 'Not on site'", (run_start,))
            st["marked_not_on_site"] = cur.rowcount
            conn.commit()
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        on_log(f"[ON-DONE] {st}")


def on_worker():
    try:
        on_run()
    except Exception as ex:
        on_log(f"[ON-ERROR] {ex}\n{traceback.format_exc()}")
        on_state["last"] = {**(on_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        on_state["running"] = False
        on_lock.release()


# ---------------------------------------------------------------- routes
def on_secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


@ounass_bp.route("/ounass/test")
def on_test_route():
    """Page 1 of women's fragrance + which 'next page' method works. Run this BEFORE /ounass/run."""
    if not on_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    path = ON_CATEGORIES[0]
    try:
        first = on_get(path) or ""
        first_http = on_state["last_http"]
        items = on_parse(first)
        pname, pval = detect_paging(path, {x["url"] for x in items})
        second = on_parse(on_get(path, {pname: pval(2)}) or "") if pname else []
    except RuntimeError as ex:
        on_log(f"[ON-TEST] {ex}")
        return jsonify({"ok": False, "error": str(ex), "what_ounass_returned": on_state["last_http"]})
    result = {"ok": bool(items), "what_ounass_returned": first_http,
              "site_total": total_items(first), "page1_products": len(items),
              "paging_method": pname, "page2_products": len(second),
              "page1_first_3": items[:3], "page2_first_3": second[:3],
              "brands_seen": sorted({x["brand"] or "-" for x in items})[:15]}
    on_log(f"[ON-TEST] {result}")
    return jsonify(result)


@ounass_bp.route("/ounass/run")
def on_run_route():
    if not on_secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    if not on_lock.acquire(blocking=False):
        return jsonify({"error": "Ounass download already running"}), 409
    on_state["running"] = True
    on_log("[ON-RUN] Started")
    threading.Thread(target=on_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/ounass/status"}), 202


@ounass_bp.route("/ounass/status")
def on_status_route():
    return jsonify({"running": on_state["running"], "last": on_state["last"]})
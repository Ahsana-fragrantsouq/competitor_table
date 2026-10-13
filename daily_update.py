r"""
Daily competitor update (runs on YOUR PC Monday-Friday at 11:30 AM - Windows Task Scheduler; to change the time
see START TIME below)
Saturday / Sunday: nothing runs (PC is off; if it is on, the script skips those days unless --force).
-------------------------------------------------------------------------------------------------------------------
EVERY WORKING DAY (Mon-Fri) - French Fragrance, Branded Perfume, Essenzi (their pages must be opened one by one in your Chrome):
    the NEXT 10 BRANDS of the shop's OWN brand list (A -> Z, then again from A): price + stock of ALL their products
    (matched to us or not - "Missing items" too).
    These shops have no brand column -> the brand is read from the start of the product name
    ("Christian Dior Sauvage EDT 100ml" -> "Christian Dior"), using every brand known in the database
    (our brands + Samawa / Amberiya / Ounass brands); unknown -> the first word of the name.
    Branded Perfume + Essenzi are the same products as French Fragrance -> same brand list and same position.

EVERY WORKING DAY after the daily brands (FULL_LISTS_EVERY_DAY = True; False = once a week on the day chosen in
Settings -> Automate) -
Samawa, Amberiya, V Perfumes, Al Injaz, Niche Gallerie, Ounass give their WHOLE product list in one go:
    full download of every product (all brands), then matching to our products on Render.
    NEW PRODUCTS are SAVED (WEEKLY_SAVE_NEW = True) and matched to our products like the others.
    PRODUCTS ALREADY SAVED: only price + stock change - name, brand, GTIN, link, volume ... are put back to what
    they were before the download (WEEKLY_ONLY_PRICE_STOCK).
       Samawa / Amberiya / V Perfumes  -> download runs on Render (/samawa-catalog/run, /amberiya/run, /vperfumes/run)
       Al Injaz / Niche Gallerie / Ounass -> aj_scraper.py / ng_scraper.py / on_scraper.py on this PC (Chrome)
       then /competitor/match-samawa, match-am, match-vp, match-aj, match-ng, match-on on Render (one after another)

EVERY WORKING DAY as well - NEW French Fragrance products (FF_NEW_PRODUCTS = True): French Fragrance's sitemap is
compared with our table; new products are opened and saved, their Branded Perfume + Essenzi links added and opened,
then matched to our products (/competitor/match-ff). Shop pages show "N new products added".

RUN 2 WITHOUT THE PC: Samawa, Amberiya, V Perfumes, Al Injaz, Niche Gallerie, Ounass run on Render every working day
at RUN2_TIME, inside the website (app.py starts run2_scheduler(); no cron job). The PC does no full lists.

At the end Render recalculates least price + suggested price and updates the automatic brands in Airtable.
Settings -> Automate shows: brands per day, weekly day, last run, today's brands, results per shop, next brands.
Progress is saved in table app_settings (key 'daily_update'), so a stopped run continues with the same brands.

BEFORE the first run (once):
  1. Put this file in your scraper folder, next to bp_scraper.py, es_scraper.py, aj_scraper.py, ng_scraper.py,
     on_scraper.py (+ alinjaz.py, nichegallerie.py, ounass.py which those scrapers import).
  2. Edit daily_update.bat (database URL, RUN_SECRET) and create the scheduled task (see daily_update.bat).

Run by hand:
  python daily_update.py --test            -> today's brands, 3 products per shop, PRINT only (nothing saved)
  python daily_update.py                   -> the real run (daily part; weekly part only on the weekly day)
  python daily_update.py --force           -> run today's brands again
  python daily_update.py --weekly          -> run the weekly full lists now (whatever day it is)
  python daily_update.py --no-weekly       -> daily part only
  python daily_update.py --brands "Dior,Chanel"   -> only these brands (does NOT move the daily position)
  python daily_update.py --shop es         -> daily part for one shop only (ff, bp, es; daily position not moved)
Everything is printed AND written to daily_update.log next to this file.
"""

import os
import re
import sys
import json
import time
import argparse
import subprocess
from datetime import datetime, date, timedelta

import requests
import psycopg2

# ---------------------------------------------------------------- settings
# START TIME (11:30 AM, Monday-Friday) is NOT set in this file - Windows Task Scheduler starts daily_update.bat.
#   To change it, open Command Prompt and run (example 11:30 -> 10:00):
#       schtasks /Change /TN "Competitor daily update" /ST 10:00
#   (or Task Scheduler -> Competitor daily update -> Triggers -> Edit -> Start time).
#   The time written on the website is only text: competitors_page.py, search for "START TIME".
SITE = os.environ.get("CT_SITE", "https://competitor-table.onrender.com")   # Render app
CDP_URL = "http://localhost:9222"                                           # your Chrome (remote debugging)
CHROME_EXE = os.environ.get("CHROME_EXE", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
CHROME_PROFILE = os.environ.get("CHROME_PROFILE", r"C:\ff-scraper\chrome-profile")
DEFAULT_PER_DAY = 10           # brands per day when nothing is saved in Settings -> Automate
DEFAULT_WEEKLY_DAY = 4         # 0 = Monday ... 4 = Friday (Settings -> Automate; weekends are never used)
WORK_DAYS = range(0, 5)        # Monday-Friday: the PC is off on Saturday and Sunday
DELAY = 1.5                    # seconds between product pages (be gentle with the shops)
STATE_KEY = "daily_update"     # app_settings key: positions, today's brands, results
PER_DAY_KEY = "daily_brands_per_day"
WEEKLY_DAY_KEY = "daily_weekly_day"
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "daily_update.log")

# Daily shops (page by page). All three use the French Fragrance product list -> one brand list / position.
DAILY_SHOPS = {
    "ff": {"label": "French Fragrance", "table": "french_fragrance_catalog", "reader": "bp_scraper"},
    "bp": {"label": "Branded Perfume", "table": "branded_perfume_catalog", "reader": "bp_scraper"},
    "es": {"label": "Essenzi", "table": "essenzi_catalog", "reader": "es_scraper"},
}
# Weekly shops (whole list in one go): where the download runs + the match link on Render
# WEEKLY_SAVE_NEW = True  -> NEW products of the shop (not in our table yet) are SAVED and then matched to our products.
#                   False -> new rows made by the download are removed again right after it (only current products).
WEEKLY_SAVE_NEW = True
# FULL_LISTS_EVERY_DAY = True -> the full lists of the 6 shops (new products + price / stock) run EVERY working day
#                               after the daily brands, not only on the weekly day. False = only on the weekly day.
FULL_LISTS_EVERY_DAY = True
# WEEKLY_ONLY_PRICE_STOCK = True -> for products ALREADY saved, only price + stock change: name, brand, GTIN, link,
# volume ... are put back to what they were before the download (new products keep everything they came with).
WEEKLY_ONLY_PRICE_STOCK = True
# columns the download is allowed to change on products already saved:
WEEKLY_UPDATE_COLUMNS = {"price", "price_inc_tax", "stock", "updated_at"}
WEEKLY_TABLES = {"samawa": "samawa_catalog", "am": "amberiya_catalog", "vp": "vperfumes_catalog",
                 "aj": "alinjaz_catalog", "ng": "nichegallerie_catalog", "on": "ounass_catalog"}
# RUN 2 WITHOUT THE PC: runs inside the website on Render at RUN2_TIME (Mon-Fri, see RUN 2 START TIME below)
# downloads ALL 6 shops on Render. If a shop ever blocks Render (Settings -> Automate shows an error for it), move its
# key to PC_FULL_LISTS: the PC run then downloads it in your Chrome (aj_scraper.py / ng_scraper.py / on_scraper.py).
RENDER_FULL_LISTS = ["samawa", "am", "vp", "aj", "ng", "on"]   # Run 2 on Render (no PC)
PC_FULL_LISTS = []                                             # empty = the PC does no full lists at all
RENDER_STATE_KEY = "full_lists"                     # app_settings key for the Render run (separate from the PC run)
# RUN 2 START TIME (India time, Monday-Friday) - CHANGE IT HERE, then push to GitHub + deploy:
RUN2_TIME = "17:35"            # "HH:MM", 24-hour India time. Example: "06:30", "09:00", "14:00"
# RUN 2 runs INSIDE the website on Render (no cron job, no PC): app.py starts run2_scheduler() below when the
# website starts; it checks the time every minute and runs the full lists once each working day at RUN2_TIME.
# By hand (e.g. to test): python daily_update.py --full-lists --force
WEEKLY_SHOPS = [
    # (key, label, Render download link or None, PC scraper or None, Render status link, match link)
    ("samawa", "Samawa", "/samawa-catalog/run", None, "/samawa-catalog/status", "/competitor/match-samawa"),
    ("am", "Amberiya", "/amberiya/run", None, "/amberiya/status", "/competitor/match-am"),
    ("vp", "V Perfumes", "/vperfumes/run", None, "/vperfumes/status", "/competitor/match-vp"),
    ("aj", "Al Injaz", "/alinjaz/run", None, "/alinjaz/status", "/competitor/match-aj"),             # Render
    ("ng", "Niche Gallerie", "/nichegallerie/run", None, "/nichegallerie/status", "/competitor/match-ng"),   # Render
    ("on", "Ounass", "/ounass/run", None, "/ounass/status", "/competitor/match-on"),                 # Render
]


def log(*a):
    """Print + append to daily_update.log (the scheduled task has no window, so the file is the log)."""
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- database
_db_logged = False      # the connection message is printed only once (db() is called many times)
DB_TRIES = 20           # connection tries before giving up (internet / DNS drop)
DB_WAIT = 30            # seconds between tries -> up to 10 minutes of waiting


def db():
    """Open a connection to french_fragrance_db on Render (FF_DATABASE_URL is set in daily_update.bat)."""
    global _db_logged
    url = os.environ.get("FF_DATABASE_URL", "")
    if not url or "..." in url:
        log("[DB] FF_DATABASE_URL is missing or still '...' -> fill it in daily_update.bat")
        raise SystemExit('Set the database first:  set "FF_DATABASE_URL=postgresql://...render.com/french_fragrance_db"')
    if not _db_logged:
        log(f"[DB] Connecting to {url.split('@')[-1]}")       # host + database only (never the password)
        _db_logged = True
    # internet / DNS can drop for a moment ("could not translate host name") -> try again for up to ~10 minutes
    for attempt in range(1, DB_TRIES + 1):
        try:
            return psycopg2.connect(url, sslmode="require", connect_timeout=30)
        except psycopg2.OperationalError as ex:
            if attempt == DB_TRIES:
                log(f"[DB] Could not connect after {DB_TRIES} tries - giving up: {str(ex).strip()[:150]}")
                raise
            log(f"[DB] Connection failed (try {attempt}/{DB_TRIES}): {str(ex).strip()[:120]} "
                f"-> internet / DNS problem? waiting {DB_WAIT}s and trying again")
            time.sleep(DB_WAIT)


def read_setting(cur, k):
    """One value from app_settings (saved by Settings -> Automate or by this script), or None."""
    # the table is normally created by Render; created here too so a first run on an empty database works
    cur.execute("""CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT, updated_by TEXT,
                                                             updated_at TIMESTAMP DEFAULT NOW())""")
    cur.execute("SELECT value FROM app_settings WHERE key = %s", (k,))
    row = cur.fetchone()
    value = row[0] if row else None
    shown = (value[:120] + "...") if value and len(value) > 120 else value
    log(f"[SETTINGS] {k} = {shown if value is not None else '(not saved yet -> default)'}")
    return value


def write_state(state):
    """Save progress / results so Settings -> Automate can show them (and a stopped run can continue).
    A database problem here is logged, never stops the run (the prices are already saved)."""
    try:
        _write_state(state)
    except Exception as ex:
        log(f"[STATE] Could not save progress (run continues): {str(ex).strip()[:150]}")


def _write_state(state):
    conn = db()
    cur = conn.cursor()
    cur.execute("""INSERT INTO app_settings (key, value, updated_by) VALUES (%s, %s, 'daily_update.py')
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                                                   updated_at = NOW()""", (STATE_KEY, json.dumps(state)))   # PC or Render key
    conn.commit()
    cur.close()
    conn.close()
    log(f"[STATE] Saved: daily {state.get('daily_status', '-')} ({', '.join(state.get('brands') or [])}) | "
        f"weekly {state.get('weekly_status', '-')}")


def table_exists(cur, t):
    """True when the table is in the database (a shop that was never downloaded has no table yet)."""
    cur.execute("SELECT to_regclass(%s)", (t,))
    ok = cur.fetchone()[0] is not None
    if not ok:
        log(f"[DB] Table {t} does not exist (yet)")
    return ok


# ---------------------------------------------------------------- brand of a French Fragrance product
def norm(s):
    """Text for comparing names: lower case, only letters / numbers / &, single spaces.
    'Dolce & Gabbana - Light Blue' -> 'dolce & gabbana light blue'. (No log here: called for every product.)"""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9&]+", " ", (s or "").lower())).strip()


def known_brands(cur):
    """Every brand written anywhere in the database: ours + Samawa / Amberiya / Ounass brand columns."""
    names = set()
    for t, col in (("competitor_table", "brand"), ("samawa_catalog", "brand"), ("amberiya_catalog", "brand"),
                   ("ounass_catalog", "brand")):
        if table_exists(cur, t):
            cur.execute(f"SELECT DISTINCT TRIM({col}) FROM {t} WHERE {col} IS NOT NULL AND TRIM({col}) <> ''")
            found = {b for (b,) in cur.fetchall()}
            log(f"[BRANDS] {len(found)} brands in {t}")
            names |= found
    # Ounass writes "Tom Ford Beauty", "Ajmal Perfumes" -> also keep the name without that word
    names |= {re.sub(r"\s+(beauty|fragrances?|perfumes?|parfums?|cosmetics)$", "", b, flags=re.I) for b in names}
    by_norm = {}
    # sorted(): the SAME spelling is chosen every run ("Acqua Di Parma" vs "Acqua di Parma" used to change
    # from run to run, so a continued run could not find a brand saved with the other spelling)
    for b in sorted(names):
        n = norm(b)
        if len(n) >= 2:
            by_norm.setdefault(n, b)                        # first spelling (alphabetical) is shown
    log(f"[BRANDS] {len(by_norm)} different brand names known (all tables together)")
    return by_norm


def brand_of(name, by_norm, longest):
    """'Christian Dior Sauvage EDT' -> 'Christian Dior' (longest known brand at the START of the name),
    else the first word of the name ('Zimaya Phantom ...' -> 'Zimaya')."""
    # No log here (called for every French Fragrance product); ff_brand_groups() prints a summary.
    n = norm(name)
    words = n.split(" ")
    for k in range(min(longest, len(words)), 0, -1):         # longest brand first: 'Christian Dior' before 'Christian'
        cand = " ".join(words[:k])
        if cand in by_norm:
            return by_norm[cand]
    first = (name or "").strip().split(" ")[0] if name else ""
    return first.title() if first else "(no name)"


def ff_brand_groups(cur):
    """French Fragrance products grouped by brand: {brand: [french_fragrance_catalog.id, ...]}"""
    by_norm = known_brands(cur)
    longest = max((len(k.split(" ")) for k in by_norm), default=1)
    cur.execute("SELECT id, name FROM french_fragrance_catalog")
    groups = {}
    for fid, name in cur.fetchall():
        groups.setdefault(brand_of(name, by_norm, longest), []).append(fid)
    log(f"[BRANDS] {len(by_norm)} known brand names -> French Fragrance products are in {len(groups)} brands")
    known = {v for v in by_norm.values()}
    guessed = [b for b in groups if b not in known]            # brand = first word of the name (not in any table)
    biggest = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:5]
    log(f"[BRANDS] {len(groups) - len(guessed)} brands recognised, {len(guessed)} taken from the first word of the name")
    log(f"[BRANDS] Biggest brands: " + ", ".join(f"{b} ({len(ids)})" for b, ids in biggest))
    return groups


# ---------------------------------------------------------------- which brands today
def pick_next(all_brands, last, per_day):
    """Next per_day brands after `last` (alphabetical), wrapping to A at the end."""
    # brands alphabetically AFTER the last one done; when fewer than per_day are left, continue from A
    after = [b for b in all_brands if b.lower() > (last or "").lower()] if last else all_brands
    picked = list(dict.fromkeys((after + all_brands)[:per_day]))      # dict.fromkeys = no brand twice
    log(f"[ROTATION] After '{last or '(start)'}': {len(after)} brands left in this round -> next {len(picked)}: "
        f"{', '.join(picked)}")
    return picked, len(after) < per_day


def todays_brands(cur, groups, state, per_day, force):
    """Same day again -> the SAME brands (a stopped run continues; a finished run is skipped unless --force)."""
    today = date.today().isoformat()
    all_brands = sorted(groups, key=str.lower)
    if state.get("date") == today and state.get("brands"):
        if state.get("daily_status") == "done" and not force:
            log(f"[DAILY] Today's brands are already done ({', '.join(state['brands'])}). Use --force to run again.")
            return None
        log(f"[DAILY] Continuing today's brands: {', '.join(state['brands'])}")
        return state["brands"]
    picked, wrapped = pick_next(all_brands, state.get("last_brand"), per_day)
    log(f"[DAILY] {len(all_brands)} French Fragrance brands, {per_day} per working day -> one full round every "
        f"{-(-len(all_brands) // per_day)} working days (Mon-Fri)")
    log(f"[DAILY] Last brand done: {state.get('last_brand') or '(none - starting at A)'}"
        + (" -> end of the list reached, starting again at A" if wrapped else ""))
    state.update({"date": today, "brands": picked, "per_day": per_day, "daily_status": "running",
                  "shops": {}, "total_brands": len(all_brands)})
    return picked


# ---------------------------------------------------------------- one daily shop
def todo_for(cur, key, ids):
    """Rows to open for one shop: (row id, link) of the French Fragrance products of today's brands."""
    t = DAILY_SHOPS[key]["table"]
    if not table_exists(cur, t):
        log(f"[{key.upper()}] table {t} does not exist -> skipped")
        return []
    if key == "ff":
        cur.execute("SELECT id, product_url FROM french_fragrance_catalog WHERE id = ANY(%s) AND product_url IS NOT NULL "
                    "ORDER BY id", (ids,))
    else:      # Branded Perfume / Essenzi: same products, linked by ff_id
        cur.execute(f"SELECT id, {key}_url FROM {t} WHERE ff_id = ANY(%s) AND {key}_url IS NOT NULL ORDER BY id", (ids,))
    rows = cur.fetchall()
    log(f"[{key.upper()}] {len(rows)} links found in {t} for {len(ids)} French Fragrance products of today's brands")
    return rows


def old_values(cur, key, rid):
    """(price shown incl. VAT, stock) saved before for this row - used for the price check and the log."""
    if key == "ff":
        cur.execute("SELECT price_inc_tax, stock FROM french_fragrance_catalog WHERE id = %s", (rid,))
    else:
        t = DAILY_SHOPS[key]["table"]
        cur.execute(f"SELECT {key}_price_inc_tax, {key}_stock FROM {t} WHERE id = %s", (rid,))
    row = cur.fetchone()
    return (float(row[0]) if row and row[0] is not None else None), (row[1] if row else None)


def check_price(key, url, r, old_price):
    """Price safety check, so a wrongly read price is never saved.
    French Fragrance writes some prices like '150,00' -> read as 15000 (100x too big). So:
      new price ~100x the saved one        -> divided by 100 (logged as PRICE-FIX)
      new price > 5x or < 1/5 of saved one -> NOT saved, the saved price is kept (logged as PRICE-CHECK)
      no saved price yet and new >= 10,000 -> same 100x check is not possible, but a perfume never costs that
                                              much on these shops -> divided by 100 when that gives >= 10 AED"""
    # Root cause on French Fragrance: the page shows "AED147.00" with small raised decimals -> page text "AED14700",
    # so the inc-tax price read from the text is 100x. The price WITHOUT VAT (from the page's product data) is right,
    # so when inc-tax is far above it, inc-tax = price + 5% VAT.
    if r.get("price") and r.get("price_inc_tax") and float(r["price_inc_tax"]) > float(r["price"]) * 2:
        fixed = round(float(r["price"]) * 1.05, 2)
        log(f"[{key.upper()}-PRICE-FIX] {url}: inc-tax read as AED {r['price_inc_tax']}, price without VAT "
            f"AED {r['price']} -> inc-tax AED {fixed}")
        r = {**r, "price_inc_tax": fixed}
    new = r.get("price_inc_tax")
    if new is None:
        return r
    new = float(new)
    if old_price:
        ratio = new / old_price
        if 80 <= ratio <= 120:                                   # exactly the 100x mistake
            log(f"[{key.upper()}-PRICE-FIX] {url}: read AED {new}, saved AED {old_price} -> using AED {round(new / 100, 2)}")
            return {**r, "price_inc_tax": round(new / 100, 2),
                    "price": round(float(r["price"]) / 100, 2) if r.get("price") else None}
        if ratio > 5 or ratio < 0.2:
            log(f"[{key.upper()}-PRICE-CHECK] {url}: read AED {new}, saved AED {old_price} - too different, "
                f"price NOT changed (stock is still updated)")
            return {**r, "price_inc_tax": None, "price": None}
    elif new >= 10000 and new / 100 >= 10:
        log(f"[{key.upper()}-PRICE-FIX] {url}: read AED {new} (no saved price) -> using AED {round(new / 100, 2)}")
        return {**r, "price_inc_tax": round(new / 100, 2),
                "price": round(float(r["price"]) / 100, 2) if r.get("price") else None}
    return r


def save_one(cur, key, rid, url, r):
    """New price / stock -> the shop's own table (+ competitor_table for French Fragrance). True = something changed.
    Branded Perfume / Essenzi prices reach competitor_table through update-least at the end."""
    t = DAILY_SHOPS[key]["table"]
    if key == "ff":
        cur.execute("SELECT price_inc_tax, stock FROM french_fragrance_catalog WHERE id = %s", (rid,))
        old = cur.fetchone()
        cur.execute("""UPDATE french_fragrance_catalog SET price_inc_tax = COALESCE(%s, price_inc_tax),
                              price = COALESCE(%s, price), stock = %s, updated_at = NOW() WHERE id = %s""",
                    (r.get("price_inc_tax"), r.get("price"), r["stock"], rid))
        cur.execute("""UPDATE competitor_table SET ff_price = COALESCE(%s, ff_price), ff_stock = %s, updated_at = NOW()
                       WHERE ff_link = %s""", (r.get("price_inc_tax"), r["stock"] == "In stock", url))
    else:
        cur.execute(f"SELECT {key}_price_inc_tax, {key}_stock FROM {t} WHERE id = %s", (rid,))
        old = cur.fetchone()
        # COALESCE: an empty price (not read / refused by the price check) keeps the saved price
        cur.execute(f"""UPDATE {t} SET {key}_price = COALESCE(%s, {key}_price),
                               {key}_price_inc_tax = COALESCE(%s, {key}_price_inc_tax), {key}_stock = %s, {key}_status = %s,
                               {key}_title = COALESCE(%s, {key}_title), {key}_size = COALESCE(%s, {key}_size),
                               checked_at = NOW(), updated_at = NOW() WHERE id = %s""",
                    (r.get("price"), r.get("price_inc_tax"), r["stock"], r["status"], r.get("title"), r.get("size"), rid))
    if not old:
        log(f"[{key.upper()}-SAVE] row {rid} not found in {t} - nothing saved")
        return False
    # changed = new price differs (an empty new price never counts) OR stock text differs
    old_price = float(old[0]) if old[0] is not None else None
    new_price = r.get("price_inc_tax")
    changed = (new_price is not None and old_price != float(new_price)) or old[1] != r["stock"]
    if changed:
        log(f"[{key.upper()}-SAVE] {url}: AED {old_price} / {old[1]}  ->  AED {new_price} / {r['stock']}")
    return changed


def run_daily_shop(key, ids, test, get_page):
    """Open every product page of today's brands in ONE shop and save price + stock."""
    s = DAILY_SHOPS[key]
    st = {"checked": 0, "changed": 0, "gone": 0, "errors": 0}
    conn = db()
    cur = conn.cursor()
    try:
        # Branded Perfume / Essenzi: make sure the title + size columns exist (older tables did not have them)
        if s["table"] != "french_fragrance_catalog" and table_exists(cur, s["table"]):
            cur.execute(f"ALTER TABLE {s['table']} ADD COLUMN IF NOT EXISTS {key}_title TEXT; "
                        f"ALTER TABLE {s['table']} ADD COLUMN IF NOT EXISTS {key}_size TEXT")
            conn.commit()
        todo = todo_for(cur, key, ids)
        if test:
            todo = todo[:3]
        log(f"\n[{key.upper()}] {s['label']}: {len(todo)} products of today's brands")
        if not todo:
            return st
        reader = __import__(s["reader"])                    # bp_scraper / es_scraper: same page reading as before
        m = re.match(r"https?://[^/]+", todo[0][1])             # shop's home page, e.g. https://essenzi.com
        log(f"[{key.upper()}] Page reader: {s['reader']}.py | site {m.group(0) if m else '?'}")
        page = get_page(m.group(0) if m else None, reader)
        errors_in_row = 0
        for i, (rid, url) in enumerate(todo, 1):
            try:
                r = reader.scrape_product(page, url)
                errors_in_row = 0
            except Exception as ex:
                st["errors"] += 1
                errors_in_row += 1
                log(f"[{key.upper()}] {i}/{len(todo)} ERROR {url}: {str(ex).splitlines()[0][:150]}")
                if errors_in_row >= 10:
                    log(f"[{key.upper()}] 10 errors in a row - probably blocked. Stopping this shop for today.")
                    break
                time.sleep(DELAY)
                continue
            st["checked"] += 1
            st["gone"] += r["status"] != "ok"             # page gone from the shop -> saved as "Not on site"
            for db_try in (1, 2):                         # database connection lost -> reconnect once and save again
                try:
                    old_price, old_stock = old_values(cur, key, rid)
                    r2 = check_price(key, url, r, old_price)          # wrongly read price -> fixed or not saved
                    if test:
                        log(f"[TEST] {i}/{len(todo)} {(r2.get('title') or url)[:60]} -> AED {r2.get('price_inc_tax')} | "
                            f"{r2['stock']}  (saved now: AED {old_price} | {old_stock}) (not saved)")
                    else:
                        changed = save_one(cur, key, rid, url, r2)
                        conn.commit()                    # saved one by one: a stop never loses checked products
                        st["changed"] += changed
                        log(f"[{key.upper()}] {i}/{len(todo)} {(r2.get('title') or url)[:50]} | "
                            f"AED {r2.get('price_inc_tax') or '-'} | {r2['stock']}{'  <- CHANGED' if changed else ''}")
                    break
                except (psycopg2.OperationalError, psycopg2.InterfaceError) as ex:
                    log(f"[{key.upper()}] {i}/{len(todo)} database connection lost: {str(ex).strip()[:120]} "
                        + ("-> reconnecting" if db_try == 1 else "-> product skipped"))
                    try:
                        conn.close()
                    except Exception:
                        pass
                    if db_try == 2:
                        st["errors"] += 1
                        conn = db()                      # fresh connection for the next product
                        cur = conn.cursor()
                        break
                    conn = db()                          # waits up to 10 minutes for the internet to come back
                    cur = conn.cursor()
            time.sleep(DELAY)
    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
    log(f"[{key.upper()} DONE] {st}")
    return st


# ---------------------------------------------------------------- Render calls
def render_get(path, tries=6):
    """GET a Render run / status link with the secret. 409 (busy) / no answer -> wait 2 min and try again."""
    secret = os.environ.get("RUN_SECRET", "")
    if not secret:
        log(f"[RENDER] RUN_SECRET not set -> cannot open {path}")
        return None
    for attempt in range(1, tries + 1):
        try:
            r = requests.get(SITE + path, params={"secret": secret}, timeout=120)   # Render may need ~1 min to wake up
            if r.status_code != 409:
                if not path.endswith("/status"):                  # status links are polled often -> not logged
                    log(f"[RENDER] GET {path} -> HTTP {r.status_code}")
                return r
            log(f"[RENDER] {path}: busy (another run) - waiting 2 min ({attempt}/{tries})")
        except requests.RequestException as ex:
            log(f"[RENDER] {path}: {str(ex)[:120]} - waiting 2 min ({attempt}/{tries})")   # Render asleep / no internet
        time.sleep(120)
    return None


def wait_render(status_path, label, max_minutes=180):
    """Wait until a Render job says running = false."""
    started = time.time()
    checks = 0
    while time.time() - started < max_minutes * 60:
        time.sleep(30)                                   # ask every 30 seconds
        checks += 1
        if checks % 10 == 0:                             # every 5 minutes: show it is still waiting
            log(f"[RENDER] {label}: still running ({round((time.time() - started) / 60)} min)")
        r = render_get(status_path, tries=3)
        try:
            data = r.json() if r is not None else {}
        except ValueError:
            data = {}
        if r is not None and not data.get("running"):
            last = data.get("last") or data.get("last_summary") or {}
            log(f"[RENDER] {label} finished: {json.dumps(last)[:300]}")
            return last
    log(f"[RENDER] {label}: still running after {max_minutes} min - continuing without waiting")
    return {"error": "timeout"}


def start_and_wait(path, status_path, label):
    """Start a Render job (download / match) and wait until it has finished. 202 = started."""
    log(f"[RENDER] Starting {label}: {path}")
    r = render_get(path)
    if r is None or r.status_code != 202:
        log(f"[RENDER] {label} not started: {r.status_code if r is not None else 'no answer'} "
            f"{(r.text[:150] if r is not None else '')}")
        return {"error": f"not started ({r.status_code if r is not None else 'no answer'})"}
    log(f"[RENDER] {label} started")
    return wait_render(status_path, label)


# ---------------------------------------------------------------- weekly full lists
def weekly_due(cur, state, forced):
    """Due on the weekly day (Settings -> Automate) once; if the PC was off that day -> the next run catches up."""
    try:
        day = int(read_setting(cur, WEEKLY_DAY_KEY) or DEFAULT_WEEKLY_DAY)
    except ValueError:
        day = DEFAULT_WEEKLY_DAY
    if day not in WORK_DAYS:
        day = DEFAULT_WEEKLY_DAY                         # an old Saturday / Sunday setting -> Friday
    last = state.get("weekly_date")
    today = date.today()
    since = (today - date.fromisoformat(last)).days if last else None
    if FULL_LISTS_EVERY_DAY:                             # every working day, once (a finished day is not repeated)
        due = forced or last is None or since > 0
        log(f"[WEEKLY] Full lists EVERY working day | last full lists: {last or 'never'} -> "
            f"{'RUN TODAY' if due else 'already done today'}")
        return due
    due = forced or last is None or (today.weekday() == day and since > 0) or since >= 8
    log(f"[WEEKLY] Weekly day: {DAYS[day]} | last full lists: {last or 'never'} -> "
        f"{'RUN TODAY' if due else 'not today'}")
    return due


def max_row_id(table):
    """Highest row number in a shop's table before its download (0 = table empty / missing)."""
    conn = db()
    cur = conn.cursor()
    try:
        if not table_exists(cur, table):
            return 0
        cur.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}")
        return cur.fetchone()[0]
    finally:
        cur.close()
        conn.close()


def remove_new_products(table, label, before_id):
    """WEEKLY_SAVE_NEW = False only: rows added by this download (row number above the old highest) are deleted again.
    WEEKLY_SAVE_NEW = True: new products are kept -> only counted and logged."""
    if WEEKLY_SAVE_NEW:
        conn = db()
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table} WHERE id > %s", (before_id or 0,))
            n = cur.fetchone()[0]
        finally:
            cur.close()
            conn.close()
        log(f"[WEEKLY] {label}: {n} NEW products SAVED (they are matched to our products next)")
        return n
    if not before_id:
        log(f"[WEEKLY] {label}: table was empty before -> nothing removed (first download keeps everything)")
        return 0
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(f"DELETE FROM {table} WHERE id > %s", (before_id,))
        n = cur.rowcount
        conn.commit()
    finally:
        cur.close()
        conn.close()
    log(f"[WEEKLY] {label}: {n} NEW products removed again (only current products are kept and updated)")
    return n


def keep_table(table):
    return f"weekly_keep_{table}"


def restore_details(table, label):
    """Put back every column except price / stock from the copy made before the download. Returns rows restored."""
    keep = keep_table(table)
    conn = db()
    cur = conn.cursor()
    try:
        if not table_exists(cur, keep):
            return 0
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position",
                    (table,))
        cols = [c for (c,) in cur.fetchall() if c != "id" and c not in WEEKLY_UPDATE_COLUMNS]
        n = 0
        if cols:
            sets = ", ".join(f"{c} = k.{c}" for c in cols)
            differs = " OR ".join(f"t.{c} IS DISTINCT FROM k.{c}" for c in cols)
            # only rows where a detail really changed are written back
            cur.execute(f"UPDATE {table} t SET {sets} FROM {keep} k WHERE t.id = k.id AND ({differs})")
            n = cur.rowcount
        cur.execute(f"DROP TABLE {keep}")
        conn.commit()
        log(f"[WEEKLY] {label}: only price + stock kept from the download - other details put back on {n} products "
            f"({', '.join(cols)})")
        return n
    finally:
        cur.close()
        conn.close()


def keep_details(table, label):
    """Copy of the shop's table before the download, so names / brands / GTINs / links can be put back after it."""
    keep = keep_table(table)
    restore_details(table, label)                        # a copy left by a stopped run -> put it back first
    conn = db()
    cur = conn.cursor()
    try:
        if not table_exists(cur, table):
            return False
        cur.execute(f"CREATE TABLE {keep} AS SELECT * FROM {table}")
        n = cur.rowcount
        conn.commit()
        log(f"[WEEKLY] {label}: copy of {n} products made ({keep}) - after the download only price + stock change")
        return True
    finally:
        cur.close()
        conn.close()


def run_weekly(state, only=None):
    """Whole product list of the 6 shops, then matching on Render (one shop after another).
    New products are saved (WEEKLY_SAVE_NEW); products already saved only get new price + stock (WEEKLY_ONLY_PRICE_STOCK)."""
    results = dict(state.get("weekly_results") or {})   # keeps French Fragrance / BP / Essenzi "new products" info
    env = dict(os.environ)
    for key, label, dl, scraper, status, match in WEEKLY_SHOPS:
        if only is not None and key not in only:        # Render run: samawa/am/vp/ng | PC run: aj/on
            continue
        log(f"\n[WEEKLY] ===== {label}: full product list =====")
        table = WEEKLY_TABLES[key]
        before_id = max_row_id(table)                    # everything above this after the download = new products
        log(f"[WEEKLY] {label}: highest row in {table} before the download = {before_id}")
        if WEEKLY_ONLY_PRICE_STOCK:
            keep_details(table, label)                   # names / brands / GTINs / links are put back afterwards
        if dl:
            res = start_and_wait(dl, status, f"{label} download")
        else:
            path = os.path.join(HERE, scraper)
            if not os.path.exists(path):
                log(f"[WEEKLY] {scraper} not found next to daily_update.py -> {label} skipped")
                results[label] = {"error": f"{scraper} missing"}
                continue
            # PC scraper (aj_scraper.py / ng_scraper.py / on_scraper.py) - same as running it by hand
            log(f"[WEEKLY] Running {scraper} (this can take a while, its output is summarised below)")
            started = time.time()
            p = subprocess.run([sys.executable, path], cwd=HERE, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            tail = [ln for ln in (p.stdout or "").splitlines() if ln.strip()][-3:]
            for ln in tail:
                log(f"[WEEKLY] {scraper}: {ln}")
            if p.returncode != 0:
                log(f"[WEEKLY] {scraper} failed: {(p.stderr or '')[-300:]}")
            res = {"exit": p.returncode, "minutes": round((time.time() - started) / 60, 1), "last_line": tail[-1:] }
        n_new = remove_new_products(table, label, before_id)   # WEEKLY_SAVE_NEW = True -> kept (and counted)
        if WEEKLY_SAVE_NEW:
            # shown on the shop's page: "N new products added" (+ filter: rows with id above new_after_id)
            res["new_added"], res["new_after_id"], res["date"] = n_new, before_id or 0, date.today().isoformat()
        else:
            res["new_removed"] = n_new
        if WEEKLY_ONLY_PRICE_STOCK:
            res["details_restored"] = restore_details(table, label)          # only price + stock changed
        log(f"[WEEKLY] {label}: matching to our products on Render")
        res["match"] = start_and_wait(match, "/samawa/status", f"{label} match")      # all matches use /samawa/status
        results[label] = res
        state["weekly_results"] = results
        write_state(state)
    return results


# ---------------------------------------------------------------- NEW French Fragrance products (every working day)
# French Fragrance's sitemap = the list of ALL its product links. Links not in french_fragrance_catalog yet are NEW:
#   1. open each new page (same reading as the daily run) -> name, GTIN, price, stock, size -> saved in our table
#   2. the same product on Branded Perfume + Essenzi = same link on their domain -> rows added + their pages opened
#   3. /competitor/match-ff on Render -> new French Fragrance products are matched to our products
# The shop pages then show "N new products added" (French Fragrance, Branded Perfume, Essenzi).
FF_NEW_PRODUCTS = True          # False = never look for new French Fragrance products
FF_SITE = "https://frenchfragrance.com"
FF_CATEGORIES = ["perfumes", "gift-sets"]      # same folders as ff_scraper.py (perfumes + gift sets only)
FF_NEW_MAX_PER_DAY = 300        # at most this many new products a day (the rest come the next day)
SISTERS = {"bp": ("branded_perfume_catalog", "brandedperfume.com", "Branded Perfume"),
           "es": ("essenzi_catalog", "essenzi.com", "Essenzi")}


def url_key(u):
    """Link compared without http(s), www, capitals or the last '/'."""
    return re.sub(r"^https?://(www\.)?", "", (u or "").strip().lower()).rstrip("/")


def ff_sitemap_urls(page):
    """All French Fragrance product links from sitemap.xml (+ its product sitemaps), English pages, chosen folders."""
    import bp_scraper
    todo, seen, urls = [f"{FF_SITE}/sitemap.xml"], set(), set()
    while todo:
        sm = todo.pop()
        if sm in seen:
            continue
        seen.add(sm)
        try:
            page.goto(sm, wait_until="domcontentloaded", timeout=90000)
        except Exception as ex:
            raise ConnectionError(f"sitemap {sm}: {str(ex).splitlines()[0][:100]}")
        if not bp_scraper.wait_cloudflare(page):
            raise RuntimeError("Cloudflare block on the sitemap")
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", page.content())
        if not locs:                                     # Chrome sometimes shows the XML as plain text
            locs = re.findall(r"https?://frenchfragrance\.com/[^\s<\"']+", page.inner_text("body"))
        for u in locs:
            u = u.replace("&amp;", "&")
            if u.endswith(".xml"):
                if re.search(r"/products\d*\.xml$", u):  # only product sitemaps (no images / blog / categories)
                    todo.append(u)
            else:
                urls.add(u)
        log(f"[FF-NEW] Sitemap {sm}: {len(locs)} links (total pages so far {len(urls)})")
    products = sorted(u for u in urls
                      if not re.search(r"frenchfragrance\.com/[a-z]{2}/", u)          # no /ar/ copies
                      and any(f"frenchfragrance.com/{c}/" in u for c in FF_CATEGORIES))
    log(f"[FF-NEW] {len(products)} products on French Fragrance (folders {', '.join(FF_CATEGORIES)})")
    return products


def ff_gtin(page):
    """Barcode of the open French Fragrance page: JSON-LD gtin13/gtin..., else a barcode-like SKU, else 'GTIN: ...'."""
    import bp_scraper
    try:
        blocks = page.eval_on_selector_all('script[type="application/ld+json"]', "els => els.map(e => e.textContent)")
        jd = bp_scraper.find_product_jsonld(blocks) or {}
        for k in ("gtin13", "gtin", "gtin14", "gtin12", "gtin8"):
            if jd.get(k):
                return str(jd[k]).strip()
        if re.fullmatch(r"\d{8,14}", str(jd.get("sku", ""))):
            return str(jd["sku"])
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page.content()))
        m = re.search(r"(?:GTIN|EAN|Barcode)[^0-9]{0,40}(\d{8,14})", text, re.I)
        return m.group(1) if m else None
    except Exception:
        return None


def run_ff_new(get_page, state, test):
    """Find + save new French Fragrance products (and their Branded Perfume / Essenzi rows). Returns a result dict."""
    import bp_scraper
    res = {"new_found": 0, "added": 0, "errors": 0}
    log("\n[FF-NEW] ===== New French Fragrance products =====")
    page = get_page(FF_SITE, bp_scraper)
    urls = ff_sitemap_urls(page)
    if not urls:
        log("[FF-NEW] No products in the sitemap - nothing done (check FF_CATEGORIES)")
        return {**res, "error": "sitemap empty"}
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("SELECT product_url FROM french_fragrance_catalog WHERE product_url IS NOT NULL")
        have = {url_key(u) for (u,) in cur.fetchall()}
    finally:
        cur.close()
        conn.close()
    new = [u for u in urls if url_key(u) not in have]
    res["new_found"] = len(new)
    log(f"[FF-NEW] {len(have)} products already saved -> {len(new)} NEW on French Fragrance")
    if len(new) > max(500, len(urls) * 0.5):            # safety: links written differently -> do not add thousands
        log(f"[FF-NEW] STOP: more than half of the sitemap looks new - the links are probably written differently "
            f"than the saved ones (example new: {new[0]}). Nothing added - send this log to Claude.")
        return {**res, "error": "too many new - link format?"}
    if not new:
        return res
    todo = new[:3] if test else new[:FF_NEW_MAX_PER_DAY]
    if len(new) > len(todo) and not test:
        log(f"[FF-NEW] {len(todo)} today (FF_NEW_MAX_PER_DAY), the other {len(new) - len(todo)} on the next days")
    before = {"ff": max_row_id("french_fragrance_catalog")}
    for k, (t, _, _) in SISTERS.items():
        before[k] = max_row_id(t)
    added_ids, errors_in_row = [], 0
    conn = db()
    cur = conn.cursor()
    try:
        for i, url in enumerate(todo, 1):
            try:
                r = bp_scraper.scrape_product(page, url)
                errors_in_row = 0
            except Exception as ex:
                res["errors"] += 1
                errors_in_row += 1
                log(f"[FF-NEW] {i}/{len(todo)} ERROR {url}: {str(ex).splitlines()[0][:150]}")
                if errors_in_row >= 10:
                    log("[FF-NEW] 10 errors in a row - probably blocked. Stopping for today.")
                    break
                time.sleep(DELAY)
                continue
            if r.get("status") != "ok":
                log(f"[FF-NEW] {i}/{len(todo)} {url}: page does not open a product - skipped")
                time.sleep(DELAY)
                continue
            gtin = ff_gtin(page)
            r = check_price("ff", url, r, None)          # same 100x price check as the daily run
            if test:
                log(f"[TEST] NEW {i}/{len(todo)} {(r.get('title') or url)[:60]} | GTIN {gtin} | "
                    f"AED {r.get('price_inc_tax')} | {r['stock']} | {r.get('size')} (not saved)")
            else:
                cur.execute("""INSERT INTO french_fragrance_catalog
                                   (airtable_record_id, product_url, gtin, name, price_inc_tax, price, stock, volume, updated_at)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                               ON CONFLICT (airtable_record_id) DO NOTHING RETURNING id""",
                            (f"new:{url}", url, gtin, r.get("title"), r.get("price_inc_tax"), r.get("price"),
                             r["stock"], r.get("size")))
                row = cur.fetchone()
                conn.commit()
                if row:
                    added_ids.append(row[0])
                    log(f"[FF-NEW] {i}/{len(todo)} ADDED {(r.get('title') or url)[:60]} | GTIN {gtin or '-'} | "
                        f"AED {r.get('price_inc_tax') or '-'} | {r['stock']}")
            time.sleep(DELAY)
        # Branded Perfume + Essenzi: the same product = the same link on their domain
        for k, (t, domain, label) in SISTERS.items():
            if test or not added_ids or not table_exists(cur, t):
                continue
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (t,))
            cols = {c for (c,) in cur.fetchall()}
            extra = [c for c in ("ff_url", "gtin", "name", "volume") if c in cols]
            src = {"ff_url": "f.product_url", "gtin": "f.gtin", "name": "f.name", "volume": "f.volume"}
            cur.execute(f"""INSERT INTO {t} (ff_id, {k}_url{''.join(', ' + c for c in extra)})
                            SELECT f.id, REPLACE(f.product_url, 'frenchfragrance.com', %s){''.join(', ' + src[c] for c in extra)}
                            FROM french_fragrance_catalog f
                            WHERE f.id = ANY(%s) AND NOT EXISTS (SELECT 1 FROM {t} s WHERE s.ff_id = f.id)""",
                        (domain, added_ids))
            log(f"[FF-NEW] {label}: {cur.rowcount} new links added ({domain})")
            conn.commit()
    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
    res["added"] = len(added_ids)
    log(f"[FF-NEW] {len(added_ids)} new French Fragrance products saved")
    if test or not added_ids:
        return res
    # open the new Branded Perfume / Essenzi pages now (price + stock), same as the daily brands
    for k in SISTERS:
        try:
            res[k] = run_daily_shop(k, added_ids, False, get_page)
        except Exception as ex:
            log(f"[FF-NEW] {SISTERS[k][2]} pages FAILED: {ex}")
    # shown on the shop pages: "N new products added" (+ Show = rows above the old highest row number)
    today = date.today().isoformat()
    wr = state.setdefault("weekly_results", {})
    wr["French Fragrance"] = {"new_added": len(added_ids), "new_after_id": before["ff"], "date": today}
    for k, (t, _, label) in SISTERS.items():
        wr[label] = {"new_added": len(added_ids), "new_after_id": before[k], "date": today}
    write_state(state)
    # match the new French Fragrance products to our products (Render)
    res["match"] = start_and_wait("/competitor/match-ff", "/samawa/status", "French Fragrance match")
    return res


# ---------------------------------------------------------------- Chrome
def chrome(pw):
    """Connect to your Chrome on port 9222; start it (same profile as the scrapers) when it is not open."""
    try:
        b = pw.chromium.connect_over_cdp(CDP_URL)
        log(f"[CHROME] Connected to the Chrome already open on {CDP_URL}")
        return b
    except Exception:
        log(f"[CHROME] Not running -> starting Chrome ({CHROME_PROFILE})")
        subprocess.Popen([CHROME_EXE, "--remote-debugging-port=9222", f"--user-data-dir={CHROME_PROFILE}"])
        for _ in range(30):
            time.sleep(2)
            try:
                b = pw.chromium.connect_over_cdp(CDP_URL)
                log("[CHROME] Started and connected")
                return b
            except Exception:
                pass                                     # Chrome still opening -> try again in 2 seconds
    log("[CHROME] Could not start Chrome - check CHROME_EXE / CHROME_PROFILE at the top of daily_update.py")
    raise RuntimeError("could not start / connect to Chrome on port 9222")


# ---------------------------------------------------------------- RUN 2 on Render (no PC)
def india_now():
    """Current India time (Render's clock is UTC; the website's own clock is not changed)."""
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def render_full_lists(args, quiet=False, restart_stopped=False):
    """RUN 2 on Render: full lists of RENDER_FULL_LISTS (new products saved, saved products only price + stock),
    matching, then least / suggested price. Progress in app_settings 'full_lists' (shown on the website).
    Returns True when it ran. quiet=True (scheduler): no 'waiting' lines every minute."""
    global STATE_KEY
    STATE_KEY = RENDER_STATE_KEY                         # never touches the PC run's progress ('daily_update')
    now = india_now()
    today = now.date().isoformat()
    # before RUN2_TIME / weekend / already done today -> nothing to do
    if now.weekday() not in WORK_DAYS and not args.force:
        if not quiet:
            log(f"[RUN2] {now:%a %H:%M} India - no run on Saturday / Sunday (--force to run)")
        return False
    if now.strftime("%H:%M") < RUN2_TIME and not args.force:
        if not quiet:
            log(f"[RUN2] {now:%H:%M} India - waiting for RUN2_TIME {RUN2_TIME} (--force to run now)")
        return False
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM app_settings WHERE key = %s", (STATE_KEY,))
    row = cur.fetchone()
    state = json.loads(row[0]) if row and row[0] else {}
    cur.close()
    conn.close()
    if state.get("weekly_date") == today and not args.force:
        if not quiet:
            log(f"[RUN2] Full lists already done today ({state.get('weekly_finished')})")
        return False
    started = state.get("weekly_started") or ""
    if state.get("weekly_status") == "running" and started[:10] == today and restart_stopped:
        log(f"[RUN2] A run started at {started[11:16]} was stopped (website restarted / deployed) -> starting again")
    elif state.get("weekly_status") == "running" and started[:10] == today and not args.force:
        if not quiet:
            log(f"[RUN2] Still running since {started[11:16]} - not started twice")
        return False
    log("=" * 70)
    log(f"[RUN2] Started {now:%d %b %Y %H:%M} India time (RUN2_TIME {RUN2_TIME}) - full lists of "
        f"{', '.join(RENDER_FULL_LISTS)} on Render, no PC")
    state.update({"weekly_status": "running", "weekly_started": now.isoformat(timespec="minutes")})
    write_state(state)
    run_weekly(state, only=RENDER_FULL_LISTS)            # download -> new products saved -> details kept -> match
    state.update({"weekly_status": "done", "weekly_date": today,
                  "weekly_finished": india_now().isoformat(timespec="minutes")})
    r = render_get("/competitor/update-least")           # least price + suggested price + automatic brands
    state["recalc"] = f"HTTP {r.status_code}" if r is not None else "not started"
    write_state(state)
    log(f"[RUN2] Finished {india_now():%H:%M} India | update-least {state['recalc']}")
    return True


def run2_scheduler(check_every=60):
    """Started by app.py when the website starts (one background thread): every minute -> render_full_lists()."""
    log(f"[RUN2] Scheduler started inside the website: full lists every working day at {RUN2_TIME} India time "
        f"({', '.join(RENDER_FULL_LISTS)})")
    args = argparse.Namespace(force=False)
    done_on = None                                       # remember 'done today' -> no database check every minute
    first = True                                         # website just started: a 'running' run today was cut off
    while True:
        try:
            today = india_now().date().isoformat()
            if done_on != today:
                ran = render_full_lists(args, quiet=True, restart_stopped=first)
                if india_now().strftime("%H:%M") >= RUN2_TIME:
                    first = False                        # the cut-off check is only needed once, at/after RUN2_TIME
                now = india_now()
                if ran or now.strftime("%H:%M") >= RUN2_TIME:
                    done_on = today                      # ran now, or already done / running elsewhere today
                    if not ran:
                        log(f"[RUN2] {now:%H:%M} India - nothing to start today (already done or running)")
        except Exception as ex:                          # never stops the website; tries again next minute
            log(f"[RUN2] ERROR: {str(ex)[:200]} - trying again in {check_every}s")
            done_on = None
        time.sleep(check_every)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="3 products per shop, print only, nothing saved")
    ap.add_argument("--force", action="store_true", help="run today's brands again")
    ap.add_argument("--weekly", action="store_true", help="run the weekly full lists now")
    ap.add_argument("--no-weekly", action="store_true", help="daily part only")
    ap.add_argument("--brands", default="", help='only these brands, e.g. "Dior,Chanel" (daily position unchanged)')
    ap.add_argument("--shop", default="", choices=[""] + list(DAILY_SHOPS), help="daily part for one shop only")
    ap.add_argument("--full-lists", action="store_true",
                    help="RUN 2 by hand: full lists of " + ", ".join(RENDER_FULL_LISTS) + " (normally the website runs it at RUN2_TIME)")
    args = ap.parse_args()
    if args.full_lists:
        return render_full_lists(args)
    # where the page readers (bp_scraper.py, es_scraper.py) and PC scrapers are: the folder of this file
    sys.path.insert(0, HERE)                             # bp_scraper.py, es_scraper.py ... next to this file
    log("=" * 70)
    log(f"[RUN] Started {datetime.now():%d %b %Y %H:%M}" + (" (TEST - nothing saved)" if args.test else ""))
    log(f"[RUN] Options: {vars(args)} | folder {HERE} | Render {SITE}")
    if date.today().weekday() not in WORK_DAYS and not (args.force or args.test or args.weekly or args.brands or args.shop):
        log(f"[RUN] Today is {DAYS[date.today().weekday()]} - no update on Saturday / Sunday "
            f"(use --force to run anyway)")
        return

    conn = db()
    cur = conn.cursor()
    state = json.loads(read_setting(cur, STATE_KEY) or "{}")
    try:
        per_day = max(1, int(float(read_setting(cur, PER_DAY_KEY) or DEFAULT_PER_DAY)))
    except ValueError:
        per_day = DEFAULT_PER_DAY
    groups = ff_brand_groups(cur)                         # French Fragrance products grouped by brand
    # by hand (--brands / --shop / --test): the daily position and results on the website are NOT changed
    by_hand = bool(args.brands or args.shop or args.test)
    log(f"[RUN] {per_day} brands per day | {'run by hand - daily position not moved' if by_hand else 'normal daily run'}")
    if args.brands:
        wanted = {b.strip().lower() for b in args.brands.split(",") if b.strip()}
        brands = [b for b in groups if b.lower() in wanted]
        log(f"[DAILY] Brands from --brands: {', '.join(brands) or 'none found'} (daily position not changed)")
    else:
        brands = todays_brands(cur, groups, state, per_day, args.force or args.shop or args.test)
    weekly = (bool(PC_FULL_LISTS) and not args.no_weekly and not args.brands and not args.shop and not args.test
              and weekly_due(cur, state, args.weekly))
    conn.commit()
    cur.close()
    conn.close()

    # ---------------- daily part: French Fragrance, Branded Perfume, Essenzi
    today = date.today().isoformat()
    ff_new_due = (FF_NEW_PRODUCTS and not args.brands and not args.shop
                  and (args.test or args.force or state.get("ff_new_date") != today))   # once a working day
    log(f"[FF-NEW] New French Fragrance products: {'check today' if ff_new_due else 'already checked today / off'}")
    ff_new_added = 0
    results = {}
    if brands or ff_new_due:
        ids = []
        # brand names matched without caring about capitals / spaces ("Acqua di Parma" = "Acqua Di Parma")
        by_key = {}
        for g, g_ids in groups.items():
            by_key.setdefault(norm(g), []).extend(g_ids)
        for b in (brands or []):
            found = by_key.get(norm(b), [])
            if not found:
                log(f"[DAILY] Brand '{b}' has no French Fragrance products now - skipped")
            ids += found
        if brands:
            log(f"[DAILY] Today's {len(brands)} brands ({len(ids)} French Fragrance products): {', '.join(brands)}")
        if brands and not by_hand:
            state["started"] = datetime.now().isoformat(timespec="minutes")
            write_state(state)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            holder = {"page": None, "opened": set()}

            def get_page(site, reader):
                """One Chrome tab for all shops; opens the shop's home page once (passes Cloudflare)."""
                log(f"[CHROME] Tab for {site or '?'}" + (" (home page already opened)" if site in holder["opened"] else ""))
                if holder["page"] is None:
                    holder["page"] = chrome(pw).contexts[0].new_page()
                    log("[CHROME] Connected - a new tab is used")
                if site and site not in holder["opened"]:
                    holder["page"].goto(site, wait_until="domcontentloaded", timeout=90000)
                    reader.wait_cloudflare(holder["page"])
                    holder["opened"].add(site)
                return holder["page"]

            for key in (([args.shop] if args.shop else list(DAILY_SHOPS)) if brands else []):
                try:
                    results[DAILY_SHOPS[key]["label"]] = run_daily_shop(key, ids, args.test, get_page)
                except Exception as ex:                  # one shop failing must not stop the others
                    log(f"[{key.upper()}] FAILED: {ex}")
                    results[DAILY_SHOPS[key]["label"]] = {"error": str(ex)[:200]}
                if not by_hand:
                    state["shops"] = results
                    write_state(state)                   # progress visible in Settings -> Automate
            # NEW French Fragrance products (+ Branded Perfume / Essenzi rows), once a working day
            if ff_new_due:
                try:
                    r_new = run_ff_new(get_page, state, args.test)
                    ff_new_added = r_new.get("added", 0)
                    log(f"[FF-NEW] Result: {json.dumps(r_new)[:300]}")
                except Exception as ex:
                    log(f"[FF-NEW] FAILED: {ex}")
                if not args.test:
                    state["ff_new_date"] = today
                    write_state(state)
            if holder["page"] is not None:
                try:
                    holder["page"].close()
                except Exception:
                    pass
        if brands and not by_hand:
            nxt, _ = pick_next(sorted(groups, key=str.lower), brands[-1], per_day)
            state.update({"daily_status": "done", "last_brand": brands[-1], "next": nxt,
                          "finished": datetime.now().isoformat(timespec="minutes")})
            write_state(state)
            log(f"[DAILY] Done. Next run starts after brand '{brands[-1]}': {', '.join(nxt)}")
        log(f"[DAILY] Results: {json.dumps(results)}")

    if args.test:
        log("[TEST] Done - nothing was saved, daily position not changed.")
        return

    # ---------------- weekly part: whole lists of Samawa, Amberiya, V Perfumes, Al Injaz, Niche Gallerie, Ounass
    if weekly:
        state["weekly_status"] = "running"
        state["weekly_started"] = datetime.now().isoformat(timespec="minutes")
        write_state(state)
        run_weekly(state, only=PC_FULL_LISTS)            # only shops moved back to the PC (normally none)
        state.update({"weekly_status": "done", "weekly_date": date.today().isoformat(),
                      "weekly_finished": datetime.now().isoformat(timespec="minutes")})
        write_state(state)

    # ---------------- least price + suggested price + automatic brands (Branded Perfume / Essenzi prices too)
    if not brands and not ff_new_added:
        log("[RUN] No daily brands / new products today -> update-least not needed")
    if (brands or ff_new_added) and not args.test:
        r = render_get("/competitor/update-least")
        state["recalc"] = (f"HTTP {r.status_code}" if r is not None else "not started")
        log(f"[RENDER] update-least -> {state['recalc']}")
        if not by_hand:
            write_state(state)
    log(f"[RUN] Finished {datetime.now():%H:%M}")


if __name__ == "__main__":
    main()
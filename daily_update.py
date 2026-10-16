r"""
Competitor price update - TWO runs (this file is used by both)
================================================================
RUN 1 - on YOUR PC, Monday-Friday 11:30 AM India time (Windows Task Scheduler starts daily_update.bat; to change the
        time see START TIME in daily_update.bat). Saturday / Sunday: nothing (skipped unless --force).
    French Fragrance, Branded Perfume, Essenzi (their pages must be opened one by one in your Chrome):
    the NEXT N BRANDS (Settings -> Automate, default 10) of their own brand list (A -> Z, then again from A):
    price + stock of ALL products of those brands. Branded Perfume + Essenzi = the same products as French Fragrance.
    The brand is read from the start of the product name ("Christian Dior Sauvage EDT" -> "Christian Dior").
    Also once a day: NEW French Fragrance products from its sitemap are saved (+ their Branded Perfume / Essenzi rows)
    and matched to our products (FF_NEW_PRODUCTS).
    Progress: app_settings key 'daily_update' (a stopped run continues with the same brands).

RUN 2 - inside the website on Render (no PC, no cron job), EVERY day at RUN2_TIME (7:00 AM India time):
    ONE shop a day in RUN2_ORDER: Samawa -> Ounass -> V Perfumes -> Amberiya -> Al Injaz -> Niche Gallerie,
    then a pause; a new round starts RUN2_ROUND_DAYS (14) days after the last one began.
    Whole product list of that shop: new products are saved, products already saved only get the new price + stock,
    then matching to our products. app.py starts run2_scheduler() when the website starts.
    Progress: app_settings key 'full_lists'.

After each run Render recalculates least + suggested price and updates the automatic brands in Airtable
(/competitor/update-least). Settings -> Automate shows both runs.

BEFORE the first run (once): put this file next to bp_scraper.py, es_scraper.py, price_triggers.py,
suggested_price.py, then fill in daily_update.bat (database URL, RUN_SECRET) and create the scheduled task.

Run by hand (PC):
  python daily_update.py --test            -> today's brands, 3 products per shop, PRINT only (nothing saved)
  python daily_update.py                   -> RUN 1 (the real run)
  python daily_update.py --force           -> RUN 1 again for today's brands (also on Saturday / Sunday)
  python daily_update.py --brands "Dior,Chanel"   -> only these brands (does NOT move the daily position)
  python daily_update.py --shop es         -> one shop only (ff, bp, es; daily position not moved)
  python daily_update.py --full-lists --force     -> RUN 2 now (today's shop) - normally the website does this
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

# TRIGGER 1 (price_triggers.py + suggested_price.py, same files as on Render - copy both next to this file):
# a changed competitor price / stock recalculates that product's suggested price at once.
# Missing files -> the run still works, prices are then recalculated only at the end (update-least).
try:
    import price_triggers
except ImportError as _ex:
    price_triggers = None
    print(f"[TRIGGER 1] price_triggers.py / suggested_price.py not found ({_ex}) -> suggested prices are "
          f"recalculated only at the end of the run", flush=True)

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
WORK_DAYS = range(0, 5)        # RUN 1: Monday-Friday (the PC is off on Saturday and Sunday)
DELAY = 1.5                    # seconds between product pages (be gentle with the shops)
STATE_KEY = "daily_update"     # RUN 1 progress in app_settings: positions, today's brands, results
PER_DAY_KEY = "daily_brands_per_day"
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(HERE, "daily_update.log")

# Daily shops (page by page). All three use the French Fragrance product list -> one brand list / position.
DAILY_SHOPS = {
    "ff": {"label": "French Fragrance", "table": "french_fragrance_catalog", "reader": "bp_scraper"},
    "bp": {"label": "Branded Perfume", "table": "branded_perfume_catalog", "reader": "bp_scraper"},
    "es": {"label": "Essenzi", "table": "essenzi_catalog", "reader": "es_scraper"},
}
# ---------------------------------------------------------------- RUN 2 settings (website on Render, one shop a day)
RUN2_STATE_KEY = "full_lists"  # RUN 2 progress in app_settings (separate from RUN 1)
# RUN 2 START TIME (India time, EVERY day incl. Saturday / Sunday) - CHANGE IT HERE, then push to GitHub + deploy:
RUN2_TIME = "07:00"            # "HH:MM", 24-hour India time. Example: "06:30", "09:00", "14:00"
# ONE SHOP PER DAY in this order (day 1 Samawa, day 2 Ounass, day 3 V Perfumes ...). After the 6th shop the round is
# complete; the next round starts RUN2_ROUND_DAYS after the start of the last one -> every shop once every 2 weeks.
RUN2_ORDER = ["samawa", "on", "vp", "am", "aj", "ng"]
RUN2_ROUND_DAYS = 14           # a new round every 14 days (7 = every week)
RUN2_DAYS = range(0, 7)        # 0 = Monday ... 6 = Sunday: Render runs every day
# RUN2_SAVE_NEW = True  -> NEW products of the shop are SAVED and matched to our products.
#                 False -> new rows made by the download are removed again right after it.
RUN2_SAVE_NEW = True
# RUN2_ONLY_PRICE_STOCK = True -> products ALREADY saved only get the new price + stock: name, brand, GTIN, link,
# volume ... are put back to what they were before the download (new products keep everything they came with).
RUN2_ONLY_PRICE_STOCK = True
RUN2_UPDATE_COLUMNS = {"price", "price_inc_tax", "stock", "updated_at"}   # what the download may change
RUN2_SHOPS = [
    # (key, label, table, Render download link, Render status link, match link)
    ("samawa", "Samawa", "samawa_catalog", "/samawa-catalog/run", "/samawa-catalog/status", "/competitor/match-samawa"),
    ("on", "Ounass", "ounass_catalog", "/ounass/run", "/ounass/status", "/competitor/match-on"),
    ("vp", "V Perfumes", "vperfumes_catalog", "/vperfumes/run", "/vperfumes/status", "/competitor/match-vp"),
    ("am", "Amberiya", "amberiya_catalog", "/amberiya/run", "/amberiya/status", "/competitor/match-am"),
    ("aj", "Al Injaz", "alinjaz_catalog", "/alinjaz/run", "/alinjaz/status", "/competitor/match-aj"),
    ("ng", "Niche Gallerie", "nichegallerie_catalog", "/nichegallerie/run", "/nichegallerie/status",
     "/competitor/match-ng"),
]
RUN2_LABELS = {k: label for k, label, *_ in RUN2_SHOPS}


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
    log(f"[STATE] Saved ({STATE_KEY}): daily {state.get('daily_status', '-')} ({', '.join(state.get('brands') or [])})"
        + (f" | run 2 {state.get('weekly_status')}" if STATE_KEY == RUN2_STATE_KEY else ""))


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
    Changed -> TRIGGER 1 (price_triggers.py): the new price / stock goes into competitor_table (Branded Perfume /
    Essenzi too) and that product's least + suggested price are recalculated at once.
    update-least at the end of the run still recalculates everything (+ Airtable for automatic brands)."""
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
        # TRIGGER 1: competitor price / stock changed -> recalculate the suggested price of the matched product(s)
        # (a failed page - 403 / timeout - never reaches save_one, so it never triggers anything)
        if price_triggers:
            price_triggers.on_competitor_change(cur, key, url, price=new_price, in_stock=r["stock"] == "In stock")
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


# ---------------------------------------------------------------- RUN 2: one shop's whole product list
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
    """RUN2_SAVE_NEW = False only: rows added by this download (row number above the old highest) are deleted again.
    RUN2_SAVE_NEW = True: new products are kept -> only counted and logged."""
    if RUN2_SAVE_NEW:
        conn = db()
        cur = conn.cursor()
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table} WHERE id > %s", (before_id or 0,))
            n = cur.fetchone()[0]
        finally:
            cur.close()
            conn.close()
        log(f"[RUN2] {label}: {n} NEW products SAVED (they are matched to our products next)")
        return n
    if not before_id:
        log(f"[RUN2] {label}: table was empty before -> nothing removed (first download keeps everything)")
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
    log(f"[RUN2] {label}: {n} NEW products removed again (only current products are kept and updated)")
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
        cols = [c for (c,) in cur.fetchall() if c != "id" and c not in RUN2_UPDATE_COLUMNS]
        n = 0
        if cols:
            sets = ", ".join(f"{c} = k.{c}" for c in cols)
            differs = " OR ".join(f"t.{c} IS DISTINCT FROM k.{c}" for c in cols)
            # only rows where a detail really changed are written back
            cur.execute(f"UPDATE {table} t SET {sets} FROM {keep} k WHERE t.id = k.id AND ({differs})")
            n = cur.rowcount
        cur.execute(f"DROP TABLE {keep}")
        conn.commit()
        log(f"[RUN2] {label}: only price + stock kept from the download - other details put back on {n} products "
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
        log(f"[RUN2] {label}: copy of {n} products made ({keep}) - after the download only price + stock change")
        return True
    finally:
        cur.close()
        conn.close()


def run2_shop(state, shop):
    """RUN 2 for ONE shop: download its whole list on Render -> new products saved -> details of saved products put
    back (only price + stock change) -> matching on Render. Result saved in state["weekly_results"][label]
    (key name kept: the shop pages read it for "N new products added")."""
    results = state.setdefault("weekly_results", {})
    key, label, table, dl, status, match = next(x for x in RUN2_SHOPS if x[0] == shop)
    log(f"\n[RUN2] ===== {label}: full product list =====")
    before_id = max_row_id(table)                        # everything above this after the download = new products
    log(f"[RUN2] {label}: highest row in {table} before the download = {before_id}")
    if RUN2_ONLY_PRICE_STOCK:
        keep_details(table, label)                       # names / brands / GTINs / links are put back afterwards
    res = start_and_wait(dl, status, f"{label} download")
    n_new = remove_new_products(table, label, before_id)  # RUN2_SAVE_NEW = True -> kept (and counted)
    if RUN2_SAVE_NEW:
        # shown on the shop's page: "N new products added" (+ filter: rows with id above new_after_id)
        res["new_added"], res["new_after_id"], res["date"] = n_new, before_id or 0, date.today().isoformat()
    else:
        res["new_removed"] = n_new
    if RUN2_ONLY_PRICE_STOCK:
        res["details_restored"] = restore_details(table, label)          # only price + stock changed
    log(f"[RUN2] {label}: matching to our products on Render")
    res["match"] = start_and_wait(match, "/samawa/status", f"{label} match")      # all matches use /samawa/status
    results[label] = res
    write_state(state)
    return res


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


# ---------------------------------------------------------------- RUN 2 scheduler (inside the website on Render)
def india_now():
    """Current India time (Render's clock is UTC; the website's own clock is not changed)."""
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


def run2_plan(state, today):
    """Which shop today? Returns (shop key or None, message). Round = RUN2_ORDER one shop a day; next round starts
    RUN2_ROUND_DAYS after the start of the previous round."""
    idx = state.get("next_index", 0)
    start = state.get("round_start")
    if start and idx >= len(RUN2_ORDER):                          # round complete -> wait for the next round
        next_round = date.fromisoformat(start) + timedelta(days=RUN2_ROUND_DAYS)
        if date.fromisoformat(today) < next_round:
            return None, f"round of {start} complete - next round starts {next_round.isoformat()}"
        start, idx = None, 0
    if not start:                                                  # first run ever / new round
        state.update({"round_start": today, "next_index": 0})
        idx = 0
        log(f"[RUN2] New round starts today {today}: {' -> '.join(RUN2_ORDER)} (one shop a day)")
    return RUN2_ORDER[idx], f"shop {idx + 1} of {len(RUN2_ORDER)} in the round of {state['round_start']}"


def render_full_lists(args, quiet=False, restart_stopped=False):
    """RUN 2 on Render: ONE shop per day (RUN2_ORDER) - full list of that shop (new products saved, saved products
    only price + stock), matching, then least / suggested price. Progress in app_settings 'full_lists'.
    Returns True when it ran. quiet=True (scheduler): no 'waiting' lines every minute."""
    global STATE_KEY
    STATE_KEY = RUN2_STATE_KEY                           # never touches RUN 1's progress ('daily_update')
    now = india_now()
    today = now.date().isoformat()
    if now.weekday() not in RUN2_DAYS and not args.force:
        if not quiet:
            log(f"[RUN2] {now:%a %H:%M} India - Run 2 is off on this day (RUN2_DAYS)")
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
    started = state.get("weekly_started") or ""
    cut_off = state.get("weekly_status") == "running" and started[:10] == today
    if cut_off and restart_stopped:
        log(f"[RUN2] Today's shop started at {started[11:16]} was stopped (website restarted / deployed) -> again")
    elif cut_off and not args.force:
        if not quiet:
            log(f"[RUN2] Still running since {started[11:16]} - not started twice")
        return False
    elif state.get("weekly_date") == today and not args.force:
        if not quiet:
            log(f"[RUN2] Today's shop already done ({state.get('weekly_finished')})")
        return False
    shop, why = run2_plan(state, today)
    if shop is None:
        log(f"[RUN2] {now:%d %b %H:%M} India - no shop today: {why}")
        state.update({"weekly_date": today, "today_shop": None, "next_info": why})
        write_state(state)                               # 'checked today' -> not asked again until tomorrow
        return False
    label = RUN2_LABELS[shop]
    log("=" * 70)
    log(f"[RUN2] Started {now:%d %b %Y %H:%M} India time - today: {label} ({why})")
    state.update({"weekly_status": "running", "weekly_started": now.isoformat(timespec="minutes"), "today_shop": label})
    write_state(state)
    run2_shop(state, shop)                               # download -> new products saved -> details kept -> match
    res = state.setdefault("weekly_results", {}).setdefault(label, {})
    res["done_on"] = today                               # shown on the Automate page: last update of this shop
    idx = RUN2_ORDER.index(shop) + 1
    if idx < len(RUN2_ORDER):
        state["next_info"] = f"tomorrow: {RUN2_LABELS[RUN2_ORDER[idx]]}"
    else:
        nr = date.fromisoformat(state["round_start"]) + timedelta(days=RUN2_ROUND_DAYS)
        state["next_info"] = f"round complete - next round ({RUN2_LABELS[RUN2_ORDER[0]]} first) on {nr.isoformat()}"
    state.update({"next_index": idx, "weekly_status": "done", "weekly_date": today,
                  "weekly_finished": india_now().isoformat(timespec="minutes")})
    r = render_get("/competitor/update-least")           # least price + suggested price + automatic brands
    state["recalc"] = f"HTTP {r.status_code}" if r is not None else "not started"
    write_state(state)
    log(f"[RUN2] {label} finished {india_now():%H:%M} India | update-least {state['recalc']} | next: {state['next_info']}")
    return True


def run2_scheduler(check_every=60):
    """Started by app.py when the website starts (one background thread): every minute -> render_full_lists()."""
    log(f"[RUN2] Scheduler started inside the website: one shop a day at {RUN2_TIME} India time "
        f"({' -> '.join(RUN2_ORDER)}, a new round every {RUN2_ROUND_DAYS} days)")
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


# ---------------------------------------------------------------- main (RUN 1)
RUN1_RESULT_SHOPS = ("French Fragrance", "Branded Perfume", "Essenzi")   # "new products" info RUN 1 keeps


def clean_old_run2(state):
    """RUN 1 progress used to hold the PC's old full-list results too. Remove them (RUN 2 has its own progress)."""
    old = [k for k in ("weekly_status", "weekly_started", "weekly_date", "weekly_finished") if k in state]
    for k in old:
        state.pop(k)
    wr = state.get("weekly_results") or {}
    gone = [shop for shop in list(wr) if shop not in RUN1_RESULT_SHOPS]
    for shop in gone:
        wr.pop(shop)
    if old or gone:
        log(f"[RUN] Old PC full-list results removed from RUN 1 progress: {', '.join(gone) or '-'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="3 products per shop, print only, nothing saved")
    ap.add_argument("--force", action="store_true", help="run today's brands again")
    ap.add_argument("--brands", default="", help='only these brands, e.g. "Dior,Chanel" (daily position unchanged)')
    ap.add_argument("--shop", default="", choices=[""] + list(DAILY_SHOPS), help="daily part for one shop only")
    ap.add_argument("--full-lists", action="store_true",
                    help="RUN 2 by hand: today's shop (normally the website runs it at RUN2_TIME); add --force")
    args = ap.parse_args()
    if args.full_lists:
        return render_full_lists(args)
    # where the page readers (bp_scraper.py, es_scraper.py) are: the folder of this file
    sys.path.insert(0, HERE)                             # bp_scraper.py, es_scraper.py ... next to this file
    log("=" * 70)
    log(f"[RUN] Started {datetime.now():%d %b %Y %H:%M}" + (" (TEST - nothing saved)" if args.test else ""))
    log(f"[RUN] Options: {vars(args)} | folder {HERE} | Render {SITE}")
    if date.today().weekday() not in WORK_DAYS and not (args.force or args.test or args.brands or args.shop):
        log(f"[RUN] Today is {DAYS[date.today().weekday()]} - no update on Saturday / Sunday "
            f"(use --force to run anyway)")
        return

    conn = db()
    cur = conn.cursor()
    state = json.loads(read_setting(cur, STATE_KEY) or "{}")
    clean_old_run2(state)                                # RUN 2 no longer runs on the PC -> its old results removed
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
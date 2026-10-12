r"""
Daily + weekly competitor update (runs on YOUR PC Monday-Friday at 1 PM - Windows Task Scheduler, see daily_update.bat)
Saturday / Sunday: nothing runs (PC is off; if it is on, the script skips those days unless --force).
-------------------------------------------------------------------------------------------------------------------
EVERY WORKING DAY (Mon-Fri) - French Fragrance, Branded Perfume, Essenzi (their pages must be opened one by one in your Chrome):
    the NEXT 10 BRANDS of the shop's OWN brand list (A -> Z, then again from A): price + stock of ALL their products
    (matched to us or not - "Missing items" too).
    These shops have no brand column -> the brand is read from the start of the product name
    ("Christian Dior Sauvage EDT 100ml" -> "Christian Dior"), using every brand known in the database
    (our brands + Samawa / Amberiya / Ounass brands); unknown -> the first word of the name.
    Branded Perfume + Essenzi are the same products as French Fragrance -> same brand list and same position.

ONCE A WEEK (Settings -> Automate, Monday-Friday, default Friday; if the PC was off that day it runs the next
working day) -
Samawa, Amberiya, V Perfumes, Al Injaz, Niche Gallerie, Ounass give their WHOLE product list in one go:
    full download of every product (all brands), then matching to our products on Render.
       Samawa / Amberiya / V Perfumes  -> download runs on Render (/samawa-catalog/run, /amberiya/run, /vperfumes/run)
       Al Injaz / Niche Gallerie / Ounass -> aj_scraper.py / ng_scraper.py / on_scraper.py on this PC (Chrome)
       then /competitor/match-samawa, match-am, match-vp, match-aj, match-ng, match-on on Render (one after another)

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
from datetime import datetime, date

import requests
import psycopg2

# ---------------------------------------------------------------- settings
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
WEEKLY_SHOPS = [
    # (key, label, Render download link or None, PC scraper or None, Render status link, match link)
    ("samawa", "Samawa", "/samawa-catalog/run", None, "/samawa-catalog/status", "/competitor/match-samawa"),
    ("am", "Amberiya", "/amberiya/run", None, "/amberiya/status", "/competitor/match-am"),
    ("vp", "V Perfumes", "/vperfumes/run", None, "/vperfumes/status", "/competitor/match-vp"),
    ("aj", "Al Injaz", None, "aj_scraper.py", None, "/competitor/match-aj"),
    ("ng", "Niche Gallerie", None, "ng_scraper.py", None, "/competitor/match-ng"),
    ("on", "Ounass", None, "on_scraper.py", None, "/competitor/match-on"),
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
    return psycopg2.connect(url, sslmode="require")


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
    """Save progress / results so Settings -> Automate can show them (and a stopped run can continue)."""
    conn = db()
    cur = conn.cursor()
    cur.execute("""INSERT INTO app_settings (key, value, updated_by) VALUES (%s, %s, 'daily_update.py')
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                                                   updated_at = NOW()""", (STATE_KEY, json.dumps(state)))
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
    for b in names:
        n = norm(b)
        if len(n) >= 2:
            by_norm.setdefault(n, b)                        # first spelling seen is shown
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
            old_price, old_stock = old_values(cur, key, rid)
            r = check_price(key, url, r, old_price)          # wrongly read price -> fixed or not saved
            if test:
                log(f"[TEST] {i}/{len(todo)} {(r.get('title') or url)[:60]} -> AED {r.get('price_inc_tax')} | "
                    f"{r['stock']}  (saved now: AED {old_price} | {old_stock}) (not saved)")
            else:
                changed = save_one(cur, key, rid, url, r)
                conn.commit()                            # saved one by one: a stop never loses checked products
                st["changed"] += changed
                log(f"[{key.upper()}] {i}/{len(todo)} {(r.get('title') or url)[:50]} | AED {r.get('price_inc_tax') or '-'} | "
                    f"{r['stock']}{'  <- CHANGED' if changed else ''}")
            time.sleep(DELAY)
    finally:
        cur.close()
        conn.close()
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
    due = forced or last is None or (today.weekday() == day and since > 0) or since >= 8
    log(f"[WEEKLY] Weekly day: {DAYS[day]} | last full lists: {last or 'never'} -> "
        f"{'RUN TODAY' if due else 'not today'}")
    return due


def run_weekly(state):
    """Whole product list of the 6 shops, then matching on Render (one shop after another)."""
    results = {}
    env = dict(os.environ)
    for key, label, dl, scraper, status, match in WEEKLY_SHOPS:
        log(f"\n[WEEKLY] ===== {label}: full product list =====")
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
        log(f"[WEEKLY] {label}: matching to our products on Render")
        res["match"] = start_and_wait(match, "/samawa/status", f"{label} match")      # all matches use /samawa/status
        results[label] = res
        state["weekly_results"] = results
        write_state(state)
    return results


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


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="3 products per shop, print only, nothing saved")
    ap.add_argument("--force", action="store_true", help="run today's brands again")
    ap.add_argument("--weekly", action="store_true", help="run the weekly full lists now")
    ap.add_argument("--no-weekly", action="store_true", help="daily part only")
    ap.add_argument("--brands", default="", help='only these brands, e.g. "Dior,Chanel" (daily position unchanged)')
    ap.add_argument("--shop", default="", choices=[""] + list(DAILY_SHOPS), help="daily part for one shop only")
    args = ap.parse_args()
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
    weekly = (not args.no_weekly and not args.brands and not args.shop and not args.test
              and weekly_due(cur, state, args.weekly))
    conn.commit()
    cur.close()
    conn.close()

    # ---------------- daily part: French Fragrance, Branded Perfume, Essenzi
    if brands:
        ids = [i for b in brands for i in groups.get(b, [])]
        log(f"[DAILY] Today's {len(brands)} brands ({len(ids)} French Fragrance products): {', '.join(brands)}")
        if not by_hand:
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

            results = {}
            for key in ([args.shop] if args.shop else list(DAILY_SHOPS)):
                try:
                    results[DAILY_SHOPS[key]["label"]] = run_daily_shop(key, ids, args.test, get_page)
                except Exception as ex:                  # one shop failing must not stop the others
                    log(f"[{key.upper()}] FAILED: {ex}")
                    results[DAILY_SHOPS[key]["label"]] = {"error": str(ex)[:200]}
                if not by_hand:
                    state["shops"] = results
                    write_state(state)                   # progress visible in Settings -> Automate
            if holder["page"] is not None:
                try:
                    holder["page"].close()
                except Exception:
                    pass
        if not by_hand:
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
        run_weekly(state)                                # each match also recalculates least / suggested prices
        state.update({"weekly_status": "done", "weekly_date": date.today().isoformat(),
                      "weekly_finished": datetime.now().isoformat(timespec="minutes")})
        write_state(state)

    # ---------------- least price + suggested price + automatic brands (Branded Perfume / Essenzi prices too)
    if not brands:
        log("[RUN] No daily brands today (already done) -> update-least not needed")
    if brands and not args.test:
        r = render_get("/competitor/update-least")
        state["recalc"] = (f"HTTP {r.status_code}" if r is not None else "not started")
        log(f"[RENDER] update-least -> {state['recalc']}")
        if not by_hand:
            write_state(state)
    log(f"[RUN] Finished {datetime.now():%H:%M}")


if __name__ == "__main__":
    main()
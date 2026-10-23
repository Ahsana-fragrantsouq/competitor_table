"""
Price triggers - WHEN the suggested price is recalculated (the rule itself is in suggested_price.py).
=====================================================================================================
Two triggers, both recalculate ONE product at once (least price, least priced website, suggested price):

  TRIGGER 1  a competitor's price or stock CHANGED        -> on_competitor_change(cur, shop, link, price, in_stock)
             called by daily_update.py save_one() right after a changed price / stock is saved
             (French Fragrance, Branded Perfume, Essenzi - one product at a time, while the run goes on).
             Run 2 shops (Samawa, Ounass, V Perfumes, Amberiya, Al Injaz, Niche Gallerie) save the WHOLE shop in one
             go (app.py ct_match_shop) and then recalculate ALL products at once (app.py ct_update_least) -
             same rule, same result.
             A failed check (403 / timeout / blocked page) saves nothing -> nothing changes -> no trigger.

  TRIGGER 2  an admin saved a new LOP or MOP on a card     -> on_limits_change(cur, fid, user)
             called by price_limits.py limits_save() (POST /competitor/limits)

Both use recalc_product(cur, fid) = THE calculation for one product:
    1. our UAE price + every IN-STOCK competitor with a link and a price
    2. least price = cheapest of them (ties -> all websites listed)
    3. suggested price = suggested_price.calculate_suggested_price(cheapest in-stock COMPETITOR, rate X, LOP, MOP)
         = cheapest competitor - X%  ->  <= LOP -> LOP  |  >= MOP -> MOP
    No Auto discount rate saved -> least price updated, suggested price left as it is (nothing is guessed).
    No competitor in stock      -> no suggested price (LOP / MOP never invent one).

Where things are (change them here):
  SECTION 1  settings                -> COMPETITORS (shops + order of ties), RATE_KEY
  SECTION 2  read helpers            -> read_rate(), has_limits()
  SECTION 3  recalc_product()        -> the calculation for ONE product (used by both triggers + app.py least_for)
  SECTION 4  TRIGGER 1               -> on_competitor_change()
  SECTION 5  TRIGGER 2               -> on_limits_change()

No Flask, no website: works on Render (app.py, price_limits.py) AND on the PC (daily_update.py).
Needs suggested_price.py next to it (on Render AND in the PC folder).
Our UAE price in Airtable (automatic brands) is NOT changed here:
  after TRIGGER 1 -> at the end of the run (/competitor/update-least -> automatic brands)
  after TRIGGER 2 -> at once by price_limits.py (if the brand is automatic)
"""

from suggested_price import calculate_suggested_price, raw_suggested_price


# ============================================================ SECTION 1: settings
# (label, column prefix in competitor_table) - same list + order as app.py LEAST_ORDER (order = order of tied websites)
COMPETITORS = [("Samawa", "samawa"), ("French Fragrance", "ff"), ("Branded Perfume", "bp"),
               ("Essenzi", "es"), ("V Perfumes", "vp"), ("Al Injaz", "aj"),
               ("Ounass", "on"), ("Amberiya", "am"), ("Niche Gallerie", "ng"),
               ("Perfume Dubai", "pd"), ("MHG Boutique", "mh")]
SHOP_LABELS = {p: label for label, p in COMPETITORS}
RATE_KEY = "discount_rate"          # Settings -> Auto discount rate (table app_settings, discount_rate.py)


def log(*a):
    """Print to the Render log / PC console (lines start with [TRIGGER 1], [TRIGGER 2] or [RECALC])."""
    print(*a, flush=True)


# ============================================================ SECTION 2: read helpers
def read_rate(cur):
    """Auto discount rate X in % (e.g. 3), or None when the admin never saved one.
    Read straight from app_settings (discount_rate.py needs Flask, the PC does not have the website)."""
    cur.execute("SELECT to_regclass('app_settings')")
    if cur.fetchone()[0] is None:
        return None
    cur.execute("SELECT value FROM app_settings WHERE key = %s", (RATE_KEY,))
    row = cur.fetchone()
    try:
        return float(row[0]) if row and row[0] not in (None, "") else None
    except ValueError:
        log(f"[RECALC] Saved discount rate {row[0]!r} is not a number -> treated as NOT SET")
        return None


_limits_columns = None      # remembered after the first check (True / False)


def has_limits(cur):
    """True when competitor_table already has the lop / mop columns (made by app.py on Render).
    The PC can run before the new app.py is deployed -> then LOP / MOP are simply not used."""
    global _limits_columns
    if _limits_columns is None:
        cur.execute("""SELECT COUNT(*) FROM information_schema.columns
                       WHERE table_name = 'competitor_table' AND column_name IN ('lop', 'mop')""")
        _limits_columns = cur.fetchone()[0] == 2
        if not _limits_columns:
            log("[RECALC] competitor_table has no lop / mop columns yet (deploy app.py) -> LOP / MOP not used")
    return _limits_columns


# ============================================================ SECTION 3: the calculation for ONE product
def recalc_product(cur, fid, reason=""):
    """Recalculate least price, least priced website and suggested price of ONE product (fi_record_id) and save them.
    Does NOT commit (the caller commits). Returns {"least", "sites", "suggested", "old_suggested"} or None."""
    tag = f"[RECALC{(' ' + reason) if reason else ''}]"
    limits = has_limits(cur)
    cols = ", ".join(f"{p}_price, {p}_stock, {p}_link" for _, p in COMPETITORS)
    extra = ", lop, mop" if limits else ", NULL, NULL"
    cur.execute(f"SELECT uae_price, suggested_price{extra}, {cols} FROM competitor_table WHERE fi_record_id = %s",
                (fid,))
    row = cur.fetchone()
    if not row:
        log(f"{tag} {fid}: product not in competitor_table -> nothing to do")
        return None
    uae, old_sugg, lop, mop = row[0], row[1], row[2], row[3]

    # 1. offers: our price always + competitors only when IN STOCK with a link and a price
    comp = []
    for i, (label, _) in enumerate(COMPETITORS):
        price, stock, link = row[4 + i * 3: 7 + i * 3]
        if stock and link and price and price > 0:
            comp.append((label, float(price)))
    offers = ([("Fragrant Souq", float(uae))] if uae and uae > 0 else []) + comp

    # 2. least price + every website with that price
    least = min((pr for _, pr in offers), default=None)
    sites = ", ".join(s for s, pr in offers if least is not None and abs(pr - least) < 0.005) or None
    best_comp = min((pr for _, pr in comp), default=None)

    # 3. suggested price (suggested_price.py): cheapest competitor - X%, then LOP / MOP
    rate = read_rate(cur)
    if rate is None:
        cur.execute("UPDATE competitor_table SET least_price = %s, least_priced_website = %s WHERE fi_record_id = %s",
                    (least, sites, fid))
        log(f"{tag} {fid}: least {least} ({sites}) | suggested NOT changed (no Auto discount rate saved)")
        return {"least": least, "sites": sites, "suggested": old_sugg, "old_suggested": old_sugg}
    try:
        suggested = calculate_suggested_price(best_comp, rate, lop, mop)
    except ValueError as ex:                               # e.g. LOP > MOP typed straight into the database
        log(f"{tag} {fid}: LOP / MOP ignored ({ex})")
        suggested = raw_suggested_price(best_comp, rate)
    cur.execute("UPDATE competitor_table SET least_price = %s, least_priced_website = %s, suggested_price = %s "
                "WHERE fi_record_id = %s", (least, sites, suggested, fid))
    old = float(old_sugg) if old_sugg is not None else None
    log(f"{tag} {fid}: cheapest competitor {best_comp} - {rate}% | LOP {lop} | MOP {mop} -> suggested "
        f"{old} -> {suggested}{'' if old != suggested else ' (same)'} | least {least} ({sites})")
    return {"least": least, "sites": sites, "suggested": suggested, "old_suggested": old}


# ============================================================ SECTION 4: TRIGGER 1 - competitor price / stock changed
def on_competitor_change(cur, shop, link, price=None, in_stock=None):
    """A competitor's price or stock CHANGED for the product page `link` (call it only when it really changed).
        shop     : column prefix - "ff", "bp", "es", "samawa", "vp", "aj", "on", "am", "ng", "pd", "mh"
        link     : the product page on that shop (the same text saved in competitor_table.<shop>_link)
        price    : new price incl. VAT (None = price not read -> the saved price is kept)
        in_stock : True / False (None = stock not known -> the saved stock is kept)
    1. the new price / stock is written into competitor_table for every product matched to that link
       (Branded Perfume / Essenzi otherwise only reach competitor_table at the end of the run)
    2. recalc_product() for each of those products
    Never stops the caller: a problem is logged and undone (SAVEPOINT); the end-of-run recalculation still runs.
    Does NOT commit (the caller commits). Returns the number of products recalculated."""
    label = SHOP_LABELS.get(shop)
    if not label or not link:
        log(f"[TRIGGER 1] unknown shop {shop!r} or empty link -> nothing done")
        return 0
    cur.execute("SAVEPOINT price_trigger")
    try:
        cur.execute(f"""UPDATE competitor_table SET {shop}_price = COALESCE(%s, {shop}_price),
                               {shop}_stock = COALESCE(%s, {shop}_stock), updated_at = NOW()
                        WHERE {shop}_link = %s RETURNING fi_record_id""", (price, in_stock, link))
        fids = [r[0] for r in cur.fetchall()]
        if not fids:
            log(f"[TRIGGER 1] {label} changed ({link}) but it is not matched to any of our products -> nothing to recalculate")
        for fid in fids:
            log(f"[TRIGGER 1] {label} price / stock changed -> AED {price} | "
                f"{'in stock' if in_stock else 'out of stock' if in_stock is not None else 'stock not read'} -> recalculating {fid}")
            recalc_product(cur, fid, reason="TRIGGER 1")
        cur.execute("RELEASE SAVEPOINT price_trigger")
        return len(fids)
    except Exception as ex:
        cur.execute("ROLLBACK TO SAVEPOINT price_trigger")
        log(f"[TRIGGER 1] {label} {link}: recalculation failed ({str(ex).strip()[:150]}) "
            f"-> will be done at the end of the run (update-least)")
        return 0


# ============================================================ SECTION 5: TRIGGER 2 - admin saved LOP / MOP
def on_limits_change(cur, fid, user=""):
    """An admin saved a new LOP or MOP for product `fid` (price_limits.py has already written lop / mop).
    Recalculates that product at once. Does NOT commit (price_limits.py commits). Returns recalc_product()'s dict."""
    log(f"[TRIGGER 2] {user or 'admin'} saved LOP / MOP for {fid} -> recalculating")
    return recalc_product(cur, fid, reason="TRIGGER 2")
"""
Database triggers - prices follow AUTOMATICALLY, whoever changes them (scraper, Run 1, Run 2, a query by hand).
===============================================================================================================
Postgres itself does the work, so NOTHING can forget it:

  TRIGGER A  (one per competitor list: samawa_catalog, vperfumes_catalog, ...)
      a product's price or stock changes in a competitor's list
        -> the same price / stock is copied at once into competitor_table for every product matched to that link
           (competitor_table.<shop>_link = the list's product link)

  TRIGGER B  (on competitor_table)
      a competitor price / stock / link, our UAE price, LOP or MOP changes in competitor_table
        -> least price, least priced website and suggested price of THAT product are recalculated at once:
             suggested = cheapest in-stock competitor - X%  (rounded)  ->  <= LOP -> LOP  |  >= MOP -> MOP
           (same rule as suggested_price.py and app.py SUGGEST_SQL - if you change the rule, change all three)

  So:  V Perfumes list 93 -> 80   =>  card: V Perfumes 80  =>  least 80, suggested 78   (no link to open)

Not done here (a database cannot call Airtable): our UAE Price of AUTOMATIC brands still follows at the end of
each run (/competitor/update-least -> automatic brands) and at once after LOP / MOP Save.

Where things are (change them here):
  SECTION 1  settings      -> SHOP_LISTS (each competitor list: table, link column, price columns, stock column)
                              COMPETITORS (competitor_table prefixes, order = order of tied websites)
  SECTION 2  TRIGGER A     -> copy_sql(): SQL for one list
  SECTION 3  TRIGGER B     -> recalc_sql(): SQL for competitor_table
  SECTION 4  install()     -> creates / refreshes everything (app.py calls it once when the website starts)
  SECTION 5  remove()      -> switches everything off (python db_triggers.py remove)

Check in the database:  SELECT event_object_table, trigger_name FROM information_schema.triggers;
Run by hand:            python db_triggers.py            (install, reads FF_DATABASE_URL)
                        python db_triggers.py remove     (switch off)
"""

import os
import sys


def log(*a):
    """Print to the Render log (lines start with [DB-TRIGGERS])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
# (prefix in competitor_table, competitor list table, link column, price columns (first one not empty), stock column)
SHOP_LISTS = [
    ("samawa", "samawa_catalog",           "product_url", ["price"],                        "stock"),
    ("ff",     "french_fragrance_catalog", "product_url", ["price_inc_tax", "price"],       "stock"),
    ("bp",     "branded_perfume_catalog",  "bp_url",      ["bp_price_inc_tax", "bp_price"], "bp_stock"),
    ("es",     "essenzi_catalog",          "es_url",      ["es_price_inc_tax", "es_price"], "es_stock"),
    ("vp",     "vperfumes_catalog",        "product_url", ["price_inc_tax", "price"],       "stock"),
    ("aj",     "alinjaz_catalog",          "product_url", ["price_inc_tax", "price"],       "stock"),
    ("on",     "ounass_catalog",           "product_url", ["price_inc_tax", "price"],       "stock"),
    ("am",     "amberiya_catalog",         "product_url", ["price_inc_tax", "price"],       "stock"),
    ("ng",     "nichegallerie_catalog",    "product_url", ["price_inc_tax", "price"],       "stock"),
    ("pd",     "perfumedubai_catalog",     "product_url", ["price_inc_tax", "price"],       "stock"),
    ("mh",     "mhgboutique_catalog",      "product_url", ["price_inc_tax", "price"],       "stock"),
    ("ep",     "emiratespride_catalog",    "product_url", ["price_inc_tax", "price"],       "stock"),
    ("an",     "anfasic_catalog",          "product_url", ["price_inc_tax", "price"],       "stock"),
    ("kh",     "khaltat_catalog",          "product_url", ["price_inc_tax", "price"],       "stock"),
    ("oe",     "oudelite_catalog",         "product_url", ["price_inc_tax", "price"],       "stock"),
]
# (label shown as least priced website, prefix) - same order as app.py LEAST_ORDER / LEAST_SQL
COMPETITORS = [("Samawa", "samawa"), ("French Fragrance", "ff"), ("Branded Perfume", "bp"),
               ("Essenzi", "es"), ("V Perfumes", "vp"), ("Al Injaz", "aj"),
               ("Ounass", "on"), ("Amberiya", "am"), ("Niche Gallerie", "ng"),
               ("Perfume Dubai", "pd"), ("MHG Boutique", "mh"), ("Emirates Pride", "ep"),
               ("Anfasic", "an"), ("Khaltat", "kh"), ("Oud Elite", "oe")]
RATE_KEY = "discount_rate"        # Settings -> Auto discount rate (app_settings)
DECIMALS = 0                      # rounding of the suggested price (same as suggested_price.py DECIMALS)


# ============================================================ SECTION 2: TRIGGER A - competitor list -> competitor_table
def copy_sql(prefix, table, link_col, price_cols, stock_col):
    """SQL that makes TRIGGER A for one competitor list."""
    new_price = "COALESCE(" + ", ".join(f"NEW.{c}" for c in price_cols) + f", c.{prefix}_price)"  # empty -> keep
    new_stock = f"(COALESCE(NEW.{stock_col}, '') = 'In stock')"     # Out of stock / Not on site -> FALSE
    fn = f"copy_{prefix}_to_competitor"
    return f"""
CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- {table}: price / stock of one product changed -> same values into competitor_table (matched products only)
    UPDATE competitor_table c
       SET {prefix}_price = {new_price}, {prefix}_stock = {new_stock}, updated_at = NOW()
     WHERE c.{prefix}_link = NEW.{link_col}
       AND (c.{prefix}_price IS DISTINCT FROM {new_price} OR c.{prefix}_stock IS DISTINCT FROM {new_stock});
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS copy_to_competitor ON {table};
CREATE TRIGGER copy_to_competitor AFTER INSERT OR UPDATE OF {", ".join(price_cols + [stock_col])} ON {table}
    FOR EACH ROW EXECUTE FUNCTION {fn}();
CREATE INDEX IF NOT EXISTS competitor_table_{prefix}_link_idx ON competitor_table ({prefix}_link);
"""


# ============================================================ SECTION 3: TRIGGER B - recalculate one product
def recalc_sql():
    """SQL that makes TRIGGER B on competitor_table (BEFORE: the row is fixed before it is saved, no extra update)."""
    watched = ["uae_price", "lop", "mop"] + [f"{p}_{x}" for _, p in COMPETITORS for x in ("price", "stock", "link")]
    in_stock = {p: f"(NEW.{p}_stock AND NEW.{p}_link IS NOT NULL AND NEW.{p}_price > 0)" for _, p in COMPETITORS}
    best = "LEAST(" + ", ".join(f"CASE WHEN {in_stock[p]} THEN NEW.{p}_price END" for _, p in COMPETITORS) + ")"
    sites = ", ".join(["CASE WHEN NEW.uae_price > 0 AND NEW.uae_price = v_least THEN 'Fragrant Souq' END"] +
                      [f"CASE WHEN {in_stock[p]} AND NEW.{p}_price = v_least THEN '{label}' END"
                       for label, p in COMPETITORS])
    changed = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in watched)
    return f"""
CREATE OR REPLACE FUNCTION recalc_suggested_price() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_rate  NUMERIC;   -- Auto discount rate X in %
    v_best  NUMERIC;   -- cheapest IN-STOCK competitor
    v_least NUMERIC;   -- cheapest of our price + in-stock competitors
    v_raw   NUMERIC;   -- cheapest competitor - X%
    v_text  TEXT;
BEGIN
    -- nothing that matters changed (e.g. only least / suggested price written) -> keep the row as it is
    IF TG_OP = 'UPDATE' THEN
        IF NOT ({changed}) THEN
            RETURN NEW;
        END IF;
    END IF;

    -- 1. least price + every website with that price (our price always counts, competitors only in stock)
    v_best  := {best};
    v_least := LEAST(v_best, CASE WHEN NEW.uae_price > 0 THEN NEW.uae_price END);
    NEW.least_price := v_least;
    NEW.least_priced_website := CASE WHEN v_least IS NULL THEN NULL ELSE NULLIF(concat_ws(', ', {sites}), '') END;

    -- 2. Auto discount rate (Settings). Not saved / not a number -> suggested price left as it is
    v_text := NULL;
    IF to_regclass('app_settings') IS NOT NULL THEN
        SELECT value INTO v_text FROM app_settings WHERE key = '{RATE_KEY}';
    END IF;
    IF v_text IS NULL OR v_text !~ '^\\s*[0-9]+(\\.[0-9]+)?\\s*$' THEN
        RETURN NEW;
    END IF;
    v_rate := v_text::NUMERIC;

    -- 3. suggested price = cheapest competitor - X%, then LOP / MOP (no competitor in stock -> no suggested price)
    IF v_best IS NULL THEN
        NEW.suggested_price := NULL;
        RETURN NEW;
    END IF;
    v_raw := ROUND(v_best * (100 - v_rate) / 100, {DECIMALS});
    IF NEW.lop IS NOT NULL AND NEW.mop IS NOT NULL AND NEW.lop > NEW.mop THEN
        NULL;                                                   -- LOP > MOP (not allowed) -> limits ignored
    ELSIF NEW.lop IS NOT NULL AND v_raw <= NEW.lop THEN
        v_raw := NEW.lop;                                       -- <= LOP -> LOP
    ELSIF NEW.mop IS NOT NULL AND v_raw >= NEW.mop THEN
        v_raw := NEW.mop;                                       -- >= MOP -> MOP
    END IF;
    NEW.suggested_price := v_raw;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS recalc_suggested_price ON competitor_table;
CREATE TRIGGER recalc_suggested_price BEFORE INSERT OR UPDATE ON competitor_table
    FOR EACH ROW EXECUTE FUNCTION recalc_suggested_price();
"""


# ============================================================ SECTION 4: install (app.py calls this at start)
def _columns(cur, table):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
    return {c for (c,) in cur.fetchall()}


def install(cur):
    """Create / refresh both triggers. Safe to run again and again (CREATE OR REPLACE / DROP IF EXISTS).
    competitor_table must already exist with all columns (app.py runs CT_CREATE first). Does NOT commit."""
    cur.execute("SET LOCAL lock_timeout = '15s'")            # a long run holding the table -> give up, never hang
    ct_cols = _columns(cur, "competitor_table")
    needed = {"uae_price", "lop", "mop", "least_price", "least_priced_website", "suggested_price", "updated_at"} | \
             {f"{p}_{x}" for _, p in COMPETITORS for x in ("price", "stock", "link")}
    missing = needed - ct_cols
    if missing:
        log(f"[DB-TRIGGERS] competitor_table is missing {sorted(missing)} -> triggers NOT installed")
        return {"installed": False, "missing": sorted(missing)}
    cur.execute(recalc_sql())
    log("[DB-TRIGGERS] TRIGGER B on competitor_table: price / stock / LOP / MOP change -> least + suggested price")
    done = []
    for prefix, table, link_col, price_cols, stock_col in SHOP_LISTS:
        cols = _columns(cur, table)
        if not cols:
            log(f"[DB-TRIGGERS] {table} does not exist yet -> skipped (installed at the next start)")
            continue
        prices = [c for c in price_cols if c in cols]
        if link_col not in cols or stock_col not in cols or not prices:
            log(f"[DB-TRIGGERS] {table}: columns {link_col} / {price_cols} / {stock_col} not found -> skipped")
            continue
        cur.execute(copy_sql(prefix, table, link_col, prices, stock_col))
        done.append(table)
        log(f"[DB-TRIGGERS] TRIGGER A on {table}: {'/'.join(prices)} + {stock_col} -> competitor_table.{prefix}_*")
    log(f"[DB-TRIGGERS] Installed: competitor_table + {len(done)} competitor lists")
    return {"installed": True, "lists": done}


# ============================================================ SECTION 5: remove (switch off)
def remove(cur):
    """Remove both triggers (prices then only follow at the end of each run, like before). Does NOT commit."""
    cur.execute("DROP TRIGGER IF EXISTS recalc_suggested_price ON competitor_table")
    for prefix, table, *_ in SHOP_LISTS:
        cur.execute("SELECT to_regclass(%s)", (table,))
        if cur.fetchone()[0] is not None:
            cur.execute(f"DROP TRIGGER IF EXISTS copy_to_competitor ON {table}")
    log("[DB-TRIGGERS] All triggers removed")


if __name__ == "__main__":
    import psycopg2
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        sys.exit("Set FF_DATABASE_URL first")
    conn = psycopg2.connect(url, sslmode=os.environ.get("PGSSLMODE", "require"))
    cur = conn.cursor()
    remove(cur) if sys.argv[1:] == ["remove"] else install(cur)
    conn.commit()
    conn.close()
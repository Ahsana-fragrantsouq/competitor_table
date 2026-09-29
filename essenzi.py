"""
Essenzi catalog (essenzi.com -> Postgres french_fragrance_db, table essenzi_catalog)
Plugged into app.py as a Blueprint. Separate from Branded Perfume (branded_perfume.py is not used here).

Essenzi uses the same website system (CS-Cart) as French Fragrance, so every French Fragrance product
gets its Essenzi link by changing only the domain:
    https://frenchfragrance.com/perfumes/xyz-100ml/  ->  https://essenzi.com/perfumes/xyz-100ml/

Routes (open in the browser):
  GET /essenzi/load?secret=XXX    STEP 1: copy all French Fragrance products into essenzi_catalog
  GET /essenzi/status             progress of step 1

STEP 2 (open every Essenzi page -> price, inc-tax price, stock, size) runs on the PC:
  python es_scraper.py --test 5   then   python es_scraper.py
(essenzi.com blocks servers like Render, so it needs your own Chrome)

Env vars (already set on Render):
  FF_DATABASE_URL = Postgres URL ending in /french_fragrance_db
  RUN_SECRET      = same secret as the other /run URLs
"""

import os
import time
import threading
import traceback

import psycopg2
from flask import Blueprint, request, jsonify

essenzi_bp = Blueprint("essenzi", __name__)

FF_DOMAIN = "frenchfragrance.com"
ES_DOMAIN = "essenzi.com"

es_lock = threading.Lock()                       # only one Essenzi job at a time
es_state = {"running": False, "last": None}      # shown at /essenzi/status


def es_log(*args):
    print(*args, flush=True)


# ---------------------------------------------------------------- database
ES_CREATE = """
CREATE TABLE IF NOT EXISTS essenzi_catalog (
    id                SERIAL PRIMARY KEY,
    ff_id             INTEGER UNIQUE NOT NULL,     -- id of the product in french_fragrance_catalog
    ff_url            TEXT,                        -- French Fragrance link (source)
    gtin              TEXT,
    name              TEXT,
    volume            TEXT,
    es_url            TEXT,                        -- same page on essenzi.com
    es_price          NUMERIC(10,2),               -- price without VAT
    es_price_inc_tax  NUMERIC(10,2),               -- price with 5% VAT (the price customers pay)
    es_stock          TEXT,                        -- In stock / Out of stock / Not on site
    es_status         TEXT,                        -- ok / not_found ... (why)
    es_title          TEXT,                        -- product name on Essenzi
    es_size           TEXT,                        -- e.g. "100 ml" read from that name
    checked_at        TIMESTAMP,                   -- when the Essenzi page was last opened (PC scraper)
    updated_at        TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_es_gtin ON essenzi_catalog (gtin);
"""

# STEP 1: copy French Fragrance products (new -> insert, existing -> update details, keep Essenzi results)
ES_LOAD = f"""
INSERT INTO essenzi_catalog (ff_id, ff_url, gtin, name, volume, es_url)
SELECT id, product_url, gtin, name, volume, REPLACE(product_url, '{FF_DOMAIN}', '{ES_DOMAIN}')
FROM french_fragrance_catalog
WHERE product_url IS NOT NULL
ON CONFLICT (ff_id) DO UPDATE SET
    ff_url     = EXCLUDED.ff_url,
    gtin       = EXCLUDED.gtin,
    name       = EXCLUDED.name,
    volume     = EXCLUDED.volume,
    es_url     = EXCLUDED.es_url,
    updated_at = NOW();
"""


def es_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    es_log("[ES-DB] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


# ---------------------------------------------------------------- step 1
def es_load():
    """Create essenzi_catalog (first time) and copy all French Fragrance products into it."""
    started = time.time()
    st = {"step": "load", "status": "running"}
    es_state["last"] = st
    conn = es_conn()
    cur = conn.cursor()
    try:
        es_log("[ES-LOAD] Creating table essenzi_catalog (if not exists)")
        cur.execute(ES_CREATE)
        es_log("[ES-LOAD] Copying French Fragrance products ...")
        cur.execute(ES_LOAD)
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM essenzi_catalog")
        st["products"] = cur.fetchone()[0]
        st["status"] = "done"
    finally:
        st["seconds"] = round(time.time() - started, 1)
        cur.close()
        conn.close()
        es_log(f"[ES-LOAD DONE] {st}")


def es_worker():
    """Background thread for step 1. Always releases es_lock at the end, even after an error."""
    try:
        es_load()
    except Exception as ex:
        es_log(f"[ES-ERROR] {ex}\n{traceback.format_exc()}")
        es_state["last"] = {**(es_state["last"] or {}), "status": "error", "error": str(ex)}
    finally:
        es_state["running"] = False
        es_lock.release()


# ---------------------------------------------------------------- routes
@essenzi_bp.route("/essenzi/load")
def es_load_route():
    secret = os.environ.get("RUN_SECRET", "")
    if secret and request.args.get("secret") != secret:
        es_log("[ES-LOAD] Unauthorized attempt")
        return jsonify({"error": "unauthorized"}), 401
    if not es_lock.acquire(blocking=False):
        return jsonify({"error": "an Essenzi job is already running"}), 409
    es_state["running"] = True
    es_log("[ES-LOAD] Started")
    threading.Thread(target=es_worker, daemon=True).start()
    return jsonify({"started": True, "check_status": "/essenzi/status"}), 202


@essenzi_bp.route("/essenzi/status")
def es_status_route():
    return jsonify({"running": es_state["running"], "last": es_state["last"]})
"""
Load prices from Shopify  (Item Master: 5 market prices + their "Compare at" prices, FROM Shopify INTO our database)
===================================================================================================================
Shopify Admin -> Markets -> Catalogs (United Arab Emirates, Asia Price, Kuwait, America catlog, Qatar) shows per product
"Price in AED" + "Compare at price". This module reads exactly those values with the Shopify Admin GraphQL API
(env SHOPIFY_SHOP + SHOPIFY_API_TOKEN, same as 'Create in website' / shopify_create.py) and saves them in item_master:
    uae_price / uae_comparison_price, asia_price / asia_comparison_price, kuwait_..., america_..., qatar_...
Products are found by SKU (Shopify variant SKU = item_master.sku, any capitals / spaces).
  * catalog title -> column: item_edit.MARKET_NAMES (the same names the Edit button uses to SEND prices)
  * a product not in a catalog's price list -> that market is left as it is
  * UAE also falls back to the product's normal price / compare-at price (the UAE catalog uses the store price)
  * Compare at 0.00 or empty = no comparison price (saved empty)
  * values are written in the price list currency AS IS (Qatar = QAR, like the Airtable fields)
  * the loaded columns are marked in item_master.edited_fields ('shopify') -> "Load from Airtable" keeps them
  * UAE price is also copied to competitor_table.uae_price (+ uae_price_website = TRUE), only when it differs
    -> the Competitor table card and least / suggested price use the Shopify price

Where things are:
  SECTION 1  settings        -> PAGE_SIZE, THROTTLE_WAIT
  SECTION 2  Shopify reading -> sp_variants() (normal prices), sp_catalogs(), sp_price_list() (one catalog)
  SECTION 3  save            -> sp_save()
  SECTION 4  job + routes    -> POST /item-master/shopify-prices (admin button), GET /shopify-prices/status,
                                GET /shopify-prices/run?secret=, GET /shopify-prices/test?secret= (nothing saved; short links = allowed with ?secret= by auth.RUN_PATH)
  SECTION 5  page parts      -> SP_BUTTON, SP_JS (item_master.py puts them on the Item Master page)
"""

import os
import time
import threading
import traceback

from flask import Blueprint, request, jsonify, g

from auth import is_admin

shopify_prices_bp = Blueprint("shopify_prices", __name__)


def log(*a):
    """Print to the Render log (lines start with [SHOPIFY-PRICES])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
PAGE_SIZE = 100                     # Shopify rows per GraphQL call (cost stays well under the limit)
# ONE-TIME load (user's choice 2026-11-04): prices come from Shopify ONCE; after that Item Master is the master and
# Edit sends changes TO Shopify (item_edit.SEND_PRICES). So: no button on the page, and a 2nd load is refused
# (it would overwrite prices edited since) unless ?force=1. The finished load is remembered in app_settings.
SHOW_BUTTON = False                 # True = "Load prices from Shopify" button on Item Master again
DONE_KEY = "shopify_prices_loaded"  # app_settings row written after a successful load
THROTTLE_WAIT = 5                   # seconds to wait when Shopify says THROTTLED (then try again, max 6 times)
_hooks = {}                         # init(connect) from app.py
_lock = threading.Lock()
state = {"running": False, "last": None}


# ============================================================ SECTION 2: Shopify reading
def _gql(query, variables=None):
    """GraphQL call with waits when Shopify is busy (THROTTLED). Returns data dict; raises on real errors."""
    import shopify_create as sc
    for attempt in range(6):
        res = sc.shopify_graphql(query, variables)
        errs = res.get("errors") or []
        if any("THROTTLED" in str(e) for e in errs):
            log(f"[SHOPIFY-PRICES] Shopify busy (THROTTLED) -> waiting {THROTTLE_WAIT}s (try {attempt + 1})")
            time.sleep(THROTTLE_WAIT)
            continue
        if errs:
            raise RuntimeError(f"Shopify: {str(errs)[:300]}")
        return res.get("data") or {}
    raise RuntimeError("Shopify stayed busy (THROTTLED) - try again in a minute")


def _num(m):
    """Shopify money -> float, None when empty / 0 (Compare at 0.00 = no comparison price)."""
    try:
        v = float((m or {}).get("amount") if isinstance(m, dict) else m)
    except (TypeError, ValueError):
        return None
    return round(v, 2) if v > 0 else None


def _key(sku):
    """SKU -> compare key (capitals, no spaces)."""
    return "".join(str(sku or "").split()).upper()


VARIANTS = """query($after: String) { productVariants(first: %d, after: $after) {
  pageInfo { hasNextPage endCursor } nodes { sku price compareAtPrice } } }""" % PAGE_SIZE


def sp_variants(max_pages=None):
    """{SKU: (price, compare at)} = every product's NORMAL price (= UAE store price)."""
    out, after, pages = {}, None, 0
    while True:
        d = _gql(VARIANTS, {"after": after})["productVariants"]
        for n in d["nodes"]:
            if _key(n.get("sku")):
                out[_key(n["sku"])] = (_num(n.get("price")), _num(n.get("compareAtPrice")))
        pages += 1
        if not d["pageInfo"]["hasNextPage"] or (max_pages and pages >= max_pages):
            break
        after = d["pageInfo"]["endCursor"]
    log(f"[SHOPIFY-PRICES] normal prices: {len(out)} SKUs ({pages} pages)")
    return out


CATALOGS = """{ catalogs(first: 50, type: MARKET) { nodes { title status priceList { id currency } } } }"""
PRICES = """query($id: ID!, $after: String) { priceList(id: $id) { prices(first: %d, after: $after) {
  pageInfo { hasNextPage endCursor }
  nodes { variant { sku } price { amount } compareAtPrice { amount } } } } }""" % PAGE_SIZE


def sp_catalogs():
    """{catalog title: {"id", "currency"}} of the ACTIVE market catalogs."""
    out = {}
    for c in (_gql(CATALOGS).get("catalogs") or {}).get("nodes") or []:
        if c.get("status") == "ACTIVE" and c.get("priceList"):
            out[c["title"]] = c["priceList"]
    log("[SHOPIFY-PRICES] catalogs: " + ", ".join(f"{t} ({v.get('currency')})" for t, v in out.items()))
    return out


def sp_price_list(pl_id, title, max_pages=None):
    """{SKU: (price, compare at)} of ONE catalog's price list (what the catalog page shows)."""
    out, after, pages = {}, None, 0
    while True:
        d = ((_gql(PRICES, {"id": pl_id, "after": after}).get("priceList") or {}).get("prices")) or {}
        for n in d.get("nodes") or []:
            sku = _key((n.get("variant") or {}).get("sku"))
            if sku:
                out[sku] = (_num(n.get("price")), _num(n.get("compareAtPrice")))
        pages += 1
        if not (d.get("pageInfo") or {}).get("hasNextPage") or (max_pages and pages >= max_pages):
            break
        after = d["pageInfo"]["endCursor"]
    log(f"[SHOPIFY-PRICES] {title}: {len(out)} SKUs ({pages} pages)")
    return out


def sp_read(max_pages=None, progress=None):
    """All markets -> {column: {SKU: (price, compare)}} using item_edit.MARKET_NAMES (column -> catalog title)."""
    import item_edit
    progress = progress if progress is not None else {}
    lists = sp_catalogs()
    found = {}
    for col, title in item_edit.MARKET_NAMES.items():
        pl = lists.get(title)
        if not pl:
            log(f"[SHOPIFY-PRICES] catalog '{title}' ({col}) not found / not active in Shopify -> skipped")
            progress.setdefault("catalogs_missing", []).append(title)
            continue
        progress["step"] = f"reading catalog {title}"
        found[col] = sp_price_list(pl["id"], title, max_pages)
        progress.setdefault("catalogs", {})[title] = {"currency": pl.get("currency"), "products": len(found[col])}
    progress["step"] = "reading normal prices (UAE fallback)"
    base = sp_variants(max_pages)
    uae = found.setdefault("uae_price", {})
    for sku, v in base.items():                 # UAE catalog not listing a product -> its normal store price
        uae.setdefault(sku, v)
    return found


# ============================================================ SECTION 3: save
def sp_save(conn, found, user):
    """Write the prices into item_master (+ UAE into competitor_table). Returns counts."""
    import item_edit
    cols = list(item_edit.PRICE_COLS)
    rows = {}                                   # SKU -> {column: value}
    for col in cols:
        for sku, (price, cmp) in (found.get(col) or {}).items():
            r = rows.setdefault(sku, {})
            r[col] = price
            r[item_edit.COMPARE_OF[col]] = cmp
    cur = conn.cursor()
    cur.execute("CREATE TEMP TABLE sp_in (sku TEXT, col TEXT, val NUMERIC(12,2)) ON COMMIT DROP")
    from psycopg2.extras import execute_values
    execute_values(cur, "INSERT INTO sp_in VALUES %s",
                   [(sku, c, v) for sku, r in rows.items() for c, v in r.items()], page_size=2000)
    changed_items = 0
    all_cols = cols + [item_edit.COMPARE_OF[c] for c in cols]
    for c in all_cols:                          # one UPDATE per column (only rows that have this market)
        cur.execute(f"""UPDATE item_master im SET {c} = s.val,
                            edited_fields = COALESCE(im.edited_fields, '{{}}'::jsonb) || jsonb_build_object('{c}', 'shopify')
                        FROM sp_in s
                        WHERE s.col = %s AND UPPER(REGEXP_REPLACE(COALESCE(im.sku, ''), '\\s', '', 'g')) = s.sku
                          AND im.{c} IS DISTINCT FROM s.val""", (c,))
        changed_items += cur.rowcount
        log(f"[SHOPIFY-PRICES] item_master.{c}: {cur.rowcount} products changed")
    cur.execute("SELECT COUNT(DISTINCT im.fi_record_id) FROM item_master im JOIN (SELECT DISTINCT sku FROM sp_in) s "
                "ON UPPER(REGEXP_REPLACE(COALESCE(im.sku, ''), '\\s', '', 'g')) = s.sku")
    matched = cur.fetchone()[0]
    # UAE price also on the Competitor table (our price there; least / suggested follow by trigger)
    cur.execute("SELECT to_regclass('competitor_table') IS NOT NULL")
    ct_changed = 0
    if cur.fetchone()[0]:
        cur.execute("""UPDATE competitor_table ct SET uae_price = s.val, uae_price_website = TRUE, updated_at = NOW()
                       FROM sp_in s
                       WHERE s.col = 'uae_price' AND s.val IS NOT NULL
                         AND UPPER(REGEXP_REPLACE(COALESCE(ct.sku, ''), '\\s', '', 'g')) = s.sku
                         AND ct.uae_price IS DISTINCT FROM s.val""")
        ct_changed = cur.rowcount
    conn.commit()
    log(f"[SHOPIFY-PRICES] saved by {user}: {len(rows)} Shopify SKUs, {matched} matched in item_master, "
        f"{changed_items} values changed, competitor_table UAE price changed for {ct_changed}")
    return {"shopify_skus": len(rows), "matched_products": matched, "values_changed": changed_items,
            "competitor_table_uae_changed": ct_changed}


# ============================================================ SECTION 4: job + routes
def _ready():
    import shopify_create as sc
    if not (sc.SHOP and sc.TOKEN):
        return "SHOPIFY_SHOP / SHOPIFY_API_TOKEN are not set in Render -> Environment"
    if not _hooks:
        return "not ready (app.py did not call shopify_prices.init)"
    return None


def _job(user):
    st = {"status": "running", "by": user, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "step": "starting"}
    state["last"] = st
    t0 = time.time()
    try:
        found = sp_read(progress=st)
        st["step"] = "saving"
        conn = _hooks["connect"]()
        try:
            st.update(sp_save(conn, found, user))
        finally:
            conn.close()
        st["status"], st["step"] = "done", "done"
        conn = _hooks["connect"]()                  # remember: loaded once -> next load needs ?force=1
        try:
            conn.cursor().execute("""INSERT INTO app_settings (key, value, updated_by, updated_at)
                                     VALUES (%s, %s, %s, NOW())
                                     ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                                         updated_by = EXCLUDED.updated_by, updated_at = NOW()""",
                                  (DONE_KEY, f"{st['matched_products']} products", user))
            conn.commit()
        finally:
            conn.close()
    except Exception as ex:
        st["status"], st["error"] = "error", str(ex)[:300]
        log(f"[SHOPIFY-PRICES] ERROR {ex}\n{traceback.format_exc()}")
    finally:
        st["seconds"] = round(time.time() - t0, 1)
        state["running"] = False
        _lock.release()
        log(f"[SHOPIFY-PRICES] DONE {st}")


def _loaded_before():
    """(when, by) of the last finished load, or None."""
    conn = _hooks["connect"]()
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('app_settings') IS NOT NULL")
        if not cur.fetchone()[0]:
            return None
        cur.execute("SELECT updated_at, updated_by FROM app_settings WHERE key = %s", (DONE_KEY,))
        return cur.fetchone()
    finally:
        conn.close()


def _start(user, force=False):
    err = _ready()
    if err:
        return jsonify({"ok": False, "error": err}), 400
    before = _loaded_before()
    if before and not force:                        # one-time load already done
        log(f"[SHOPIFY-PRICES] refused: already loaded {before[0]} by {before[1]} (add force=1 to load again)")
        return jsonify({"ok": False, "error": f"Prices were already loaded from Shopify on {before[0]:%Y-%m-%d %H:%M} "
                        f"by {before[1]}. Loading again overwrites prices edited in Item Master since - "
                        "add &force=1 to the link if you really want that."}), 409
    if not _lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "already loading - wait for it to finish"}), 409
    state["running"] = True
    log(f"[SHOPIFY-PRICES] started by {user}")
    threading.Thread(target=_job, args=(user,), daemon=True).start()
    return jsonify({"ok": True, "started": True})


def _secret_ok():
    secret = os.environ.get("RUN_SECRET", "")
    return not secret or request.args.get("secret") == secret


@shopify_prices_bp.post("/item-master/shopify-prices")
def sp_button():
    """'Load prices from Shopify' button (admins)."""
    if not is_admin():
        return jsonify({"ok": False, "error": "admins only"}), 403
    return _start(g.get("user") or "admin", force=request.args.get("force") == "1")


@shopify_prices_bp.get("/shopify-prices/run")
def sp_run_route():
    """Same, with ?secret=RUN_SECRET (no login needed)."""
    if not _secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    return _start("run link", force=request.args.get("force") == "1")


@shopify_prices_bp.get("/shopify-prices/status")
def sp_status_route():
    return jsonify({"running": state["running"], "last": state["last"]})


@shopify_prices_bp.get("/shopify-prices/test")
def sp_test_route():
    """First page of every catalog + normal prices -> shows what WOULD be saved for 10 SKUs. Nothing is saved."""
    if not _secret_ok():
        return jsonify({"error": "unauthorized"}), 401
    err = _ready()
    if err:
        return jsonify({"ok": False, "error": err}), 400
    try:
        prog = {}
        found = sp_read(max_pages=1, progress=prog)
    except Exception as ex:
        return jsonify({"ok": False, "error": str(ex)[:300]}), 502
    skus = sorted({s for col in found.values() for s in col})[:10]
    sample = {s: {col: {"price": found[col][s][0], "compare_at": found[col][s][1]}
                  for col in found if s in found[col]} for s in skus}
    return jsonify({"ok": True, "catalogs": prog.get("catalogs"), "catalogs_missing": prog.get("catalogs_missing"),
                    "first_page_skus": {col: len(v) for col, v in found.items()}, "sample": sample})


def init(connect):
    """connect() -> new Postgres connection (app.pg_conn)."""
    _hooks["connect"] = connect
    log("[SHOPIFY-PRICES] Linked to app.py: Item Master 'Load prices from Shopify' (5 markets + compare-at)")


# ============================================================ SECTION 5: page parts (item_master.py)
SP_BUTTON_HTML = """{% if admin %}
  {# Load prices from Shopify (shopify_prices.py): 5 market prices + compare-at -> Item Master #}
  <button type="button" class="sp-btn" onclick="spLoad(this)">Load prices from Shopify</button>
  <span class="sp-msg" id="sp-msg"></span>
  {% endif %}"""
SP_BUTTON = SP_BUTTON_HTML if SHOW_BUTTON else ""   # one-time load: no button (use /shopify-prices/run?secret=)

SP_CSS = """ .sp-btn{background:#f2d675;color:#1c1b19;border:0;border-radius:10px;padding:8px 14px;font-weight:600;cursor:pointer;margin-left:10px}
 .sp-btn[disabled]{opacity:.5;cursor:default} .sp-msg{font-size:13px;margin-left:8px}
"""

SP_JS = """  // ---- Load prices from Shopify (shopify_prices.py): start, then show progress until done ----
  async function spLoad(btn) {
    if (!confirm("Load the 5 market prices + compare-at prices of ALL products from Shopify into Item Master?")) return;
    var msg = document.getElementById("sp-msg");
    btn.disabled = true; msg.textContent = "Starting ...";
    var r = await fetch("/item-master/shopify-prices", {method: "POST"});
    var d = await r.json().catch(function () { return {ok: false, error: "HTTP " + r.status}; });
    if (!d.ok && r.status !== 409) { msg.textContent = "Not started: " + d.error; btn.disabled = false; return; }
    console.log("[SHOPIFY-PRICES] started");
    var t = setInterval(async function () {
      var s = await (await fetch("/shopify-prices/status")).json();
      var l = s.last || {};
      msg.textContent = (l.step || "") + (l.status === "done" ? " - " + l.matched_products + " products, " +
                        l.values_changed + " prices changed. Reloading ..." : l.status === "error" ? " - ERROR " + l.error : " ...");
      if (!s.running) { clearInterval(t); btn.disabled = false; if (l.status === "done") setTimeout(function () { location.reload(); }, 2000); }
    }, 3000);
  }
"""

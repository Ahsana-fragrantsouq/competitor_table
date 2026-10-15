"""
LOP / MOP  (lowest / highest allowed suggested price, typed by hand on each product card of /competitors)
=========================================================================================================
Separate module for everything about LOP / MOP.

Rule (used for EVERY suggested price - written as functions in suggested_price.py):
    raw   = cheapest in-stock competitor price - Auto discount rate (discount_rate.py)
    final = raw <= LOP -> LOP   |   raw >= MOP -> MOP   |   otherwise raw
    empty LOP / MOP = no limit.  No competitor in stock -> no suggested price (LOP / MOP never invent one).
    Example (LOP 1500, MOP 1700): raw 1571 -> 1571 | raw 1455 -> 1500 | raw 1795 -> 1700

Where things are (change them here):
  SECTION 1  settings            -> MAX_PRICE (highest allowed number), SAVE_URL
  SECTION 2  database columns    -> LIMITS_CREATE: lop, mop, limits_by, limits_at in competitor_table
  SECTION 3  the rule            -> clamp_sql() for the SQL of ALL products, apply_limits() for ONE product
  SECTION 4  card on /competitors-> LIMITS_CSS (look), LIMITS_CARD_HTML (the 2 boxes + Save), LIMITS_JS (Save button)
  SECTION 5  save (route)        -> POST /competitor/limits: check, save, recalculate that product, Airtable if automatic
  SECTION 6  hooks from app.py   -> init(): app.py gives the functions this module needs (no circular import)

Used by:
  app.py               -> CT_CREATE + LIMITS_CREATE, SUGGEST_SQL uses clamp_sql(), least_for() uses apply_limits(),
                          registers limits_bp, calls init(...)
  competitors_page.py  -> puts LIMITS_CSS / LIMITS_CARD_HTML / LIMITS_JS into its page (placeholders @@LIMITS_...@@),
                          reads the lop / mop columns for the cards
"""

from flask import Blueprint, request, jsonify, g

from suggested_price import apply_lop_mop      # THE LOP / MOP rule (suggested_price.py SECTION 3)

limits_bp = Blueprint("price_limits", __name__)


# ============================================================ SECTION 1: settings
MAX_PRICE = 100000                 # LOP / MOP must be above 0 and below this
SAVE_URL = "/competitor/limits"    # Save button on the card sends here (SECTION 5)


def log(*a):
    """Print to the Render log (every line of this module starts with [LIMITS])."""
    print(*a, flush=True)


# ============================================================ SECTION 2: database columns (competitor_table)
# Added to app.py CT_CREATE, so they are created automatically (ADD COLUMN IF NOT EXISTS keeps existing data).
LIMITS_CREATE = """
-- LOP / MOP: lowest / highest allowed suggested price, typed by hand on /competitors (empty = no limit)
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS lop NUMERIC(10,2);
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS mop NUMERIC(10,2);
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS limits_by TEXT;          -- who saved LOP / MOP last
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS limits_at TIMESTAMP;     -- when
"""


# ============================================================ SECTION 3: the rule
def clamp_sql(expr, table_alias="c"):
    """SQL for ALL products (used in app.py SUGGEST_SQL): keep `expr` between the row's LOP and MOP.
    GREATEST / LEAST skip an empty (NULL) LOP / MOP = no limit."""
    return f"LEAST(GREATEST({expr}, {table_alias}.lop), {table_alias}.mop)"


def apply_limits(cur, fid, price):
    """For ONE product (used in app.py least_for): keep `price` between its LOP and MOP.
    price None (no competitor in stock) stays None - LOP / MOP never invent a price."""
    if price is None:
        return None
    cur.execute("SELECT lop, mop FROM competitor_table WHERE fi_record_id = %s", (fid,))
    lop, mop = cur.fetchone() or (None, None)
    # <= LOP -> LOP, >= MOP -> MOP, otherwise unchanged (suggested_price.py)
    try:
        final = apply_lop_mop(price, float(lop) if lop is not None else None, float(mop) if mop is not None else None)
    except ValueError as ex:                                      # LOP > MOP saved somehow -> limits ignored
        log(f"[LIMITS] {fid}: {ex} -> LOP / MOP ignored, suggested {price}")
        return price
    log(f"[LIMITS] {fid}: suggested {price} | LOP {lop} | MOP {mop} -> {final}"
        + (" (raised to LOP)" if final > price else " (lowered to MOP)" if final < price else ""))
    return final


# ============================================================ SECTION 4: card on /competitors (look, boxes, Save script)
# competitors_page.py replaces @@LIMITS_CSS@@, @@LIMITS_CARD@@ and @@LIMITS_JS@@ in its page with these.
# 4a. look of the boxes (colours come from the page: --card, --text, --line, --gold)
LIMITS_CSS = """ /* LOP / MOP boxes next to the suggested price */
 .lim-in{width:84px;background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:10px;
         padding:9px 8px;font-size:15px}
 .lim-btn{background:transparent;color:var(--gold);border:1px solid var(--gold);border-radius:10px;padding:9px 12px;
          font-weight:600;cursor:pointer}
 .lim-btn:disabled{opacity:.6;cursor:wait}
 .limnote{color:var(--gold);font-size:11px;margin-left:4px}
"""

# 4b. the two boxes + Save button, next to the Suggested price on each product card (r = one product row)
LIMITS_CARD_HTML = """        {# LOP / MOP: lowest / highest allowed suggested price for THIS product (empty = no limit).
           Save -> /competitor/limits (price_limits.py SECTION 5) saves them and recalculates the suggested price at once. #}
        <div class="limits"><div class="plabel">LOP / MOP
             {%- if r.suggested_price is not none and r.lop is not none and r.suggested_price == r.lop %} <span class="limnote">at LOP</span>
             {%- elif r.suggested_price is not none and r.mop is not none and r.suggested_price == r.mop %} <span class="limnote">at MOP</span>{% endif %}</div>
             <div class="upd">
               <input class="lim-in" type="number" step="0.01" min="0" id="lop-{{ r.fi }}" placeholder="LOP" title="LOP: lowest allowed price"
                      value="{{ '%g' % r.lop if r.lop is not none else '' }}">
               <input class="lim-in" type="number" step="0.01" min="0" id="mop-{{ r.fi }}" placeholder="MOP" title="MOP: highest allowed price"
                      value="{{ '%g' % r.mop if r.mop is not none else '' }}">
               <button type="button" class="lim-btn" onclick="saveLimits('{{ r.fi }}', this)">Save</button>
             </div>
             <div class="upd-msg" id="limmsg-{{ r.fi }}"></div></div>
"""

# 4c. Save button: checks LOP <= MOP, sends to SAVE_URL, shows the new suggested price on the card at once
LIMITS_JS = """  // ---- LOP / MOP Save: check, send to /competitor/limits, show the new suggested price at once ----
  async function saveLimits(fi, btn) {
    var lop = document.getElementById("lop-" + fi).value.trim(), mop = document.getElementById("mop-" + fi).value.trim();
    var msg = document.getElementById("limmsg-" + fi);
    if (lop !== "" && mop !== "" && parseFloat(lop) > parseFloat(mop)) {
      msg.className = "upd-msg out"; msg.textContent = "LOP cannot be higher than MOP"; return;
    }
    console.log("[LIMITS] Save " + fi + ": LOP=" + (lop || "-") + " MOP=" + (mop || "-"));
    btn.disabled = true; msg.className = "upd-msg gtin"; msg.textContent = "Saving...";
    try {
      var r = await fetch("/competitor/limits", {method: "POST", headers: {"Content-Type": "application/json"},
                                                 body: JSON.stringify({fi_record_id: fi, lop: lop, mop: mop})});
      var d = await r.json();
      if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
      var sp = document.getElementById("sp-" + fi);
      if (sp) sp.value = d.suggested_price !== null ? d.suggested_price : "";
      msg.className = "upd-msg in";
      msg.textContent = "Saved. Suggested price: " + (d.suggested_price !== null ? "AED " + d.suggested_price : "none (no competitor in stock)")
                        + (d.airtable ? " | sent to Airtable (automatic brand)" : "");
      console.log("[LIMITS] Saved " + fi + " -> suggested " + d.suggested_price);
    } catch (e) {
      msg.className = "upd-msg out"; msg.textContent = "Not saved: " + e.message;
    } finally { btn.disabled = false; }
  }

"""


# ============================================================ SECTION 5: save (POST /competitor/limits)
@limits_bp.post(SAVE_URL)
def limits_save():
    """Save LOP / MOP of ONE product and recalculate its suggested price at once.
    Body (JSON): {"fi_record_id": "recXXX", "lop": 1500 or "", "mop": 1700 or ""}   ("" = no limit)
    Automatic brand (Settings -> Automate) -> the new suggested price also goes to Airtable UAE Price."""
    body = request.get_json(silent=True) or {}
    fid = str(body.get("fi_record_id") or "").strip()
    user = g.get("user") or "unknown"
    if not HOOKS:
        log("[LIMITS] init() was not called by app.py -> cannot save")
        return jsonify({"ok": False, "error": "LOP / MOP not ready (app.py did not call price_limits.init)"}), 500

    # 5a. check the numbers
    def number(v, name):
        if v in (None, ""):
            return None                                  # empty box = no limit
        try:
            v = round(float(v), 2)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number")
        if not 0 < v < MAX_PRICE:
            raise ValueError(f"{name} must be between 0 and {MAX_PRICE:,}")
        return v
    try:
        lop, mop = number(body.get("lop"), "LOP"), number(body.get("mop"), "MOP")
        if not fid.startswith("rec"):
            raise ValueError("Wrong product")
        if lop is not None and mop is not None and lop > mop:
            raise ValueError("LOP cannot be higher than MOP")
    except ValueError as ex:
        log(f"[LIMITS] {user}: {fid} LOP={body.get('lop')!r} MOP={body.get('mop')!r} -> not saved: {ex}")
        return jsonify({"ok": False, "error": str(ex)}), 400

    # 5b. save + recalculate this product (least price, suggested price with the new limits)
    conn = HOOKS["connect"]()
    cur = conn.cursor()
    try:
        cur.execute(HOOKS["create_sql"])                 # makes sure the lop / mop columns exist
        cur.execute("""UPDATE competitor_table SET lop = %s, mop = %s, limits_by = %s, limits_at = NOW()
                       WHERE fi_record_id = %s""", (lop, mop, user, fid))
        if cur.rowcount == 0:
            return jsonify({"ok": False, "error": "Product not found"}), 404
        HOOKS["recalc_one"](cur, fid)                    # app.py least_for(): least + suggested price of this product
        cur.execute("SELECT suggested_price, least_price, least_priced_website, LOWER(brand), uae_price "
                    "FROM competitor_table WHERE fi_record_id = %s", (fid,))
        sugg, least, site, brand, ours = cur.fetchone()
        automatic = brand in HOOKS["automatic_brands"](cur)   # Settings -> Automate: brand ticked?
        conn.commit()
    finally:
        cur.close()
        conn.close()
    log(f"[LIMITS] {user} saved {fid}: LOP {lop} | MOP {mop} -> suggested {sugg}"
        + (" | automatic brand -> Airtable" if automatic and sugg else ""))

    # 5c. automatic brand: our UAE Price in Airtable follows the new suggested price
    pushed = False
    if automatic and sugg and ours != sugg:
        done, failed = HOOKS["push_prices"]({fid: float(sugg)}, tag="LIMITS")
        pushed = bool(done)
    return jsonify({"ok": True, "lop": lop, "mop": mop,
                    "suggested_price": float(sugg) if sugg is not None else None,
                    "least_price": float(least) if least is not None else None, "least_site": site,
                    "airtable": pushed})


# ============================================================ SECTION 6: hooks from app.py
# app.py calls init(...) once, after its functions exist. This module never imports app.py (no circular import).
HOOKS = {}


def init(connect, create_sql, recalc_one, automatic_brands, push_prices):
    """connect()                      -> new Postgres connection (app.py pg_conn)
    create_sql                       -> app.py CT_CREATE (creates / extends competitor_table)
    recalc_one(cur, fid)             -> app.py least_for: least + suggested price of ONE product
    automatic_brands(cur)            -> app.py auto_brands: list of brands on automatic (lower case)
    push_prices({fid: price}, tag)   -> app.py airtable_set_prices: write UAE Price to Airtable"""
    HOOKS.update(connect=connect, create_sql=create_sql, recalc_one=recalc_one,
                 automatic_brands=automatic_brands, push_prices=push_prices)
    log("[LIMITS] Linked to app.py: Save on LOP / MOP recalculates the suggested price at once")
"""
Skip automatic price  (checkbox at the bottom-right of each product card on /competitors)
=========================================================================================
Ticked  -> this product's UAE Price in Airtable is NEVER changed automatically, even when its brand is on
           automatic (Settings -> Automate). The suggested price is still calculated and shown as normal.
           "Update price" on the card (by hand) still works.
Unticked -> back to normal: if the brand is automatic, the suggested price goes to Airtable at once.

Every automatic Airtable update checks it:
  app.py auto_apply()            -> end of every run / update-least, discount rate saved, Automate "Update all"
  app.py automate_save_route()   -> the "products to update" count
  price_limits.py limits_save()  -> after LOP / MOP Save

Where things are (change them here):
  SECTION 1  settings + database column -> SAVE_URL, SKIP_CREATE (no_auto, no_auto_by, no_auto_at), NOT_SKIPPED_SQL
  SECTION 2  card on /competitors       -> SKIP_CSS (look), SKIP_CARD_HTML (the checkbox), SKIP_JS (saves on click)
  SECTION 3  save (route)               -> POST /competitor/no-auto
  SECTION 4  hooks from app.py          -> init()
"""

from flask import Blueprint, request, jsonify, g

skip_bp = Blueprint("skip_auto", __name__)


def log(*a):
    """Print to the Render log (lines start with [SKIP-AUTO])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings + database column
SAVE_URL = "/competitor/no-auto"     # the checkbox sends here (SECTION 3)

# Added to app.py CT_CREATE (created automatically, existing rows = FALSE = normal automation)
SKIP_CREATE = """
-- Skip automatic price: TRUE = never change this product's UAE Price automatically (checkbox on the card)
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS no_auto BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS no_auto_by TEXT;          -- who ticked / unticked it last
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS no_auto_at TIMESTAMP;     -- when
"""

# Put into every "which products get an automatic price" query: only products NOT ticked
NOT_SKIPPED_SQL = "NOT COALESCE(no_auto, FALSE)"


# ============================================================ SECTION 2: card on /competitors
# competitors_page.py puts these into its page in place of @@SKIP_CSS@@, @@SKIP_CARD@@, @@SKIP_JS@@
SKIP_CSS = """ /* bottom row of a card: "Show out of stock" on the left, "Skip automatic price" on the right */
 .cardfoot{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:10px}
 .cardfoot .outtoggle{margin-top:0}
 .noauto{margin-left:auto;display:inline-flex;align-items:center;gap:8px;color:var(--muted);font-size:14px;cursor:pointer}
 .noauto input{width:18px;height:18px;accent-color:#e0806c;cursor:pointer}
 .noauto.on{color:#e0806c;font-weight:600}
 .noauto-msg{font-size:12px;width:100%;text-align:right;min-height:0}
"""

SKIP_CARD_HTML = """    {# Skip automatic price (skip_auto.py): ticked = UAE Price of THIS product is never changed automatically #}
    <label class="noauto{{ ' on' if r.no_auto else '' }}" title="Ticked: the automatic price update never changes this product in Airtable, even if its brand is automatic">
      <input type="checkbox" id="na-{{ r.fi }}" {{ 'checked' if r.no_auto else '' }} onchange="saveNoAuto('{{ r.fi }}', this)">
      Skip automatic price</label>
    <div class="upd-msg noauto-msg" id="namsg-{{ r.fi }}"></div>
"""

SKIP_JS = """  // ---- Skip automatic price checkbox (skip_auto.py): saved at once, no Save button ----
  async function saveNoAuto(fi, box) {
    var msg = document.getElementById("namsg-" + fi), on = box.checked;
    console.log("[SKIP-AUTO] " + fi + " -> " + (on ? "skip automatic price" : "automatic price allowed again"));
    box.disabled = true; msg.className = "upd-msg noauto-msg gtin"; msg.textContent = "Saving...";
    try {
      var r = await fetch("/competitor/no-auto", {method: "POST", headers: {"Content-Type": "application/json"},
                                                  body: JSON.stringify({fi_record_id: fi, no_auto: on})});
      var d = await r.json();
      if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
      box.parentElement.classList.toggle("on", on);
      msg.className = "upd-msg noauto-msg in";
      msg.textContent = on ? "Saved: Airtable price will not change automatically"
                           : "Saved: automatic price allowed" + (d.airtable ? " - sent to Airtable now" : "");
      if (d.airtable && d.price !== null) { var our = document.getElementById("our-" + fi); if (our) our.textContent = "AED " + d.price.toFixed(2); }
    } catch (e) {
      box.checked = !on;                                   // not saved -> put the tick back
      msg.className = "upd-msg noauto-msg out"; msg.textContent = "Not saved: " + e.message;
    } finally { box.disabled = false; }
  }

"""


# ============================================================ SECTION 3: save (POST /competitor/no-auto)
@skip_bp.post(SAVE_URL)
def no_auto_save():
    """Body (JSON): {"fi_record_id": "recXXX", "no_auto": true | false}.
    Unticked + automatic brand -> the suggested price goes to Airtable at once (like before it was ticked)."""
    body = request.get_json(silent=True) or {}
    fid = str(body.get("fi_record_id") or "").strip()
    on = body.get("no_auto") is True
    user = g.get("user") or "unknown"
    if not HOOKS:
        log("[SKIP-AUTO] init() was not called by app.py -> cannot save")
        return jsonify({"ok": False, "error": "not ready (app.py did not call skip_auto.init)"}), 500
    if not fid.startswith(("rec", "new")):
        return jsonify({"ok": False, "error": "Wrong product"}), 400

    conn = HOOKS["connect"]()
    cur = conn.cursor()
    try:
        cur.execute(HOOKS["create_sql"])                 # makes sure the no_auto column exists
        cur.execute("""UPDATE competitor_table SET no_auto = %s, no_auto_by = %s, no_auto_at = NOW()
                       WHERE fi_record_id = %s RETURNING suggested_price, uae_price, LOWER(brand), sku""",
                    (on, user, fid))
        row = cur.fetchone()
        if not row:
            return jsonify({"ok": False, "error": "Product not found"}), 404
        sugg, ours, brand, sku = row
        automatic = brand in HOOKS["automatic_brands"](cur)    # Settings -> Automate: brand ticked?
        conn.commit()
    finally:
        cur.close()
        conn.close()
    log(f"[SKIP-AUTO] {user}: {sku or fid} ({brand}) -> "
        + ("SKIP automatic price (Airtable not changed automatically)" if on else "automatic price allowed again"))

    # unticked + automatic brand -> catch up now: UAE Price := suggested price
    pushed = False
    if not on and automatic and sugg and ours != sugg:
        log(f"[SKIP-AUTO] {sku or fid}: brand '{brand}' is automatic -> UAE Price {ours} -> {sugg} in Airtable now")
        done, failed = HOOKS["push_prices"]({fid: float(sugg)}, tag="SKIP-AUTO")
        pushed = bool(done)
    return jsonify({"ok": True, "no_auto": on, "airtable": pushed,
                    "price": float(sugg) if pushed else None})


# ============================================================ SECTION 4: hooks from app.py
HOOKS = {}


def init(connect, create_sql, automatic_brands, push_prices):
    """connect()                    -> new Postgres connection (app.py pg_conn)
    create_sql                     -> app.py CT_CREATE (creates / extends competitor_table)
    automatic_brands(cur)          -> app.py auto_brands: brands on automatic (lower case)
    push_prices({fid: price}, tag) -> app.py airtable_set_prices: write UAE Price to Airtable"""
    HOOKS.update(connect=connect, create_sql=create_sql, automatic_brands=automatic_brands, push_prices=push_prices)
    log("[SKIP-AUTO] Linked to app.py: 'Skip automatic price' checkbox ready")
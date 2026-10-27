"""
Edit OUR stock  (ADMIN ONLY - "Edit" next to "Stock N" on the Competitor table cards AND the Item Master cards)
==============================================================================================================
Save -> 1. SHOPIFY first: inventory "available" of this product (found by item_master.shopify_id, else its exact SKU)
           is SET to the new number at the location where the product is stocked (only one -> that one;
           none yet -> the shop's first location, like 'Create in website')
        2. only when Shopify said OK: our database
             competitor_table.our_stock_qty = number, our_stock = number > 0   ("Stock N" / "Out of stock" on the cards)
             item_master.stock_qty = number
If Shopify refuses, nothing is changed and the card shows the error. No Airtable.
Shown only for products that are on fragrantsouq.com (competitor_table.our_link). Non-admins: no button, route 403.
Needs Render env SHOPIFY_SHOP + SHOPIFY_API_TOKEN (same as 'Create in website', shopify_create.py).

Where things are (change them here):
  SECTION 1  settings      -> SAVE_URL, MAX_QTY
  SECTION 2  card parts    -> STOCK_CSS, stock_html() (Stock N + Edit + box), STOCK_JS (Save / Cancel)
  SECTION 3  Shopify       -> shopify_set_stock()
  SECTION 4  save (route)  -> POST /stock/edit
  SECTION 5  hooks         -> init(): database connection from app.py
"""

from flask import Blueprint, request, jsonify, g

from auth import is_admin

stock_bp = Blueprint("stock_edit", __name__)


def log(*a):
    """Print to the Render log (lines start with [STOCK-EDIT])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
SAVE_URL = "/stock/edit"
MAX_QTY = 100000                    # bigger numbers are refused (typing mistake)


# ============================================================ SECTION 2: card parts (competitors_page.py + item_master.py put them in)
STOCK_CSS = """ /* Edit our stock (admins only, stock_edit.py) */
 .stk-edit{background:none;border:0;color:var(--muted);font-size:12px;cursor:pointer;text-decoration:underline;padding:0;margin-left:6px}
 .stk-box{display:flex;gap:6px;justify-content:flex-end;flex-wrap:wrap;margin-top:6px}
 .stk-box[hidden]{display:none}
 .stk-box input{width:90px;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;
                padding:6px 8px;font-size:14px}
 .stk-box button{border-radius:8px;padding:6px 12px;font-weight:600;cursor:pointer;font-size:13px}
 .stk-save{background:#f2d675;color:#1c1b19;border:0}
 .stk-cancel{background:none;color:var(--muted);border:1px solid var(--line)}
 .stk-msg{font-size:12px;margin-top:4px} .stk-msg.err{color:#e0806c} .stk-msg.ok{color:#8fd19e}
"""


def stock_html(fid, cls):
    """'Stock N' line (+ Edit and its box for admins). fid = Jinja name of the product id on that page
    (r.fi on /competitors, r.fi_record_id on Item Master); cls = the CSS class the page already used for the line."""
    return ("""{% if r.our_link %}
        {# stock NUMBER on our website (our_stock_qty: Shopify admin via load-our-links / Refresh, or set here) #}
        <div class=\"""" + cls + """\"><span id="stk-{{ FID }}">Stock {{ r.our_stock_qty if r.our_stock_qty is not none else '-' }}</span>
        {%- if admin %}<button type="button" class="stk-edit" onclick="stockOpen('{{ FID }}')">Edit</button>{% endif %}</div>
        {% if admin %}{# Edit our stock: Shopify first, then our database (stock_edit.py) #}
        <div class="stk-box" id="stkbox-{{ FID }}" hidden>
          <input type="number" min="0" step="1" inputmode="numeric" id="stkin-{{ FID }}"
                 value="{{ r.our_stock_qty if r.our_stock_qty is not none else '' }}" placeholder="Stock">
          <button type="button" class="stk-save" onclick="stockSave('{{ FID }}', this)">Save</button>
          <button type="button" class="stk-cancel" onclick="stockClose('{{ FID }}')">Cancel</button>
        </div>
        <div class="stk-msg" id="stkmsg-{{ FID }}"></div>
        {% endif %}
        {% endif %}""").replace("FID", fid)


STOCK_JS = """  // ---- Edit our stock (admins only, stock_edit.py): Shopify first, then our database ----
  function stockOpen(fi) {
    document.getElementById("stkbox-" + fi).hidden = false;
    document.getElementById("stkmsg-" + fi).textContent = "";
    document.getElementById("stkin-" + fi).focus();
  }
  function stockClose(fi) { document.getElementById("stkbox-" + fi).hidden = true; }
  async function stockSave(fi, btn) {
    var input = document.getElementById("stkin-" + fi), msg = document.getElementById("stkmsg-" + fi);
    var qty = input.value.trim();
    if (!/^[0-9]+$/.test(qty)) { msg.className = "stk-msg err"; msg.textContent = "Enter a whole number (0 or more)."; return; }
    btn.disabled = true; msg.className = "stk-msg"; msg.textContent = "Updating Shopify...";
    try {
      var r = await fetch("@@SAVE_URL@@", {method: "POST", headers: {"Content-Type": "application/json"},
                                         body: JSON.stringify({fi_record_id: fi, qty: parseInt(qty, 10)})});
      var d = await r.json().catch(function () { return {ok: false, error: "HTTP " + r.status}; });
      if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
      document.getElementById("stk-" + fi).textContent = "Stock " + d.qty;
      stockClose(fi);
      msg.className = "stk-msg ok"; msg.textContent = "Updated in Shopify: " + d.qty;
      console.log("[STOCK-EDIT] " + fi + ": stock " + d.qty);
    } catch (e) {
      msg.className = "stk-msg err"; msg.textContent = "Not updated: " + e.message;
      console.log("[STOCK-EDIT] " + fi + ": ERROR " + e.message);
    } finally { btn.disabled = false; }
  }
""".replace("@@SAVE_URL@@", SAVE_URL)


# ============================================================ SECTION 3: Shopify
def shopify_set_stock(sku, shopify_id, qty):
    """Set 'available' of this SKU in Shopify to qty. Returns (True, location name) or (False, error text)."""
    import shopify_create as sc                      # Shopify helpers + env (same as 'Create in website')
    import item_edit                                 # _find_in_shopify: by shopify_id, else exact SKU
    if not (sc.SHOP and sc.TOKEN):
        return False, "SHOPIFY_SHOP / SHOPIFY_API_TOKEN are not set in Render -> Environment"
    found = item_edit._find_in_shopify(sc, sku, shopify_id)
    if not found:
        return False, f"SKU {sku} not found in Shopify"
    pid, vid, _ = found
    r = sc.shopify_rest("GET", f"variants/{vid}.json")
    if not r.ok:
        return False, f"variant not readable ({r.status_code}): {r.text[:150]}"
    item_id = (r.json().get("variant") or {}).get("inventory_item_id")
    if not item_id:
        return False, "no inventory item for this product in Shopify"
    # where the product is stocked: one location -> that one; none yet -> the shop's first location
    r = sc.shopify_rest("GET", f"inventory_levels.json?inventory_item_ids={item_id}")
    levels = (r.json().get("inventory_levels") or []) if r.ok else []
    if len(levels) > 1:
        log(f"[STOCK-EDIT] {sku}: stocked at {len(levels)} locations -> using the first ({levels[0].get('location_id')})")
    location_id = levels[0].get("location_id") if levels else None
    r = sc.shopify_rest("GET", "locations.json")
    locations = (r.json().get("locations") or []) if r.ok else []
    if location_id is None:
        if not locations:
            return False, "no locations found in Shopify"
        location_id = locations[0]["id"]
    name = next((l.get("name") for l in locations if l.get("id") == location_id), str(location_id))
    r = sc.shopify_rest("POST", "inventory_levels/set.json",
                        {"location_id": location_id, "inventory_item_id": item_id, "available": qty})
    if not r.ok:
        return False, f"stock not set ({r.status_code}): {r.text[:150]}"
    log(f"[STOCK-EDIT] {sku}: Shopify product {pid} variant {vid} -> available {qty} at {name}")
    return True, name


# ============================================================ SECTION 4: save (route)
_hooks = {}                                          # filled by init() (SECTION 5)


@stock_bp.post(SAVE_URL)
def stock_save():
    """Body (JSON): {"fi_record_id": "rec...", "qty": 7}. Shopify first; our database only when Shopify said OK."""
    if not is_admin():
        log(f"[STOCK-EDIT] Refused: {g.get('user')} is not an admin")
        return jsonify({"ok": False, "error": "admins only"}), 403
    body = request.get_json(silent=True) or {}
    fid = str(body.get("fi_record_id") or "").strip()
    qty = body.get("qty")
    if not fid.startswith(("rec", "new")):
        return jsonify({"ok": False, "error": "no product given"}), 400
    if isinstance(qty, bool) or not isinstance(qty, int) or not (0 <= qty <= MAX_QTY):
        return jsonify({"ok": False, "error": f"stock must be a whole number 0 - {MAX_QTY}"}), 400
    conn = _hooks["connect"]()
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('item_master') IS NOT NULL")
        has_im = cur.fetchone()[0]
        im_join = "LEFT JOIN item_master im ON im.fi_record_id = ct.fi_record_id" if has_im else ""
        cur.execute(f"SELECT {'COALESCE(NULLIF(TRIM(im.sku), %s), ct.sku), im.shopify_id' if has_im else 'ct.sku, NULL, %s'}, "
                    f"ct.our_link, ct.our_stock_qty FROM competitor_table ct {im_join} WHERE ct.fi_record_id = %s",
                    ("", fid))
        row = cur.fetchone()
        conn.commit()                                # no open transaction while Shopify answers
        if not row:
            return jsonify({"ok": False, "error": "product not found"}), 404
        sku, shopify_id, link, old = (row[0] or "").strip().upper(), (row[1] or "").strip(), row[2], row[3]
        if not link:
            return jsonify({"ok": False, "error": "not on fragrantsouq.com"}), 409
        if not sku:
            return jsonify({"ok": False, "error": "product has no SKU"}), 400
        log(f"[STOCK-EDIT] {g.get('user')} -> {fid} ({sku}): stock {old} -> {qty}")
        try:
            ok, info = shopify_set_stock(sku, shopify_id, qty)
        except Exception as ex:
            ok, info = False, f"Shopify error: {str(ex)[:150]}"
        if not ok:
            log(f"[STOCK-EDIT] {fid}: NOT updated - {info}")
            return jsonify({"ok": False, "error": info}), 502
        cur.execute("UPDATE competitor_table SET our_stock_qty = %s, our_stock = %s, updated_at = NOW() "
                    "WHERE fi_record_id = %s", (qty, qty > 0, fid))
        if has_im:
            cur.execute("UPDATE item_master SET stock_qty = %s WHERE fi_record_id = %s", (qty, fid))
        conn.commit()
    except Exception as ex:
        conn.rollback()
        log(f"[STOCK-EDIT] {fid}: ERROR {ex}")
        return jsonify({"ok": False, "error": str(ex)[:200]}), 500
    finally:
        conn.close()
    log(f"[STOCK-EDIT] Done: {fid} stock {qty} (Shopify location {info})")
    return jsonify({"ok": True, "qty": qty, "location": info})


# ============================================================ SECTION 5: hooks
def init(connect):
    """connect() -> new Postgres connection (app.pg_conn)."""
    _hooks["connect"] = connect
    log("[STOCK-EDIT] Linked to app.py: 'Edit' stock -> Shopify inventory + competitor_table / item_master")

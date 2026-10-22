"""
Edit OUR barcode  (ADMIN ONLY - "Edit" next to "Barcode" at the top of each product card on /competitors)
=========================================================================================================
Save -> 1. Airtable French Inventories: field "Barcode" of this product is changed FIRST
        2. only when Airtable said OK: competitor_table.barcode is changed too (so both always agree)
If Airtable refuses, nothing is changed and the card shows the error.
The new barcode is used by the next match runs (barcode matching) and shown on the card at once.

Non-admins do not see the button, and the server refuses them too (403).

Where things are (change them here):
  SECTION 1  settings      -> SAVE_URL, MIN_DIGITS / MAX_DIGITS (allowed barcode length)
  SECTION 2  card parts    -> BC_CSS, BC_HTML (Edit button + box), BC_JS (Save / Cancel)
  SECTION 3  save (route)  -> POST /competitor/barcode
  SECTION 4  hooks         -> init(): database connection + "write one field to Airtable" from app.py
"""

import re

from flask import Blueprint, request, jsonify, g

barcode_bp = Blueprint("barcode_edit", __name__)


def log(*a):
    """Print to the Render log (lines start with [BARCODE-EDIT])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
SAVE_URL = "/competitor/barcode"
MIN_DIGITS, MAX_DIGITS = 8, 14          # EAN-8 ... GTIN-14


# ============================================================ SECTION 2: card parts (competitors_page.py puts them in)
BC_CSS = """ /* Edit our barcode (admins only, barcode_edit.py) */
 .bc-edit{background:none;border:0;color:var(--muted);font-size:12px;cursor:pointer;text-decoration:underline;padding:0;margin-left:6px}
 .bc-box{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
 .bc-box[hidden]{display:none}
 .bc-box input{width:190px;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;
               padding:7px 9px;font-size:14px}
 .bc-box button{border-radius:8px;padding:7px 12px;font-weight:600;cursor:pointer;font-size:13px}
 .bc-save{background:#f2d675;color:#1c1b19;border:0}
 .bc-cancel{background:none;color:var(--muted);border:1px solid var(--line)}
 .bc-hint{align-self:center;color:var(--muted);font-size:12px}
"""

# r = product row (inside the card, right after the Brand / SKU / Barcode line)
BC_HTML = """    {# Edit our barcode: admins only (barcode_edit.py) - Airtable first, then this table #}
    {% if admin %}
    <div class="bc-box" id="bcbox-{{ r.fi }}" hidden>
      {# our barcode; when empty and all competitors show the same barcode -> that one (competitors_page.py
         add_link_barcodes, r.bc_suggest): check it, then Save, or change it first #}
      <input type="text" inputmode="numeric" id="bcin-{{ r.fi }}" value="{{ r.barcode or r.bc_suggest or '' }}" placeholder="Barcode (8-14 digits)">
      <button type="button" class="bc-save" onclick="saveBarcode('{{ r.fi }}', this)">Save</button>
      <button type="button" class="bc-cancel" onclick="cancelBarcode('{{ r.fi }}')">Cancel</button>
      {% if not r.barcode and r.bc_suggest %}<span class="bc-hint">Same barcode on all competitors - check it, then Save</span>{% endif %}
    </div>
    <div class="upd-msg" id="bcmsg-{{ r.fi }}"></div>
    {% endif %}
"""

# the small "Edit" button next to "Barcode ..." in the card's meta line
BC_BUTTON = """{% if admin %}<button type="button" class="bc-edit" onclick="openBarcode('{{ r.fi }}')">{{ 'Edit' if r.barcode else 'Add barcode' }}</button>{% endif %}"""

BC_JS = """  // ---- Edit our barcode (admins only, barcode_edit.py): Airtable first, then the competitor table ----
  function openBarcode(fi) {
    var box = document.getElementById("bcbox-" + fi);
    box.hidden = !box.hidden;
    if (!box.hidden) document.getElementById("bcin-" + fi).focus();
  }
  function cancelBarcode(fi) {                      // close without saving, old barcode back in the box
    var input = document.getElementById("bcin-" + fi);
    input.value = input.defaultValue;
    document.getElementById("bcbox-" + fi).hidden = true;
    document.getElementById("bcmsg-" + fi).textContent = "";
  }
  async function saveBarcode(fi, btn) {
    var bc = document.getElementById("bcin-" + fi).value.replace(/\\s/g, "");
    var msg = document.getElementById("bcmsg-" + fi);
    if (!/^\\d{8,14}$/.test(bc)) { msg.className = "upd-msg out"; msg.textContent = "Barcode = 8 to 14 digits"; return; }
    console.log("[BARCODE-EDIT] " + fi + " -> " + bc);
    btn.disabled = true; msg.className = "upd-msg gtin"; msg.textContent = "Saving to Airtable...";
    try {
      var r = await fetch("/competitor/barcode", {method: "POST", headers: {"Content-Type": "application/json"},
                                                  body: JSON.stringify({fi_record_id: fi, barcode: bc})});
      var d = await r.json();
      if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
      msg.className = "upd-msg in"; msg.textContent = "Saved in Airtable and here: " + d.barcode + " - reloading...";
      setTimeout(function () { location.reload(); }, 900);
    } catch (e) {
      msg.className = "upd-msg out"; msg.textContent = "Not saved: " + e.message;
    } finally { btn.disabled = false; }
  }

"""


# ============================================================ SECTION 3: save (POST /competitor/barcode)
@barcode_bp.post(SAVE_URL)
def barcode_save():
    """Body (JSON): {"fi_record_id": "recXXX", "barcode": "6085010092065"}"""
    user = g.get("user") or "unknown"
    if g.get("role") != "admin":                       # same check as auth.is_admin()
        log(f"[BARCODE-EDIT] {user} is not an admin -> refused")
        return jsonify({"ok": False, "error": "Only admins can edit the barcode"}), 403
    body = request.get_json(silent=True) or {}
    fid = str(body.get("fi_record_id") or "").strip()
    bc = re.sub(r"\s", "", str(body.get("barcode") or ""))
    if not fid.startswith(("rec", "new")):
        return jsonify({"ok": False, "error": "Wrong product"}), 400
    if not re.fullmatch(rf"\d{{{MIN_DIGITS},{MAX_DIGITS}}}", bc):
        return jsonify({"ok": False, "error": f"Barcode must be {MIN_DIGITS} to {MAX_DIGITS} digits"}), 400
    if not HOOKS:
        return jsonify({"ok": False, "error": "not ready (app.py did not call barcode_edit.init)"}), 500

    conn = HOOKS["connect"]()
    cur = conn.cursor()
    try:
        cur.execute("SELECT barcode, sku FROM competitor_table WHERE fi_record_id = %s", (fid,))
        row = cur.fetchone()
        if not row:
            return jsonify({"ok": False, "error": "Product not found"}), 404
        old, sku = row
        # 1. Airtable first - if it refuses, nothing is changed anywhere
        ok, err = HOOKS["airtable_barcode"](fid, bc)
        if not ok:
            log(f"[BARCODE-EDIT] {user} {sku or fid}: Airtable refused {bc}: {err}")
            return jsonify({"ok": False, "error": f"Airtable: {err}"}), 502
        # 2. then our table
        cur.execute("UPDATE competitor_table SET barcode = %s, updated_at = NOW() WHERE fi_record_id = %s", (bc, fid))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    log(f"[BARCODE-EDIT] {user} {sku or fid}: barcode {old or '-'} -> {bc} (Airtable + competitor_table)")
    return jsonify({"ok": True, "barcode": bc, "old": old})


# ============================================================ SECTION 4: hooks from app.py
HOOKS = {}


def init(connect, airtable_barcode):
    """connect()                       -> new Postgres connection (app.py pg_conn)
    airtable_barcode(fid, barcode)    -> app.py airtable_set_barcode: writes the Barcode field, returns (ok, error)"""
    HOOKS.update(connect=connect, airtable_barcode=airtable_barcode)
    log("[BARCODE-EDIT] Linked to app.py: admins can edit our barcode (Airtable + competitor table)")
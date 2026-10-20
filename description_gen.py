"""
"Create product description" button  (ADMIN ONLY - Item Master cards, under the fragrantsouq.com link)
=====================================================================================================
Same job as the old Airtable automation script, but started from our website:
  1. read THIS product fresh from Airtable French Inventories (as text, like getCellValueAsString):
     Product Name, Brand, top_notes, middle_notes, base_notes, Items in Gift Set
  2. send them to the description API (DESC_API_URL, the same combined-codes.onrender.com/generate)
  3. the description that comes back is saved:
       a. Airtable French Inventories "Shopify Description"   (FIRST - Airtable stays the main copy)
       b. only when Airtable said OK: item_master.shopify_description (+ airtable_fields) in the database
If anything fails, nothing is changed and the card shows the error.
The description itself is not shown on the page (it is hidden) - read it with:
  python sql.py "SELECT sku, shopify_description FROM item_master WHERE sku = 'ASQ1028'"

Where things are (change them here):
  SECTION 1  settings      -> DESC_API_URL, field names, API timeout
  SECTION 2  card parts    -> DESC_CSS, DESC_HTML (button + message), DESC_JS (click -> POST)
  SECTION 3  steps         -> read_product(), gift_list(), call_api()
  SECTION 4  route         -> POST /item-master/description
  SECTION 5  hooks         -> init(): database connection + Airtable read / write from app.py
"""

import json

import requests
from flask import Blueprint, request, jsonify, g

from auth import is_admin

desc_bp = Blueprint("description_gen", __name__)


def log(*a):
    """Print to the Render log (lines start with [DESC])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
DESC_API_URL = "https://combined-codes.onrender.com/generate"     # same API as the Airtable automation
API_TIMEOUT = 110                    # seconds (the API may need time to wake up; website request limit is 120)
SAVE_URL = "/item-master/description"
F_NAME, F_BRAND = "Product Name", "Brand"                         # Airtable French Inventories field names
F_TOP, F_MIDDLE, F_BASE = "top_notes", "middle_notes", "base_notes"
F_GIFT = "Items in Gift Set"
F_DESC = "Shopify Description"                                    # where the new description is written
AT_TIMEZONE, AT_LOCALE = "Asia/Dubai", "en-gb"                    # needed by Airtable for "values as text"


# ============================================================ SECTION 2: card parts (item_master.py puts them in)
DESC_CSS = """ /* Create product description button (admins only, description_gen.py) */
 .desc-btn{display:block;margin:8px 0 0 auto;background:#f2d675;color:#1c1b19;border:0;border-radius:10px;padding:8px 14px;
           font-weight:600;font-size:14px;cursor:pointer}
 .desc-btn:disabled{opacity:.6;cursor:wait}
 .desc-msg{font-size:13px;margin-top:6px;max-width:320px;margin-left:auto}
 .desc-msg.ok{color:#8cc58c} .desc-msg.err{color:#e0806c}
 .desc-msg{white-space:normal}
 @media (max-width:640px){ .desc-msg,.desc-btn{margin-left:0} }
"""

# r = product row; goes right under the "View on fragrantsouq.com" link
DESC_HTML = """{% if admin %}
      <button type="button" class="desc-btn" data-has="{{ 1 if r.has_desc else 0 }}"
              onclick="createDesc('{{ r.fi_record_id }}', this)">Create product description</button>
      <div class="desc-msg" id="descmsg-{{ r.fi_record_id }}"></div>
      {% endif %}"""

DESC_JS = """  // ---- Create product description (admins only, description_gen.py) ----
  async function createDesc(fi, btn) {
    var msg = document.getElementById("descmsg-" + fi);
    if (btn.dataset.has === "1" &&
        !confirm("This product already has a Shopify description. Replace it with a new one?")) {
      console.log("[DESC] " + fi + ": cancelled (kept the old description)");
      return;
    }
    btn.disabled = true; btn.textContent = "Creating ... (up to 2 min)";
    msg.className = "desc-msg"; msg.textContent = "";
    console.log("[DESC] " + fi + ": asking the description API ...");
    try {
      var r = await fetch("@@SAVE_URL@@", {method: "POST", headers: {"Content-Type": "application/json"},
                                            body: JSON.stringify({fi_record_id: fi})});
      var d = await r.json().catch(function () { return {ok: false, error: "HTTP " + r.status}; });
      if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
      msg.className = "desc-msg ok";
      msg.textContent = "Description created (" + d.chars + " characters) and saved in Airtable + database.";
      btn.dataset.has = "1";
      console.log("[DESC] " + fi + ": done, " + d.chars + " characters");
    } catch (e) {
      msg.className = "desc-msg err"; msg.textContent = "Not created: " + e.message;
      console.log("[DESC] " + fi + ": ERROR " + e.message);
    }
    btn.disabled = false; btn.textContent = "Create product description";
  }
""".replace("@@SAVE_URL@@", SAVE_URL)


# ============================================================ SECTION 3: steps
_hooks = {}                          # filled by init() (SECTION 5)


def read_product(fid):
    """THIS record from Airtable, every value as text (cellFormat=string = Airtable's getCellValueAsString),
    so Brand / Items in Gift Set come back as names, not record ids. Returns the fields dict or None."""
    formula = "RECORD_ID()='" + fid.replace("'", "") + "'"
    recs = _hooks["read_airtable"](_hooks["fi_table"], {"filterByFormula": formula, "cellFormat": "string",
                                                         "timeZone": AT_TIMEZONE, "userLocale": AT_LOCALE})
    return recs[0].get("fields", {}) if recs else None


def gift_list(raw):
    """'Items in Gift Set' text -> list. Lines first, else commas, else one item (same rule as the old script)."""
    raw = (raw or "").strip()
    if not raw:
        return []
    parts = raw.splitlines() if "\n" in raw else raw.split(",") if "," in raw else [raw]
    return [p.strip() for p in parts if p.strip()]


def call_api(payload):
    """POST to the description API -> description text. Raises RuntimeError with a short reason."""
    try:
        r = requests.post(DESC_API_URL, json=payload, timeout=API_TIMEOUT)
    except requests.Timeout:
        raise RuntimeError(f"description API did not answer within {API_TIMEOUT}s - try again")
    except requests.RequestException as ex:
        raise RuntimeError(f"description API not reachable ({str(ex)[:80]})")
    if not r.ok:
        raise RuntimeError(f"description API error {r.status_code}: {r.text[:150]}")
    try:
        desc = (r.json().get("description") or "").strip()
    except ValueError:
        raise RuntimeError("description API sent something that is not JSON")
    if not desc:
        raise RuntimeError("description API returned no description")
    return desc


# ============================================================ SECTION 4: route
@desc_bp.post(SAVE_URL)
def create_description():
    """Button 'Create product description': Airtable -> API -> Airtable 'Shopify Description' -> database."""
    if not is_admin():
        log(f"[DESC] Refused: {g.get('user')} is not an admin")
        return jsonify({"ok": False, "error": "admins only"}), 403
    fid = ((request.get_json(silent=True) or {}).get("fi_record_id") or "").strip()
    if not fid.startswith("rec"):
        return jsonify({"ok": False, "error": "no product given"}), 400
    log(f"[DESC] {g.get('user')} -> create description for {fid}")
    try:
        f = read_product(fid)                                           # step 1: fresh from Airtable
        if f is None:
            raise RuntimeError("product not found in Airtable")
        payload = {"perfume_name": (f.get(F_NAME) or "").strip(), "brand_name": (f.get(F_BRAND) or "").strip(),
                   "top_notes": (f.get(F_TOP) or "").strip(), "middle_notes": (f.get(F_MIDDLE) or "").strip(),
                   "base_notes": (f.get(F_BASE) or "").strip(), "gift_items_list": gift_list(f.get(F_GIFT))}
        if not payload["perfume_name"]:
            raise RuntimeError("Product Name is empty in Airtable - cannot create a description")
        log(f"[DESC] {fid}: sending to API: {json.dumps(payload)[:300]}")
        desc = call_api(payload)                                        # step 2: the API writes it
        log(f"[DESC] {fid}: API returned {len(desc)} characters")

        ok, err = _hooks["write_airtable"](fid, {F_DESC: desc})          # step 3a: Airtable FIRST
        if not ok:
            raise RuntimeError(f"Airtable: {err}")
        conn = _hooks["connect"]()                                      # step 3b: then our database
        try:
            cur = conn.cursor()
            cur.execute("""UPDATE item_master SET shopify_description = %s,
                             airtable_fields = COALESCE(airtable_fields, '{}'::jsonb) || jsonb_build_object(%s, %s)
                           WHERE fi_record_id = %s""", (desc, F_DESC, desc, fid))
            saved = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        log(f"[DESC] {fid}: saved in Airtable '{F_DESC}'" + (" + item_master" if saved else
            " (not in item_master yet - comes with the next Load from Airtable)"))
        return jsonify({"ok": True, "chars": len(desc)})
    except Exception as ex:
        log(f"[DESC] {fid}: NOT created - {ex}")
        return jsonify({"ok": False, "error": str(ex)[:300]}), 502


# ============================================================ SECTION 5: hooks (called once from app.py)
def init(connect, read_airtable, write_airtable, fi_table):
    """connect()                       -> new Postgres connection (app.pg_conn)
    read_airtable(table, params)    -> list of Airtable records (app.at_list)
    write_airtable(fid, fields)     -> (True, None) or (False, error)  (app.airtable_update_fields)"""
    _hooks.update(connect=connect, read_airtable=read_airtable, write_airtable=write_airtable, fi_table=fi_table)
    log(f"[DESC] Linked to app.py: 'Create product description' -> {DESC_API_URL} -> Airtable '{F_DESC}'")

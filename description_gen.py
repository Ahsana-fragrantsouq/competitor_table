"""
"Create product description" button  (ADMIN ONLY - Item Master cards)
=====================================================================
Same job as the old Airtable automation script, but started from our website and WITHOUT AIRTABLE:
  1. read THIS product from OUR database (item_master): product_name, brand, top_notes, middle_notes, base_notes,
     gift_items (one per line)
  2. send them to the description API (DESC_API_URL, the same combined-codes.onrender.com/generate)
  3. the description that comes back is saved in item_master.shopify_description (+ desc_created_at = now,
     so "Load from Airtable" never overwrites it). ALSO_WRITE_AIRTABLE = True also copies it to Airtable.
If anything fails, nothing is changed and the card shows the error.
NOTE: item_master is only as fresh as the last "Load from Airtable now" (notes changed in Airtable -> load first).
The description itself is not shown on the page (it is hidden) - read it with:
  python sql.py "SELECT sku, shopify_description FROM item_master WHERE sku = 'ASQ1028'"

Where things are (change them here):
  SECTION 1  settings      -> DESC_API_URL, ALSO_WRITE_AIRTABLE, API timeout
  SECTION 2  card parts    -> DESC_CSS, DESC_HTML (button + message), DESC_JS (click -> POST)
  SECTION 3  steps         -> read_item(), gift_list(), call_api()
  SECTION 4  route         -> POST /item-master/description  (each step saved in the Run history, run_history.py)
  SECTION 5  hooks         -> init(): database connection (+ Airtable write, only used when ALSO_WRITE_AIRTABLE)
"""

import json

import requests
from flask import Blueprint, request, jsonify, g

from auth import is_admin
import run_history                           # every click + its steps -> Run history (/item-master/history)

desc_bp = Blueprint("description_gen", __name__)


def log(*a):
    """Print to the Render log (lines start with [DESC])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
DESC_API_URL = "https://combined-codes.onrender.com/generate"     # same API as the Airtable automation
API_TIMEOUT = 110                    # seconds (the API may need time to wake up; website request limit is 120)
SAVE_URL = "/item-master/description"
# OFF = Airtable is not used at all. True = the new description is ALSO copied to Airtable "Shopify Description"
# (only for the time both are still used; the database stays the main copy either way)
ALSO_WRITE_AIRTABLE = False
F_DESC = "Shopify Description"                                    # Airtable field (only for ALSO_WRITE_AIRTABLE)
READ_COLS = ["product_name", "brand", "top_notes", "middle_notes", "base_notes", "gift_items"]   # from item_master


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
  function descRunLink(msg, id) {                     // " · View run" -> this click in the Run history
    if (!id) return;
    var a = document.createElement("a");
    a.href = "/item-master/history?run=" + id; a.target = "_blank"; a.textContent = "View run";
    a.style.color = "inherit";
    msg.appendChild(document.createTextNode(" · ")); msg.appendChild(a);
  }
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
    var runId = null;
    try {
      var r = await fetch("@@SAVE_URL@@", {method: "POST", headers: {"Content-Type": "application/json"},
                                            body: JSON.stringify({fi_record_id: fi})});
      var d = await r.json().catch(function () { return {ok: false, error: "HTTP " + r.status}; });
      runId = d.run_id;
      if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
      msg.className = "desc-msg ok";
      msg.textContent = "Description created (" + d.chars + " characters) and saved in the database.";
      descRunLink(msg, runId);
      btn.dataset.has = "1";
      var web = document.getElementById("webbtn-" + fi);   // 'Create in website' needs a description -> now on
      if (web) { web.disabled = false; web.title = ""; }
      console.log("[DESC] " + fi + ": done, " + d.chars + " characters");
    } catch (e) {
      msg.className = "desc-msg err"; msg.textContent = "Not created: " + e.message;
      descRunLink(msg, runId);
      console.log("[DESC] " + fi + ": ERROR " + e.message);
    }
    btn.disabled = false; btn.textContent = "Create product description";
  }
""".replace("@@SAVE_URL@@", SAVE_URL)


# ============================================================ SECTION 3: steps
_hooks = {}                          # filled by init() (SECTION 5)


def read_item(fid):
    """THIS product from item_master (our database) as a dict, or None when it is not there."""
    conn = _hooks["connect"]()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(READ_COLS)} FROM item_master WHERE fi_record_id = %s", (fid,))
        row = cur.fetchone()
        return dict(zip(READ_COLS, row)) if row else None
    finally:
        conn.close()


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
    """Button 'Create product description': item_master -> API -> item_master.shopify_description. No Airtable.
    Every step is saved in the Run history (run_history.py, page /item-master/history)."""
    if not is_admin():
        log(f"[DESC] Refused: {g.get('user')} is not an admin")
        return jsonify({"ok": False, "error": "admins only"}), 403
    fid = ((request.get_json(silent=True) or {}).get("fi_record_id") or "").strip()
    if not fid.startswith("rec"):
        return jsonify({"ok": False, "error": "no product given"}), 400
    log(f"[DESC] {g.get('user')} -> create description for {fid}")
    run = run_history.Run("description", fid, g.get("user"))
    try:
        with run.step("Read the product from Item Master (our database)",                     # step 1
                      input={"table": "item_master", "columns": READ_COLS}) as st:
            f = read_item(fid)
            if f is None:
                raise RuntimeError("product not in Item Master - click 'Load from Airtable now' first")
            st["output"] = f
            run.product = f.get("product_name") or None
        payload = {"perfume_name": (f.get("product_name") or "").strip(), "brand_name": (f.get("brand") or "").strip(),
                   "top_notes": (f.get("top_notes") or "").strip(), "middle_notes": (f.get("middle_notes") or "").strip(),
                   "base_notes": (f.get("base_notes") or "").strip(), "gift_items_list": gift_list(f.get("gift_items"))}
        with run.step("Call the description API", input={"url": DESC_API_URL, "body": payload}) as st:  # step 2
            if not payload["perfume_name"]:
                raise RuntimeError("Product name is empty in Item Master - cannot create a description")
            log(f"[DESC] {fid}: sending to API: {json.dumps(payload)[:300]}")
            desc = call_api(payload)
            log(f"[DESC] {fid}: API returned {len(desc)} characters")
            st["output"] = {"characters": len(desc), "description": desc}

        with run.step("Save the description in Item Master (our database)",                  # step 3
                      input={"table": "item_master", "columns": ["shopify_description", "desc_created_at"]}) as st:
            conn = _hooks["connect"]()
            try:
                cur = conn.cursor()
                cur.execute("""UPDATE item_master SET shopify_description = %s, desc_created_at = NOW()
                               WHERE fi_record_id = %s""", (desc, fid))
                saved = cur.rowcount
                conn.commit()
            finally:
                conn.close()
            if not saved:
                raise RuntimeError("product not in Item Master any more - nothing saved")
            st["output"] = {"rows_updated": saved}

        if ALSO_WRITE_AIRTABLE:                                                            # optional copy
            with run.step("Copy to Airtable (ALSO_WRITE_AIRTABLE is on)",
                          input={"table": "French Inventories", "fields": {F_DESC: f"({len(desc)} characters)"}}) as st:
                ok, err = _hooks["write_airtable"](fid, {F_DESC: desc})
                if not ok:
                    raise RuntimeError(f"saved in our database, but the Airtable copy failed: {err}")
                st["output"] = {"updated": [F_DESC]}
        log(f"[DESC] {fid}: saved in item_master" + (" + Airtable" if ALSO_WRITE_AIRTABLE else " (Airtable not used)"))
        run.finish("success")
        return jsonify({"ok": True, "chars": len(desc), "run_id": run.id})
    except Exception as ex:
        log(f"[DESC] {fid}: NOT created - {ex}")
        run.finish("failed", ex)
        return jsonify({"ok": False, "error": str(ex)[:300], "run_id": run.id}), 502


# ============================================================ SECTION 5: hooks (called once from app.py)
def init(connect, write_airtable=None):
    """connect()                    -> new Postgres connection (app.pg_conn)
    write_airtable(fid, fields)  -> (True, None) or (False, error) - ONLY used when ALSO_WRITE_AIRTABLE is True"""
    _hooks.update(connect=connect, write_airtable=write_airtable)
    log(f"[DESC] Linked to app.py: 'Create product description' -> Item Master (database) -> {DESC_API_URL} -> "
        f"item_master" + (" + Airtable copy" if ALSO_WRITE_AIRTABLE else " (no Airtable)"))

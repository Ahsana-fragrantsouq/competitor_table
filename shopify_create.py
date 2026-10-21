"""
"Create in website" button  (ADMIN ONLY - Item Master cards, next to "Create product description")
==================================================================================================
Same job as the old Airtable automation "Create Product in Shopify", but started from our website and
WITHOUT AIRTABLE: every detail comes from OUR database (table item_master = the Item Master page).
  1. read THIS product from item_master: Product Name, Brand, Type, Category, SKU, Barcode, Size,
     Shopify Description, UAE Price, Qty given in shopify (stock_qty), ShopifyID
  2. send them to the same API: SHOP_API_URL (combined-codes.onrender.com/create-shopify-item)
     body = {"record_id": "rec...", "fields": {...}, "use_airtable": false}
     use_airtable=false tells the API (after its update) not to touch Airtable either (no ShopifyID write-back,
     no image lookup in Airtable). Image URLs = item_master.image_urls (links); none -> the API searches by name
  3. the Shopify ID that comes back is saved in item_master.shopify_id (our database, not Airtable)
The button is ON only when the product has a Shopify description (greyed out otherwise).
If the product already has a Shopify ID (item_master.shopify_id - ours, or ShopifyID imported by "Load from Airtable")
-> the card asks "Create again?" before anything is sent (no accidental duplicates).
NOTE: item_master is only as fresh as the last "Load from Airtable now" on the Item Master page.

Where things are (change them here):
  SECTION 1  settings      -> SHOP_API_URL, API timeout, ITEM_COLS (database column -> name sent to the API)
  SECTION 2  card parts    -> WEB_CSS, WEB_HTML (button + message), WEB_JS (click -> POST)
  SECTION 3  steps         -> read_item(), build_fields(), call_api(), save_shopify_id()
  SECTION 4  route         -> POST /item-master/create-in-website  (each step saved in the Run history, run_history.py)
  SECTION 5  hooks         -> init(connect): database connection from app.py
"""

import json

import requests
import psycopg2.extras
from flask import Blueprint, request, jsonify, g

from auth import is_admin
import run_history                           # every click + its steps -> Run history (/item-master/history)

web_bp = Blueprint("shopify_create", __name__)


def log(*a):
    """Print to the Render log (lines start with [WEB-CREATE])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
SHOP_API_URL = "https://combined-codes.onrender.com/create-shopify-item"   # same API as the Airtable automation
API_TIMEOUT = 110                    # seconds (API may need time to wake up; website request limit is 120)
SAVE_URL = "/item-master/create-in-website"
# item_master column -> name the API expects in "fields" (same names as the old Airtable script sent)
ITEM_COLS = {"product_name": "Product Name", "brand": "Brand", "type": "Type", "category": "Category",
             "sku": "SKU", "barcode": "Barcode", "size": "Size", "shopify_description": "ShopifyDesc"}
NUMBER_COLS = {"uae_price": "UAE Price", "stock_qty": "Qty given in shopify"}     # numbers, 0 when empty
F_SHOPIFY_ID = "ShopifyID"           # Airtable field name, kept inside item_master.airtable_fields
SHOPIFY_ID_SQL = "ALTER TABLE item_master ADD COLUMN IF NOT EXISTS shopify_id TEXT;"  # (also in item_master.FIELDS)


# ============================================================ SECTION 2: card parts (item_master.py puts them in)
WEB_CSS = """ /* Create in website button (admins only, shopify_create.py) */
 .web-btn:disabled{opacity:.45;cursor:not-allowed}
 .web-btn.busy:disabled{cursor:wait}
"""

# r = product row; goes under the "Create product description" button. OFF while there is no description.
WEB_HTML = """{% if admin %}
      <button type="button" class="desc-btn web-btn" id="webbtn-{{ r.fi_record_id }}"
              {% if not r.has_desc %}disabled title="Create the product description first"{% endif %}
              onclick="createWeb('{{ r.fi_record_id }}', this)">Create in website</button>
      <div class="desc-msg" id="webmsg-{{ r.fi_record_id }}"></div>
      {% endif %}"""

WEB_JS = """  // ---- Create in website (admins only, shopify_create.py) ----
  function webRunLink(msg, id) {                      // " · View run" -> this click in the Run history
    if (!id) return;
    var a = document.createElement("a");
    a.href = "/item-master/history?run=" + id; a.target = "_blank"; a.textContent = "View run";
    a.style.color = "inherit";
    msg.appendChild(document.createTextNode(" · ")); msg.appendChild(a);
  }
  async function createWeb(fi, btn, force) {
    var msg = document.getElementById("webmsg-" + fi);
    if (!force && !confirm("Create this product on fragrantsouq.com (Shopify) now?")) return;
    btn.disabled = true; btn.classList.add("busy"); btn.textContent = "Creating ... (up to 2 min)";
    msg.className = "desc-msg"; msg.textContent = "";
    console.log("[WEB-CREATE] " + fi + ": sending to the Shopify API" + (force ? " (again)" : ""));
    var again = false, runId = null;
    try {
      var r = await fetch("@@SAVE_URL@@", {method: "POST", headers: {"Content-Type": "application/json"},
                                            body: JSON.stringify({fi_record_id: fi, force: !!force})});
      var d = await r.json().catch(function () { return {ok: false, error: "HTTP " + r.status}; });
      runId = d.run_id;
      if (d.already) {                 // already has a Shopify ID -> ask before making a duplicate
        again = confirm("This product already has Shopify ID " + d.shopify_id +
                        " - it is probably on the website already. Create it AGAIN?");
        if (!again) { msg.textContent = "Not created (already on the website)."; webRunLink(msg, runId);
                      console.log("[WEB-CREATE] " + fi + ": cancelled"); }
      } else if (!d.ok) {
        throw new Error(d.error || ("HTTP " + r.status));
      } else {
        msg.className = "desc-msg ok";
        msg.textContent = "Created on the website" + (d.shopify_id ? " - Shopify ID " + d.shopify_id : "") + "." +
                          (d.warning ? " WARNING: " + d.warning + "." : "");
        webRunLink(msg, runId);
        console.log("[WEB-CREATE] " + fi + ": done, Shopify ID " + d.shopify_id);
      }
    } catch (e) {
      msg.className = "desc-msg err"; msg.textContent = "Not created: " + e.message;
      webRunLink(msg, runId);
      console.log("[WEB-CREATE] " + fi + ": ERROR " + e.message);
    }
    btn.disabled = false; btn.classList.remove("busy"); btn.textContent = "Create in website";
    if (again) createWeb(fi, btn, true);
  }
""".replace("@@SAVE_URL@@", SAVE_URL)


# ============================================================ SECTION 3: steps
_hooks = {}                          # filled by init() (SECTION 5)
_col_ready = False


def _conn():
    """Database connection; adds item_master.shopify_id once after each start."""
    global _col_ready
    conn = _hooks["connect"]()
    if not _col_ready:
        conn.cursor().execute(SHOPIFY_ID_SQL)
        conn.commit()
        _col_ready = True
    return conn


def read_item(fid):
    """THIS product from item_master (our database) as a dict, or None when it is not there."""
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"SELECT {', '.join(list(ITEM_COLS) + list(NUMBER_COLS))}, shopify_id, image_urls, loaded_at "
                    f"FROM item_master WHERE fi_record_id = %s", (fid,))
        return cur.fetchone()
    finally:
        conn.close()


def number(v):
    """Database number -> int when whole (7.00 -> 7), float otherwise, 0 when empty."""
    if v is None:
        return 0
    v = float(v)
    return int(v) if v.is_integer() else v


def build_fields(row):
    """item_master row -> the 'fields' the API expects (old script format).
    Image URLs = the links in item_master.image_urls (one per line); none -> the API searches images itself."""
    fields = {api: (row.get(col) or "").strip() for col, api in ITEM_COLS.items()}
    for col, api in NUMBER_COLS.items():
        fields[api] = number(row.get(col))
    fields["Image URLs"] = [u.strip() for u in (row.get("image_urls") or "").splitlines()
                            if u.strip().startswith("http")]
    return fields


def save_shopify_id(fid, sid):
    """Shopify ID of the new product -> item_master.shopify_id (our database). Returns rows updated."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE item_master SET shopify_id = %s WHERE fi_record_id = %s", (str(sid), fid))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


class ApiError(RuntimeError):
    """API answered with an error. .answer = its full answer (shown as the step's Output in the Run history)."""
    def __init__(self, msg, answer):
        super().__init__(msg)
        self.answer = answer


def call_api(fid, fields):
    """POST to the create-shopify-item API -> its JSON answer. Raises RuntimeError / ApiError with a short reason."""
    try:
        r = requests.post(SHOP_API_URL, json={"record_id": fid, "fields": fields, "use_airtable": False},
                          timeout=API_TIMEOUT)
    except requests.Timeout:
        raise RuntimeError(f"Shopify API did not answer within {API_TIMEOUT}s - check the website before "
                           f"trying again (it may have been created)")
    except requests.RequestException as ex:
        raise RuntimeError(f"Shopify API not reachable ({str(ex)[:80]})")
    log(f"[WEB-CREATE] {fid}: API answered {r.status_code}: {r.text[:300]}")
    if not r.ok:
        try:
            answer = r.json()
        except ValueError:
            answer = r.text
        # The API creates the product in Shopify FIRST and writes ShopifyID back to Airtable LAST.
        # An Airtable error in its answer = the product was most likely CREATED, only the Airtable write failed.
        if "api.airtable.com" in r.text:
            raise ApiError("Product was most likely CREATED on Shopify - only the API's last step failed "
                           "(writing ShopifyID back to Airtable). Check Shopify before clicking again. "
                           f"API answer {r.status_code}: {r.text[:200]}", answer)
        raise ApiError(f"Shopify API error {r.status_code}: {r.text[:200]}", answer)
    try:
        return r.json()
    except ValueError:
        return {}


# ============================================================ SECTION 4: route
@web_bp.post(SAVE_URL)
def create_in_website():
    """Button 'Create in website': item_master (our database) -> create-shopify-item API -> Shopify ID saved in
    item_master. No Airtable. Every step is saved in the Run history (run_history.py, page /item-master/history)."""
    if not is_admin():
        log(f"[WEB-CREATE] Refused: {g.get('user')} is not an admin")
        return jsonify({"ok": False, "error": "admins only"}), 403
    body = request.get_json(silent=True) or {}
    fid = (body.get("fi_record_id") or "").strip()
    if not fid.startswith("rec"):
        return jsonify({"ok": False, "error": "no product given"}), 400
    log(f"[WEB-CREATE] {g.get('user')} -> create on website: {fid}" + (" (again, confirmed)" if body.get("force") else ""))
    run = run_history.Run("website", fid, g.get("user"))
    if body.get("force"):
        run.note("Admin confirmed: create AGAIN although it already has a Shopify ID")
    try:
        with run.step("Read the product from Item Master (our database)",                    # step 1
                      input={"table": "item_master",
                             "columns": list(ITEM_COLS) + list(NUMBER_COLS) + ["shopify_id", "image_urls"]}) as st:
            row = read_item(fid)
            if row is None:
                raise RuntimeError("product not in Item Master - click 'Load from Airtable now' first")
            fields = build_fields(row)
            sid = row.get("shopify_id")
            run.product = fields["Product Name"] or fields["SKU"] or None
            st["output"] = {**{k: v for k, v in fields.items() if k != "ShopifyDesc"},
                            "ShopifyDesc": f"({len(fields['ShopifyDesc'])} characters)", F_SHOPIFY_ID: sid,
                            "item_master_loaded": row["loaded_at"].strftime("%Y-%m-%d %H:%M") if row.get("loaded_at") else None}

        with run.step("Check: description + product name present, not on the website yet") as st:  # step 2
            if not fields["ShopifyDesc"]:                                   # same rule as the button
                raise RuntimeError("no Shopify description - create the description first")
            if not fields["Product Name"]:
                raise RuntimeError("Product Name is empty in Item Master")
            st["output"] = {"description": "present", "product_name": "present",
                            F_SHOPIFY_ID: sid or "empty (not on the website yet)"}
        if sid not in (None, "") and not body.get("force"):
            log(f"[WEB-CREATE] {fid}: already has ShopifyID {sid} -> asking the admin first")
            run.note("Already has a Shopify ID -> stopped, asked the admin 'Create it AGAIN?'",
                     output={F_SHOPIFY_ID: str(sid)}, status="stopped")
            run.finish("stopped", f"already has Shopify ID {sid}")
            return jsonify({"ok": False, "already": True, "shopify_id": str(sid), "run_id": run.id})

        with run.step("Call the create-shopify-item API",                                   # step 3
                      input={"url": SHOP_API_URL,
                             "body": {"record_id": fid, "fields": fields, "use_airtable": False}}) as st:
            log(f"[WEB-CREATE] {fid}: sending {json.dumps({k: v for k, v in fields.items() if k != 'ShopifyDesc'})[:400]}"
                f" + description ({len(fields['ShopifyDesc'])} characters)")
            try:
                result = call_api(fid, fields)
            except ApiError as ex:
                st["output"] = {"api_answer": ex.answer}        # the API's full answer, for the Run history
                raise
            st["output"] = result
        sid = result.get("shopify_id") if isinstance(result, dict) else None
        log(f"[WEB-CREATE] {fid}: created on the website, Shopify ID {sid}")

        with run.step("Save the Shopify ID in Item Master (our database)",                 # step 4
                      input={"table": "item_master", "column": "shopify_id", "value": sid}) as st:
            if not sid:
                raise RuntimeError("the API did not send a Shopify ID back - check Shopify before clicking again")
            st["output"] = {"rows_updated": save_shopify_id(fid, sid)}
        run.finish("success")
        return jsonify({"ok": True, "shopify_id": sid, "run_id": run.id})
    except Exception as ex:
        log(f"[WEB-CREATE] {fid}: NOT created - {ex}")
        run.finish("failed", ex)
        return jsonify({"ok": False, "error": str(ex)[:300], "run_id": run.id}), 502


# ============================================================ SECTION 5: hooks
def init(connect):
    """connect() -> new Postgres connection (app.pg_conn). No Airtable here."""
    _hooks["connect"] = connect
    log(f"[WEB-CREATE] Linked to app.py: 'Create in website' -> Item Master (database) -> {SHOP_API_URL} (no Airtable)")

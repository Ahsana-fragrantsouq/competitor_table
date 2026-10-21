"""
"Create in website" button  (ADMIN ONLY - Item Master cards, under "Create product description")
=================================================================================================
Same job as the old Airtable automation "Create Product in Shopify", but started from our website:
  1. read THIS product fresh from Airtable French Inventories
       as text   : Product Name, Brand, Type, Category, SKU, Barcode, Size, Shopify Description
       as numbers: UAE Price, Qty given in shopify (0 when empty), Image URLs
  2. send them to the same API: SHOP_API_URL (combined-codes.onrender.com/create-shopify-item)
     body = {"record_id": "rec...", "fields": {...}}   - exactly the old script's body
  3. show the Shopify ID the API sends back (the API itself does the Shopify work, as before)
The button is ON only when the product has a Shopify description (greyed out otherwise).
If Airtable "ShopifyID" is already filled, the product is probably on the website already ->
the card asks "Create again?" before anything is sent (no accidental duplicates).

Where things are (change them here):
  SECTION 1  settings      -> SHOP_API_URL, field names, API timeout
  SECTION 2  card parts    -> WEB_CSS, WEB_HTML (button + message), WEB_JS (click -> POST)
  SECTION 3  steps         -> build_fields(), image_urls(), call_api()
  SECTION 4  route         -> POST /item-master/create-in-website  (each step saved in the Run history, run_history.py)
  SECTION 5  hooks         -> init() (Airtable read comes from description_gen.read_product)
"""

import json

import requests
from flask import Blueprint, request, jsonify, g

from auth import is_admin
import description_gen                       # read_product(): one Airtable record, as text or as numbers
import run_history                           # every click + its steps -> Run history (/item-master/history)

web_bp = Blueprint("shopify_create", __name__)


def log(*a):
    """Print to the Render log (lines start with [WEB-CREATE])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
SHOP_API_URL = "https://combined-codes.onrender.com/create-shopify-item"   # same API as the Airtable automation
API_TIMEOUT = 110                    # seconds (API may need time to wake up; website request limit is 120)
SAVE_URL = "/item-master/create-in-website"
# Airtable field -> name sent to the API (same as the old script). Read as TEXT:
TEXT_FIELDS = {"Product Name": "Product Name", "Brand": "Brand", "Type": "Type", "Category": "Category",
               "SKU": "SKU", "Barcode": "Barcode", "Size": "Size", "Shopify Description": "ShopifyDesc"}
NUMBER_FIELDS = ["UAE Price", "Qty given in shopify"]          # read as numbers, 0 when empty
F_IMAGES = "Image URLs"                                        # list, or text with commas
F_SHOPIFY_ID = "ShopifyID"                                     # filled = already on the website


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
      if (d.already) {                 // ShopifyID filled in Airtable -> ask before making a duplicate
        again = confirm("This product already has Shopify ID " + d.shopify_id +
                        " - it is probably on the website already. Create it AGAIN?");
        if (!again) { msg.textContent = "Not created (already on the website)."; webRunLink(msg, runId);
                      console.log("[WEB-CREATE] " + fi + ": cancelled"); }
      } else if (!d.ok) {
        throw new Error(d.error || ("HTTP " + r.status));
      } else {
        msg.className = "desc-msg ok";
        msg.textContent = "Created on the website" + (d.shopify_id ? " - Shopify ID " + d.shopify_id : "") + ".";
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
def first(v):
    """Lookup / rollup values come back as lists -> first value."""
    return (v[0] if v else None) if isinstance(v, list) else v


def image_urls(v):
    """'Image URLs': a list stays a list, text is split at commas (same rule as the old script)."""
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        return [u.strip() for u in v.split(",") if u.strip()]
    return []


def build_fields(text, raw):
    """Airtable values (text read + normal read) -> the 'fields' the API expects (old script format)."""
    fields = {api: (text.get(at) or "").strip() for at, api in TEXT_FIELDS.items()}
    for at in NUMBER_FIELDS:
        n = first(raw.get(at))
        fields[at] = n if isinstance(n, (int, float)) and not isinstance(n, bool) else 0
    fields[F_IMAGES] = image_urls(raw.get(F_IMAGES))
    return fields


def call_api(fid, fields):
    """POST to the create-shopify-item API -> its JSON answer. Raises RuntimeError with a short reason."""
    try:
        r = requests.post(SHOP_API_URL, json={"record_id": fid, "fields": fields}, timeout=API_TIMEOUT)
    except requests.Timeout:
        raise RuntimeError(f"Shopify API did not answer within {API_TIMEOUT}s - check the website before "
                           f"trying again (it may have been created)")
    except requests.RequestException as ex:
        raise RuntimeError(f"Shopify API not reachable ({str(ex)[:80]})")
    log(f"[WEB-CREATE] {fid}: API answered {r.status_code}: {r.text[:300]}")
    if not r.ok:
        raise RuntimeError(f"Shopify API error {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except ValueError:
        return {}


# ============================================================ SECTION 4: route
@web_bp.post(SAVE_URL)
def create_in_website():
    """Button 'Create in website': Airtable (fresh) -> create-shopify-item API -> Shopify ID back to the card.
    Every step is saved in the Run history (run_history.py, page /item-master/history)."""
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
        with run.step("Read the product from Airtable",                                   # step 1
                      input={"table": "French Inventories", "record_id": fid,
                             "as_text": list(TEXT_FIELDS), "as_numbers": NUMBER_FIELDS + [F_IMAGES, F_SHOPIFY_ID]}) as st:
            text = description_gen.read_product(fid, as_text=True)
            raw = description_gen.read_product(fid, as_text=False)
            if text is None or raw is None:
                raise RuntimeError("product not found in Airtable")
            fields = build_fields(text, raw)
            sid = first(raw.get(F_SHOPIFY_ID))
            run.product = fields["Product Name"] or fields["SKU"] or None
            st["output"] = {**{k: v for k, v in fields.items() if k != "ShopifyDesc"},
                            "ShopifyDesc": f"({len(fields['ShopifyDesc'])} characters)", F_SHOPIFY_ID: sid}

        with run.step("Check: description + product name present, not on the website yet") as st:  # step 2
            if not fields["ShopifyDesc"]:                                   # same rule as the button
                raise RuntimeError("no Shopify description - create the description first")
            if not fields["Product Name"]:
                raise RuntimeError("Product Name is empty in Airtable")
            st["output"] = {"description": "present", "product_name": "present",
                            F_SHOPIFY_ID: sid or "empty (not on the website yet)"}
        if sid not in (None, "") and not body.get("force"):
            log(f"[WEB-CREATE] {fid}: already has ShopifyID {sid} -> asking the admin first")
            run.note("Already has a Shopify ID -> stopped, asked the admin 'Create it AGAIN?'",
                     output={F_SHOPIFY_ID: str(sid)}, status="stopped")
            run.finish("stopped", f"already has Shopify ID {sid}")
            return jsonify({"ok": False, "already": True, "shopify_id": str(sid), "run_id": run.id})

        with run.step("Call the create-shopify-item API",                                   # step 3
                      input={"url": SHOP_API_URL, "body": {"record_id": fid, "fields": fields}}) as st:
            log(f"[WEB-CREATE] {fid}: sending {json.dumps({k: v for k, v in fields.items() if k != 'ShopifyDesc'})[:400]}"
                f" + description ({len(fields['ShopifyDesc'])} characters)")
            result = call_api(fid, fields)
            st["output"] = result
        sid = result.get("shopify_id") if isinstance(result, dict) else None
        log(f"[WEB-CREATE] {fid}: created on the website, Shopify ID {sid}")
        run.finish("success")
        return jsonify({"ok": True, "shopify_id": sid, "run_id": run.id})
    except Exception as ex:
        log(f"[WEB-CREATE] {fid}: NOT created - {ex}")
        run.finish("failed", ex)
        return jsonify({"ok": False, "error": str(ex)[:300], "run_id": run.id}), 502


# ============================================================ SECTION 5: hooks
def init():
    """Nothing to pass: Airtable reading is shared with description_gen.py (set up by description_gen.init)."""
    log(f"[WEB-CREATE] Linked to app.py: 'Create in website' -> {SHOP_API_URL}")

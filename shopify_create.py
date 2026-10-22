"""
"Create in website" button  (ADMIN ONLY - Item Master cards, next to "Create product description")
==================================================================================================
Creates the product in our Shopify store (fragrantsouq.com). NO AIRTABLE at all:
  * the product details come from OUR database (item_master = the Item Master page)
  * the Shopify steps are a COPY of Combined_codes/create_shopify_item.py (the API the Airtable automation calls),
    done here directly with the same Shopify calls - Combined_codes itself is not changed or called
  * the new Shopify ID is saved in item_master.shopify_id (instead of Airtable "ShopifyID")

Steps (each one is a step in the Run history, run_history.py):
  1. read the product from item_master: product_name, brand, type, category, sku, barcode, size,
     shopify_description, uae_price, stock_qty, image_urls, shopify_id
  2. check: description + product name present, no Shopify ID yet (else "Create AGAIN?")
  3. create the product (Shopify REST products.json) -> status "active" when stock > 0, else "draft"
     -> the Shopify ID is saved in item_master at once (so a later failure can never cause a duplicate)
  4. stock at the first Shopify location (inventory_levels/set.json)
  5. metafields: custom.size, custom.brands, google_shopping age_group / condition / gender / mpn
  6. UAE price on the UAE price list (same price lists as the API)
Images: item_master.image_urls (links); none -> search the Shopify Files by product name (same as the API).

Render -> Environment of THIS website needs (copy them from the combined-codes service):
  SHOPIFY_SHOP         e.g. yourstore.myshopify.com
  SHOPIFY_API_TOKEN    Admin API access token (never write it in code or chat)
  SHOPIFY_API_VERSION  optional, default 2025-01 (same default as the API)
NOTE: item_master is only as fresh as the last "Load from Airtable now".

Where things are (change them here):
  SECTION 1  settings      -> Shopify env vars, PRICE_LIST_IDS, ITEM_COLS (database column -> product field)
  SECTION 2  card parts    -> WEB_CSS, WEB_HTML (button + message), WEB_JS (click -> POST)
  SECTION 3  database      -> read_item(), build_fields(), save_shopify_id()
  SECTION 4  Shopify       -> shopify_rest/graphql, image_name(), search_images(), product_body(), set_metafield()
  SECTION 5  route         -> POST /item-master/create-in-website
  SECTION 6  hooks         -> init(connect): database connection from app.py
"""

import os
import re
import unicodedata

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
SHOP = os.environ.get("SHOPIFY_SHOP", "")
TOKEN = os.environ.get("SHOPIFY_API_TOKEN", "")
API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2025-01")
TIMEOUT = 40                         # seconds per Shopify request
SAVE_URL = "/item-master/create-in-website"
# same price lists as Combined_codes/create_shopify_item.py (only UAE gets a price: the old flow sent only UAE Price)
PRICE_LIST_IDS = {"UAE": "gid://shopify/PriceList/31168201019"}
CURRENCY = "AED"
SHOPIFY_CATEGORY = "Health & Beauty > Personal Care > Cosmetics > Perfumes & Colognes"
DEFAULT_WEIGHT_G = 850
# item_master column -> product field (same names the Airtable script sent to the API)
ITEM_COLS = {"product_name": "Product Name", "brand": "Brand", "type": "Type", "category": "Category",
             "sku": "SKU", "barcode": "Barcode", "size": "Size", "shopify_description": "ShopifyDesc"}
NUMBER_COLS = {"uae_price": "UAE Price", "stock_qty": "Qty given in shopify"}     # numbers, 0 when empty
SHOPIFY_ID_SQL = "ALTER TABLE item_master ADD COLUMN IF NOT EXISTS shopify_id TEXT;"  # (also in item_master.FIELDS)


# ============================================================ SECTION 2: card parts (item_master.py puts them in)
WEB_CSS = """ /* Create in website button (admins only, shopify_create.py) */
 .web-btn:disabled{opacity:.45;cursor:not-allowed}
 .web-btn.busy:disabled{cursor:wait}
"""

# r = product row; goes under the "Create product description" button. OFF while there is no description.
# HIDDEN when the product is already on our website (it has a "View on fragrantsouq.com" link = competitor_table.our_link)
WEB_HTML = """{% if admin and not r.our_link %}
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
        again = confirm("This product already has Shopify ID: " + d.shopify_id +
                        "\\n\\nIt is probably on the website already. Create it AGAIN?");
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


# ============================================================ SECTION 3: database (item_master)
_hooks = {}                          # filled by init() (SECTION 6)
_col_ready = False


def _conn():
    """Database connection; makes sure item_master.shopify_id exists (once after each start)."""
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
    """item_master row -> the product fields (same names / values the Airtable script used to send)."""
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


# ============================================================ SECTION 4: Shopify (copy of create_shopify_item.py)
def _headers():
    return {"Content-Type": "application/json", "X-Shopify-Access-Token": TOKEN}


def shopify_rest(method, path, body=None):
    """One Shopify Admin REST call -> requests.Response."""
    url = f"https://{SHOP}/admin/api/{API_VERSION}/{path}"
    return requests.request(method, url, headers=_headers(), json=body, timeout=TIMEOUT)


def shopify_graphql(query, variables=None):
    """One Shopify Admin GraphQL call -> answer (dict). Raises on HTTP errors (same as shopify_utils)."""
    r = requests.post(f"https://{SHOP}/admin/api/{API_VERSION}/graphql.json", headers=_headers(),
                      json={"query": query, **({"variables": variables} if variables else {})}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def image_name(product_title):
    """Product name -> Shopify image file name pattern (copy of convert_title_to_image_name in the API)."""
    s = unicodedata.normalize('NFKD', product_title).encode('ascii', 'ignore').decode('ascii')
    for old, new in {"Victor&Rolf": "Viktor_Rolf", "Van Cleef & Arpels": "Van_Cleef_Arpels",
                     "Dolce & Gabbana": "Dolce_Gabbana", "Yves Saint Laurent": "Yves_Saint_Laurent",
                     "Jean Paul Gaultier": "Jean_Paul_Gaultier", "Salvatore Ferragamo": "Salvatore_Ferragamo",
                     "Tonino Lamborghini": "Tonino_Lamborghini"}.items():
        s = s.replace(old, new)
    s = s.replace("'", "_").replace("â€™", "_").replace("Ãª", "e").replace("Ã©", "e").replace("&", "and")
    s = re.sub(r'([A-Za-z])(\d)', r'\1_\2', s)
    s = re.sub(r'(\d)([A-Za-z])', r'\1_\2', s)
    s = re.sub(r'([a-z])([A-Z])', r'\1_\2', s)
    s = re.sub(r'[®™°º"+]+', '', s)
    s = re.sub(r'[.,:/()\-]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip().replace(" ", "_")
    s = re.sub(r'_+', '_', s)
    s = re.sub(r'^[^A-Za]*', '', s)
    return s.strip('_')


def search_images(product_name, limit=5):
    """Images in the Shopify Files whose file name starts like the product name (same search as the API)."""
    pattern = f"filename:{image_name(product_name)}*"
    query = (f'query {{ files(first: {limit}, query: "{pattern} AND media_type:IMAGE") {{ edges {{ node {{ '
             f'... on MediaImage {{ id image {{ url }} }} }} }} }} }}')
    try:
        res = shopify_graphql(query)
    except Exception as ex:
        log(f"[WEB-CREATE] Image search failed: {ex}")
        return [], pattern
    edges = ((res.get("data") or {}).get("files") or {}).get("edges") or []
    urls = [e["node"]["image"]["url"] for e in edges if (e.get("node") or {}).get("image", {}).get("url")]
    return urls, pattern


def product_body(fields, image_urls):
    """Shopify products.json body - same as the API builds it."""
    qty = fields["Qty given in shopify"] or 0
    return {"product": {
        "title": fields["Product Name"].strip(),
        "body_html": fields["ShopifyDesc"],
        "vendor": fields["Brand"],
        "product_type": fields["Type"],
        "tags": fields["Category"],
        "status": "active" if qty > 0 else "draft",
        "category": SHOPIFY_CATEGORY,
        "variants": [{"sku": fields["SKU"], "price": str(fields["UAE Price"] or 0), "barcode": fields["Barcode"],
                      "weight": DEFAULT_WEIGHT_G, "weight_unit": "g",
                      "inventory_management": "shopify", "inventory_policy": "deny"}],
        "options": [{"name": "Title", "values": ["Default Title"]}],
        "images": [{"src": u} for u in image_urls[:10]],
    }}


METAFIELD_SET = """mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) { metafields { id namespace key value } userErrors { field message } } }"""

PRICE_ADD = """mutation priceListFixedPricesAdd($priceListId: ID!, $prices: [PriceListPriceInput!]!) {
  priceListFixedPricesAdd(priceListId: $priceListId, prices: $prices) {
    prices { price { amount currencyCode } variant { id } } userErrors { field message } } }"""


def set_metafield(owner_id, namespace, key, value):
    """One single_line_text_field metafield -> (ok, errors)."""
    res = shopify_graphql(METAFIELD_SET, {"metafields": [{"ownerId": owner_id, "namespace": namespace, "key": key,
                                                          "type": "single_line_text_field", "value": str(value)}]})
    errs = ((res.get("data") or {}).get("metafieldsSet") or {}).get("userErrors") or res.get("errors") or []
    return not errs, errs


# ============================================================ SECTION 5: route
def our_link(fid):
    """competitor_table.our_link of this product (its fragrantsouq.com page), or None."""
    conn = _hooks["connect"]()
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('competitor_table') IS NOT NULL")
        if not cur.fetchone()[0]:
            return None
        cur.execute("SELECT our_link FROM competitor_table WHERE fi_record_id = %s", (fid,))
        row = cur.fetchone()
        return (row[0] or None) if row else None
    except Exception as ex:                            # e.g. our_link column not created yet -> no block
        log(f"[WEB-CREATE] could not read our_link for {fid}: {ex}")
        return None
    finally:
        conn.close()


@web_bp.post(SAVE_URL)
def create_in_website():
    """Button 'Create in website': item_master -> Shopify (same steps as the API) -> item_master.shopify_id.
    No Airtable. Every step is saved in the Run history (run_history.py, page /item-master/history)."""
    if not is_admin():
        log(f"[WEB-CREATE] Refused: {g.get('user')} is not an admin")
        return jsonify({"ok": False, "error": "admins only"}), 403
    body = request.get_json(silent=True) or {}
    fid = (body.get("fi_record_id") or "").strip()
    if not fid.startswith(("rec", "new")):            # rec = from Airtable, new = Create a new item
        return jsonify({"ok": False, "error": "no product given"}), 400
    link = our_link(fid)                               # already on fragrantsouq.com -> never create it again
    if link:
        log(f"[WEB-CREATE] {fid}: refused - already on our website ({link})")
        return jsonify({"ok": False, "error": f"already on our website: {link}"}), 409
    log(f"[WEB-CREATE] {g.get('user')} -> create on website: {fid}" + (" (again, confirmed)" if body.get("force") else ""))
    run = run_history.Run("website", fid, g.get("user"))
    if body.get("force"):
        run.note("Admin confirmed: create AGAIN although it already has a Shopify ID")
    sid = None
    try:
        with run.step("Read the product from Item Master (our database)",                    # step 1
                      input={"table": "item_master",
                             "columns": list(ITEM_COLS) + list(NUMBER_COLS) + ["image_urls", "shopify_id"]}) as st:
            row = read_item(fid)
            if row is None:
                raise RuntimeError("product not in Item Master - click 'Load from Airtable now' first")
            fields = build_fields(row)
            old_sid = row.get("shopify_id")
            run.product = fields["Product Name"] or fields["SKU"] or None
            st["output"] = {**{k: v for k, v in fields.items() if k != "ShopifyDesc"},
                            "ShopifyDesc": f"({len(fields['ShopifyDesc'])} characters)", "shopify_id": old_sid,
                            "item_master_loaded": row["loaded_at"].strftime("%Y-%m-%d %H:%M") if row.get("loaded_at") else None}

        with run.step("Check: Shopify settings, description, product name, not on the website yet") as st:  # step 2
            if not (SHOP and TOKEN):
                raise RuntimeError("SHOPIFY_SHOP / SHOPIFY_API_TOKEN are not set in Render -> Environment of this website")
            if not fields["ShopifyDesc"]:
                raise RuntimeError("no Shopify description - create the description first")
            if not fields["Product Name"]:
                raise RuntimeError("Product name is empty in Item Master")
            st["output"] = {"shop": SHOP, "description": "present", "product_name": "present",
                            "shopify_id": old_sid or "empty (not on the website yet)"}
        if old_sid not in (None, "") and not body.get("force"):
            log(f"[WEB-CREATE] {fid}: already has Shopify ID {old_sid} -> asking the admin first")
            run.note("Already has a Shopify ID -> stopped, asked the admin 'Create it AGAIN?'",
                     output={"shopify_id": str(old_sid)}, status="stopped")
            run.finish("stopped", f"already has Shopify ID {old_sid}")
            return jsonify({"ok": False, "already": True, "shopify_id": str(old_sid), "run_id": run.id})

        images = fields["Image URLs"]
        if not images:                                                  # same as the API: search Shopify Files
            with run.step("Find images in Shopify Files (no image links in Item Master)",
                          input={"product_name": fields["Product Name"]}) as st:
                images, pattern = search_images(fields["Product Name"])
                st["output"] = {"search": pattern, "found": images}

        body_ = product_body(fields, images)
        with run.step("Create the product in Shopify",                                       # step 3
                      input={"url": f"https://{SHOP}/admin/api/{API_VERSION}/products.json",
                             "body": {"product": {**body_["product"], "body_html":
                                                  f"({len(fields['ShopifyDesc'])} characters)"}}}) as st:
            r = shopify_rest("POST", "products.json", body_)
            if r.status_code != 201:
                st["output"] = {"status": r.status_code, "answer": r.text[:3000]}
                raise RuntimeError(f"Shopify did not create the product ({r.status_code}): {r.text[:200]}")
            product = r.json().get("product", {})
            sid = product.get("id")
            variant = (product.get("variants") or [{}])[0]
            st["output"] = {"shopify_id": sid, "variant_id": variant.get("id"), "status": product.get("status"),
                            "images": len(product.get("images") or [])}
        log(f"[WEB-CREATE] {fid}: product created in Shopify, id {sid}")

        with run.step("Save the Shopify ID in Item Master (our database)",                   # right away: no duplicates
                      input={"table": "item_master", "column": "shopify_id", "value": sid}) as st:
            st["output"] = {"rows_updated": save_shopify_id(fid, sid)}

        variant_gid = f"gid://shopify/ProductVariant/{variant.get('id')}"
        product_gid = f"gid://shopify/Product/{sid}"
        qty = int(fields["Qty given in shopify"] or 0)
        with run.step("Set the stock", input={"available": qty}) as st:                      # step 4
            r = shopify_rest("GET", "locations.json")
            r.raise_for_status()
            locations = r.json().get("locations") or []
            if not locations:
                raise RuntimeError("no locations found in Shopify")
            r = shopify_rest("POST", "inventory_levels/set.json", {"location_id": locations[0]["id"],
                                                                   "inventory_item_id": variant.get("inventory_item_id"),
                                                                   "available": qty})
            st["output"] = {"location": locations[0].get("name"), "status": r.status_code,
                            "answer": r.json() if r.ok else r.text[:500]}
            if not r.ok:
                raise RuntimeError(f"stock not set ({r.status_code}): {r.text[:200]}")

        raw_gender = (fields["Category"] or "").strip().lower()
        metas = [(product_gid, "custom", "size", fields["Size"]), (product_gid, "custom", "brands", fields["Brand"]),
                 (variant_gid, "google_shopping", "age_group", "adult"),
                 (variant_gid, "google_shopping", "condition", "new"),
                 (variant_gid, "google_shopping", "gender", raw_gender if raw_gender in ("male", "female", "unisex") else "unisex"),
                 (variant_gid, "google_shopping", "mpn", fields["SKU"])]
        with run.step("Set the metafields", input={f"{ns}.{k}": v for _, ns, k, v in metas}) as st:   # step 5
            out = {}
            for owner, ns, k, v in metas:
                ok, errs = set_metafield(owner, ns, k, v)
                out[f"{ns}.{k}"] = "ok" if ok else errs
            st["output"] = out
            bad = [k for k, v in out.items() if v != "ok"]
            if bad:
                raise RuntimeError(f"metafields not set: {', '.join(bad)}")

        uae = fields["UAE Price"]
        if uae:
            with run.step("Set the UAE price list price", input={"price_list": PRICE_LIST_IDS["UAE"],
                                                                 "price": uae, "currency": CURRENCY}) as st:  # step 6
                res = shopify_graphql(PRICE_ADD, {"priceListId": PRICE_LIST_IDS["UAE"], "prices": [
                    {"variantId": variant_gid, "price": {"amount": str(uae), "currencyCode": CURRENCY}}]})
                errs = ((res.get("data") or {}).get("priceListFixedPricesAdd") or {}).get("userErrors") or res.get("errors")
                st["output"] = res
                if errs:
                    raise RuntimeError(f"UAE price not set: {errs}")

        run.finish("success")
        return jsonify({"ok": True, "shopify_id": sid, "run_id": run.id})
    except Exception as ex:
        log(f"[WEB-CREATE] {fid}: " + (f"created (id {sid}) but a later step failed - {ex}" if sid else f"NOT created - {ex}"))
        run.finish("failed", ex)
        msg = (f"Created in Shopify (ID {sid}), but a later step failed: {ex}" if sid else str(ex))
        return jsonify({"ok": False, "error": msg[:300], "shopify_id": sid, "run_id": run.id}), 502


# ============================================================ SECTION 6: hooks
def init(connect):
    """connect() -> new Postgres connection (app.pg_conn). No Airtable here."""
    _hooks["connect"] = connect
    log(f"[WEB-CREATE] Linked to app.py: 'Create in website' -> Item Master (database) -> Shopify "
        f"{SHOP or '(SHOPIFY_SHOP not set!)'} API {API_VERSION} (no Airtable, Combined_codes not used)")

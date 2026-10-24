"""
Edit competitor link  (ADMIN ONLY - "Edit link" under a competitor on the product cards of /competitors)
======================================================================================================
An admin can fix a wrong match by hand:
  Save    -> paste the correct product page of that shop. It must be in the shop's saved product list
             (the same list the matching uses). Its name, price and stock are copied, the link is marked
             CONFIRMED, so later match runs keep it (only price + stock are refreshed).
  Cancel  -> closes the box, nothing is changed.
  (The route still understands action "remove" - link removed and remembered as refused, app.py ct_match_shop
   checks <shop>_rejected - but there is no button for it on the page.)
After either: least + suggested price of the product are recalculated at once (and Airtable for automatic brands
at the end of the next run). French Fragrance -> Branded Perfume + Essenzi follow at once (same products),
unless their own link was set by an admin: Branded Perfume / Essenzi edited here are CONFIRMED and from then on
keep their own link (app.py ct_update_least / link_sisters_for skip <bp|es>_confirmed, only price + stock refreshed).

Non-admins do not see the button, and the server refuses them too (403).

Where things are (change them here):
  SECTION 1  settings      -> SHOP_LISTS (each shop's product list: table, name, price), SAVE_URL
  SECTION 2  card parts    -> EDIT_CSS, EDIT_HTML (button + box under a competitor link), EDIT_JS (Save / Cancel)
  SECTION 3  save (route)  -> POST /competitor/link
  SECTION 4  hooks         -> init(): functions from app.py (database, French Fragrance sisters, recalculation)
"""

from flask import Blueprint, request, jsonify, g

link_bp = Blueprint("link_edit", __name__)


def log(*a):
    """Print to the Render log (lines start with [LINK-EDIT])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
SAVE_URL = "/competitor/link"
# prefix in competitor_table -> (label, product list table, link column, name SQL, price SQL, stock column)
SHOP_LISTS = {
    "samawa": ("Samawa", "samawa_catalog", "product_url", "name", "price", "stock"),
    "ff": ("French Fragrance", "french_fragrance_catalog", "product_url", "name", "COALESCE(price_inc_tax, price)", "stock"),
    "bp": ("Branded Perfume", "branded_perfume_catalog", "bp_url", "COALESCE(bp_title, name)",
           "COALESCE(bp_price_inc_tax, bp_price)", "bp_stock"),
    "es": ("Essenzi", "essenzi_catalog", "es_url", "COALESCE(es_title, name)",
           "COALESCE(es_price_inc_tax, es_price)", "es_stock"),
    "vp": ("V Perfumes", "vperfumes_catalog", "product_url", "name", "COALESCE(price_inc_tax, price)", "stock"),
    "aj": ("Al Injaz", "alinjaz_catalog", "product_url", "name", "COALESCE(price_inc_tax, price)", "stock"),
    "on": ("Ounass", "ounass_catalog", "product_url", "CONCAT_WS(' ', brand, name)", "COALESCE(price_inc_tax, price)", "stock"),
    "am": ("Amberiya", "amberiya_catalog", "product_url", "CONCAT_WS(' ', brand, name)", "COALESCE(price_inc_tax, price)", "stock"),
    "ng": ("Niche Gallerie", "nichegallerie_catalog", "product_url", "name", "COALESCE(price_inc_tax, price)", "stock"),
    "pd": ("Perfume Dubai", "perfumedubai_catalog", "product_url", "CONCAT_WS(' ', brand, name)",
           "COALESCE(price_inc_tax, price)", "stock"),
    "mh": ("MHG Boutique", "mhgboutique_catalog", "product_url", "CONCAT_WS(' ', brand, name)",
           "COALESCE(price_inc_tax, price)", "stock"),
    "ep": ("Emirates Pride", "emiratespride_catalog", "product_url", "CONCAT_WS(' ', brand, name)",
           "COALESCE(price_inc_tax, price)", "stock"),
    "an": ("Anfasic", "anfasic_catalog", "product_url", "CONCAT_WS(' ', brand, name)",
           "COALESCE(price_inc_tax, price)", "stock"),
    "kh": ("Khaltat", "khaltat_catalog", "product_url", "CONCAT_WS(' ', brand, name)",
           "COALESCE(price_inc_tax, price)", "stock"),
}


# ============================================================ SECTION 2: card parts (competitors_page.py puts them in)
EDIT_CSS = """ /* Edit link (admins only): small button under a competitor link + the box it opens */
 .lnk-edit{background:none;border:0;color:var(--muted);font-size:12px;cursor:pointer;text-decoration:underline;padding:0}
 .lnk-box{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;margin-top:6px}
 .lnk-box[hidden]{display:none}
 .lnk-box input{flex:1 1 320px;min-width:0;background:var(--bg);color:var(--text);border:1px solid var(--line);
                border-radius:8px;padding:7px 9px;font-size:13px}
 .lnk-box button{border-radius:8px;padding:7px 12px;font-weight:600;cursor:pointer;font-size:13px}
 .lnk-save{background:#f2d675;color:#1c1b19;border:0}
 .lnk-cancel{background:none;color:var(--muted);border:1px solid var(--line)}
"""

# r = product row, k = shop prefix, label = shop name (inside competitors_page.py shop_block)
EDIT_HTML = """          {# Edit link: admins only (link_edit.py), every competitor #}
          {% if admin %}
          <div class="bcode"><button type="button" class="lnk-edit" onclick="openLink('{{ k }}', '{{ r.fi }}')">Edit link</button></div>
          <div class="lnk-box" id="lnk-{{ k }}-{{ r.fi }}" hidden>
            <input type="url" id="lnkin-{{ k }}-{{ r.fi }}" value="{{ r[k ~ '_url'] or r[k ~ '_suggestion'] or '' }}"
                   placeholder="Correct {{ label }} product link">
            <button type="button" class="lnk-save" onclick="saveLink('{{ k }}', '{{ r.fi }}', 'save', this)">Save</button>
            {# Cancel = close the box, nothing saved, the old link is put back in the box #}
            <button type="button" class="lnk-cancel" onclick="cancelLink('{{ k }}', '{{ r.fi }}')">Cancel</button>
          </div>
          <div class="upd-msg" id="lnkmsg-{{ k }}-{{ r.fi }}"></div>
          {% endif %}
"""

EDIT_JS = """  // ---- Edit link (admins only, link_edit.py): open the box, Save a new link or Remove the wrong one ----
  function openLink(k, fi) {
    var box = document.getElementById("lnk-" + k + "-" + fi);
    box.hidden = !box.hidden;
    if (!box.hidden) document.getElementById("lnkin-" + k + "-" + fi).focus();
  }
  function cancelLink(k, fi) {                       // close without saving, old link back in the box
    var input = document.getElementById("lnkin-" + k + "-" + fi);
    input.value = input.defaultValue;
    document.getElementById("lnk-" + k + "-" + fi).hidden = true;
    document.getElementById("lnkmsg-" + k + "-" + fi).textContent = "";
    console.log("[LINK-EDIT] cancel " + k + " " + fi);
  }
  async function saveLink(k, fi, action, btn) {
    var url = document.getElementById("lnkin-" + k + "-" + fi).value.trim();
    var msg = document.getElementById("lnkmsg-" + k + "-" + fi);
    if (action === "save" && !url) { msg.className = "upd-msg out"; msg.textContent = "Paste a link first"; return; }
    if (action === "remove" && !confirm("Remove this link? The next match will not put it back.")) return;
    console.log("[LINK-EDIT] " + action + " " + k + " " + fi + " " + url);
    btn.disabled = true; msg.className = "upd-msg gtin"; msg.textContent = "Saving...";
    try {
      var r = await fetch("/competitor/link", {method: "POST", headers: {"Content-Type": "application/json"},
                                               body: JSON.stringify({fi_record_id: fi, shop: k, action: action, url: url})});
      var d = await r.json();
      if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
      msg.className = "upd-msg in";
      msg.textContent = (action === "save" ? "Saved: " + d.name + " | AED " + d.price + " | " + d.stock : "Link removed")
                        + " - reloading...";
      setTimeout(function () { location.reload(); }, 900);
    } catch (e) {
      msg.className = "upd-msg out"; msg.textContent = "Not saved: " + e.message;
    } finally { btn.disabled = false; }
  }

"""


# ============================================================ SECTION 3: save (POST /competitor/link)
@link_bp.post(SAVE_URL)
def link_save():
    """Body (JSON): {"fi_record_id": "recXXX", "shop": "ff", "action": "save" | "remove", "url": "https://..."}"""
    user = g.get("user") or "unknown"
    if g.get("role") != "admin":                       # same check as auth.is_admin()
        log(f"[LINK-EDIT] {user} is not an admin -> refused")
        return jsonify({"ok": False, "error": "Only admins can edit links"}), 403
    body = request.get_json(silent=True) or {}
    fid, shop = str(body.get("fi_record_id") or ""), body.get("shop")
    action, url = body.get("action"), str(body.get("url") or "").strip()
    if shop not in SHOP_LISTS or action not in ("save", "remove") or not fid.startswith(("rec", "new")):
        return jsonify({"ok": False, "error": "Wrong request"}), 400
    if not HOOKS:
        return jsonify({"ok": False, "error": "not ready (app.py did not call link_edit.init)"}), 500
    label, table, link_col, name_sql, price_sql, stock_col = SHOP_LISTS[shop]

    conn = HOOKS["connect"]()
    cur = conn.cursor()
    try:
        cur.execute(HOOKS["create_sql"])
        cur.execute(f"SELECT {shop}_link FROM competitor_table WHERE fi_record_id = %s", (fid,))
        row = cur.fetchone()
        if not row:
            return jsonify({"ok": False, "error": "Product not found"}), 404
        old = row[0]

        # Branded Perfume / Essenzi have no suggestion / method / score / rejected columns -> only the basic ones
        sister = shop in ("bp", "es")
        if action == "remove" and sister:
            return jsonify({"ok": False, "error": "Remove is not available for Branded Perfume / Essenzi"}), 400
        if action == "remove":
            # removed + remembered as refused -> the next match run will not put it back
            cur.execute(f"""UPDATE competitor_table SET {shop}_link = NULL, {shop}_title = NULL, {shop}_price = NULL,
                               {shop}_stock = FALSE, {shop}_suggestion = NULL, {shop}_confirmed = FALSE,
                               {shop}_rejected = %s, {shop}_method = 'removed by ' || %s, updated_at = NOW()
                            WHERE fi_record_id = %s""", (old, user, fid))
            name, price, stock = None, None, None
        else:
            # the new link must be in the shop's saved product list (a '/' at the end does not matter)
            cur.execute(f"""SELECT {link_col}, {name_sql}, {price_sql}, {stock_col} FROM {table}
                            WHERE rtrim({link_col}, '/') = rtrim(%s, '/') LIMIT 1""", (url,))
            item = cur.fetchone()
            if not item:
                log(f"[LINK-EDIT] {user}: {url} not in {table}")
                return jsonify({"ok": False, "error": f"This link is not in the saved {label} product list. Open the "
                                                      f"product on {label} and copy the link from the address bar."}), 400
            link, name, price, stock = item
            extra = "" if sister else f", {shop}_suggestion = NULL, {shop}_method = 'set by ' || %s, {shop}_score = NULL"
            cur.execute(f"""UPDATE competitor_table SET {shop}_link = %s, {shop}_title = %s, {shop}_price = %s,
                               {shop}_stock = %s, {shop}_confirmed = TRUE{extra}, updated_at = NOW()
                            WHERE fi_record_id = %s""",
                        (link, name, price, stock == "In stock") + (() if sister else (user,)) + (fid,))
        if shop == "ff":
            HOOKS["link_sisters"](cur, fid)            # Branded Perfume + Essenzi follow the French Fragrance link
        HOOKS["recalc_one"](cur, fid)                  # least + suggested price of this product
        conn.commit()
    finally:
        cur.close()
        conn.close()
    log(f"[LINK-EDIT] {user} {action} {label} for {fid}: {old or '-'} -> "
        + (f"{url} ({name} | AED {price} | {stock})" if action == "save" else "removed (remembered as refused)"))
    return jsonify({"ok": True, "action": action, "name": name, "stock": stock,
                    "price": float(price) if price is not None else None})


# ============================================================ SECTION 4: hooks from app.py
HOOKS = {}


def init(connect, create_sql, link_sisters, recalc_one):
    """connect()                 -> new Postgres connection (app.py pg_conn)
    create_sql                  -> app.py CT_CREATE
    link_sisters(cur, fid)      -> app.py link_sisters_for: Branded Perfume + Essenzi follow French Fragrance
    recalc_one(cur, fid)        -> app.py least_for: least + suggested price of ONE product"""
    HOOKS.update(connect=connect, create_sql=create_sql, link_sisters=link_sisters, recalc_one=recalc_one)
    log("[LINK-EDIT] Linked to app.py: admins can edit competitor links on the cards")
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
  SECTION 2b shop pages    -> SHOP_CSS, SHOP_HTML, SHOP_JS: "Remove link" / "Add link" under the stock of every
                              competitor product on the shop pages (Competitors / Brands Store tabs)
  SECTION 3  save (route)  -> POST /competitor/link   (+ GET /competitor/our-products: search box of "Add link")
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
    "oe": ("Oud Elite", "oudelite_catalog", "product_url", "CONCAT_WS(' ', brand, name)",
           "COALESCE(price_inc_tax, price)", "stock"),
    "br": ("Brands Perfume", "brandsperfume_catalog", "product_url", "CONCAT_WS(' ', brand, name)",
           "COALESCE(price_inc_tax, price)", "stock"),
    "aq": ("Abdul Samad Al Qurashi", "abdulsamad_catalog", "product_url", "CONCAT_WS(' ', brand, name)",
           "COALESCE(price_inc_tax, price)", "stock"),
    "ao": ("Arabian Oud", "arabianoud_catalog", "product_url", "CONCAT_WS(' ', brand, name)",
           "COALESCE(price_inc_tax, price)", "stock"),
    "jz": ("Al Jazeera Perfumes", "aljazeera_catalog", "product_url", "CONCAT_WS(' ', brand, name)",
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


# ============================================================ SECTION 2b: shop pages - Remove link / Add link
# On a shop page (e.g. Competitors -> Samawa) every card = ONE competitor product. Admins see under the stock:
#   linked to our product(s)  -> "Linked to SKU ..." + "Remove link"  (= action "remove" above: link cleared + remembered
#                                as refused, the next match run will not put it back; not for Branded Perfume / Essenzi)
#   not linked (Missing item) -> "Add link": search our products (name / SKU / brand), pick one, Save
#                                (= action "save" above: name, price, stock copied, link CONFIRMED so matching keeps it,
#                                 least + suggested price recalculated, French Fragrance -> BP / ES follow)
# ON / OFF (user's choice 2026-11-03: OFF = no "Add link", "Remove link" or "Linked to SKU" on ANY shop page).
# True = shown again for admins. The routes stay (nothing else changes).
SHOP_LINK_BUTTONS = False
SEARCH_URL = "/competitor/our-products"
SHOP_CSS = """ /* Remove link / Add link on the shop pages (admins only, link_edit.py SECTION 2b) */
 .sl-wrap{margin-top:6px;text-align:right;font-size:12px;color:var(--muted)}
 .sl-linked{display:block;margin-top:2px}
 .sl-btn{background:none;border:0;color:var(--muted);font-size:12px;cursor:pointer;text-decoration:underline;padding:0;margin-left:6px}
 .sl-box{margin-top:6px;text-align:left}
 .sl-box[hidden]{display:none}
 .sl-box input{width:100%;box-sizing:border-box;background:var(--bg);color:var(--text);border:1px solid var(--line);
               border-radius:8px;padding:7px 9px;font-size:13px}
 .sl-list{max-height:220px;overflow:auto;margin-top:4px;border:1px solid var(--line);border-radius:8px}
 .sl-list:empty{display:none}
 .sl-opt{padding:6px 9px;cursor:pointer;font-size:13px;color:var(--text);border-bottom:1px solid var(--line)}
 .sl-opt:hover,.sl-opt.on{background:rgba(242,214,117,.15)}
 .sl-opt small{color:var(--muted)}
 .sl-btns{display:flex;gap:6px;justify-content:flex-end;margin-top:6px}
 .sl-btns button{border-radius:8px;padding:6px 12px;font-weight:600;cursor:pointer;font-size:13px}
 .sl-save{background:#f2d675;color:#1c1b19;border:0} .sl-save:disabled{opacity:.5;cursor:default}
 .sl-cancel{background:none;color:var(--muted);border:1px solid var(--line)}
 .sl-msg{margin-top:4px} .sl-msg.err{color:#e0806c} .sl-msg.ok{color:#8fd19e}
"""

# r = competitor product row of a shop page; loop.index makes the ids unique on the page
SHOP_HTML = """{% if admin and src.key in shop_lists and r.url %}
    <div class="sl-wrap" id="sl-{{ loop.index }}" data-url="{{ r.url }}" data-shop="{{ src.key }}"
         data-q="{{ (r.brand or '') ~ ' ' ~ (r.name or '') }}">
      {% for p in r.linked or [] %}
        <span class="sl-linked">Linked to <b>{{ p.sku or p.fi }}</b>{% if p.confirmed %} ✓{% endif %}
          {% if src.key not in ('bp', 'es') %}<button type="button" class="sl-btn" onclick="slRemove('{{ p.fi }}', this)">Remove link</button>{% endif %}</span>
      {% endfor %}
      {% if not r.linked %}<button type="button" class="sl-btn" onclick="slOpen({{ loop.index }})">Add link</button>{% endif %}
      <div class="sl-box" hidden>
        <input type="text" placeholder="Search our product: name, SKU or brand" oninput="slSearch({{ loop.index }})">
        <div class="sl-list"></div>
        <div class="sl-btns">
          <button type="button" class="sl-cancel" onclick="slClose({{ loop.index }})">Cancel</button>
          <button type="button" class="sl-save" disabled onclick="slSave({{ loop.index }}, this)">Save</button>
        </div>
      </div>
      <div class="sl-msg"></div>
    </div>
    {% endif %}"""

SHOP_JS = """  // ---- Remove link / Add link on the shop pages (admins only, link_edit.py SECTION 2b) ----
  var slTimer = null;
  function slEl(n) { return document.getElementById("sl-" + n); }
  function slMsg(w, cls, text) { var m = w.querySelector(".sl-msg"); m.className = "sl-msg " + cls; m.textContent = text; }
  async function slPost(body) {
    var r = await fetch("@@SAVE_URL@@", {method: "POST", headers: {"Content-Type": "application/json"},
                                        body: JSON.stringify(body)});
    var d = await r.json().catch(function () { return {ok: false, error: "HTTP " + r.status}; });
    if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
    return d;
  }
  async function slRemove(fi, btn) {                // remove THIS competitor product from our product fi
    var w = btn.closest(".sl-wrap");
    if (!confirm("Remove this link? The next match will not put it back.")) return;
    btn.disabled = true; slMsg(w, "", "Removing ...");
    try {
      await slPost({fi_record_id: fi, shop: w.dataset.shop, action: "remove"});
      btn.closest(".sl-linked").remove();
      slMsg(w, "ok", "Link removed - reload the page to link it again");
      console.log("[SHOP-LINK] removed " + w.dataset.url + " from " + fi);
    } catch (e) { slMsg(w, "err", "Not removed: " + e.message); btn.disabled = false; }
  }
  function slOpen(n) {
    var w = slEl(n), box = w.querySelector(".sl-box"), inp = box.querySelector("input");
    box.hidden = false; slMsg(w, "", "");
    if (!inp.value) inp.value = (w.dataset.q || "").trim().split(/\\s+/).slice(0, 3).join(" ");   // brand + first words
    inp.focus(); slSearch(n);
  }
  function slClose(n) { var w = slEl(n); w.querySelector(".sl-box").hidden = true; }
  function slSearch(n) {                                 // search our products (waits 300 ms after typing)
    clearTimeout(slTimer);
    slTimer = setTimeout(async function () {
      var w = slEl(n), q = w.querySelector(".sl-box input").value.trim(), list = w.querySelector(".sl-list");
      w.querySelector(".sl-save").disabled = true; delete w.dataset.fi;
      if (q.length < 2) { list.innerHTML = ""; return; }
      var r = await fetch("@@SEARCH_URL@@?shop=" + encodeURIComponent(w.dataset.shop) + "&q=" + encodeURIComponent(q));
      var d = await r.json().catch(function () { return {items: []}; });
      list.innerHTML = "";
      (d.items || []).forEach(function (p) {
        var o = document.createElement("div"); o.className = "sl-opt";
        o.textContent = (p.sku ? p.sku + " · " : "") + (p.name || p.fi);
        if (p.has_link) { var s = document.createElement("small"); s.textContent = "  (already has a link - Save replaces it)"; o.appendChild(s); }
        o.onclick = function () {
          list.querySelectorAll(".sl-opt").forEach(function (x) { x.classList.remove("on"); });
          o.classList.add("on"); w.dataset.fi = p.fi; w.querySelector(".sl-save").disabled = false;
        };
        list.appendChild(o);
      });
      if (!(d.items || []).length) list.innerHTML = '<div class="sl-opt"><small>No product found</small></div>';
    }, 300);
  }
  async function slSave(n, btn) {                        // link THIS competitor product to the chosen product
    var w = slEl(n);
    if (!w.dataset.fi) return;
    btn.disabled = true; slMsg(w, "", "Saving ...");
    try {
      var d = await slPost({fi_record_id: w.dataset.fi, shop: w.dataset.shop, action: "save", url: w.dataset.url});
      slClose(n);
      slMsg(w, "ok", "Linked: AED " + (d.price == null ? "-" : d.price) + " | " + (d.stock || "") + " (price + least price updated)");
      var miss = w.closest(".card").querySelector(".missing"); if (miss) miss.remove();
      console.log("[SHOP-LINK] linked " + w.dataset.url + " to " + w.dataset.fi);
    } catch (e) { slMsg(w, "err", "Not saved: " + e.message); btn.disabled = false; }
  }
""".replace("@@SAVE_URL@@", SAVE_URL).replace("@@SEARCH_URL@@", SEARCH_URL)


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
            set_link(cur, fid, shop, link, name, price, stock, user, recalc=False)
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


def set_link(cur, fid, shop, link, name, price, stock, user, recalc=True):
    """Put ONE competitor product on OUR product (competitor_table <shop>_*), CONFIRMED so matching keeps it.
    Also used by new_link.py ("Add new link"). recalc=True: FF sisters + least / suggested price at once (no commit)."""
    sister = shop in ("bp", "es")                     # no suggestion / method / score columns
    extra = "" if sister else f", {shop}_suggestion = NULL, {shop}_method = 'set by ' || %s, {shop}_score = NULL, {shop}_rejected = NULL"
    cur.execute(f"""UPDATE competitor_table SET {shop}_link = %s, {shop}_title = %s, {shop}_price = %s,
                       {shop}_stock = %s, {shop}_confirmed = TRUE{extra}, updated_at = NOW()
                    WHERE fi_record_id = %s""",
                (link, name, price, stock == "In stock") + (() if sister else (user,)) + (fid,))
    if recalc:
        if shop == "ff":
            HOOKS["link_sisters"](cur, fid)
        HOOKS["recalc_one"](cur, fid)


@link_bp.get(SEARCH_URL)
def our_products():
    """Search box of "Add link": our products whose name / SKU / brand has ALL the typed words (max 15).
    has_link = it already has a link on this shop (Save would replace it)."""
    if g.get("role") != "admin":
        return jsonify({"items": []}), 403
    q, shop = (request.args.get("q") or "").strip(), request.args.get("shop")
    words = [w for w in q.split() if len(w) >= 1][:6]
    if shop not in SHOP_LISTS or not words or not HOOKS:
        return jsonify({"items": []})
    conn = HOOKS["connect"]()
    cur = conn.cursor()
    try:
        where = " AND ".join("CONCAT_WS(' ', sku, product_name, brand) ILIKE %s" for _ in words)
        cur.execute(f"""SELECT fi_record_id, sku, product_name, ({shop}_link IS NOT NULL) FROM competitor_table
                        WHERE {where} ORDER BY brand, product_name LIMIT 15""", [f"%{w}%" for w in words])
        items = [{"fi": f, "sku": sk, "name": nm, "has_link": hl} for f, sk, nm, hl in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    log(f"[SHOP-LINK] search {shop} {q!r}: {len(items)} found")
    return jsonify({"items": items})


def linked_products(cur, shop, urls):
    """{competitor link: [our products linked to it]} for the cards of a shop page (dict cursor)."""
    if shop not in SHOP_LISTS or not urls:
        return {}
    cur.execute(f"""SELECT {shop}_link AS link, fi_record_id AS fi, sku, product_name AS name,
                           COALESCE({shop}_confirmed, FALSE) AS confirmed
                    FROM competitor_table WHERE {shop}_link = ANY(%s)""", (list(urls),))
    out = {}
    for r in cur.fetchall():
        out.setdefault(r["link"], []).append(dict(r))
    return out


# ============================================================ SECTION 4: hooks from app.py
HOOKS = {}


def init(connect, create_sql, link_sisters, recalc_one):
    """connect()                 -> new Postgres connection (app.py pg_conn)
    create_sql                  -> app.py CT_CREATE
    link_sisters(cur, fid)      -> app.py link_sisters_for: Branded Perfume + Essenzi follow French Fragrance
    recalc_one(cur, fid)        -> app.py least_for: least + suggested price of ONE product"""
    HOOKS.update(connect=connect, create_sql=create_sql, link_sisters=link_sisters, recalc_one=recalc_one)
    log("[LINK-EDIT] Linked to app.py: admins can edit competitor links on the cards")
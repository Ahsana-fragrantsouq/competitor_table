"""
"Edit" on Item Master cards  (ADMIN ONLY)
=========================================
Every field shown on the card can be changed EXCEPT the product title (product_name) and the SKU.
Saved in OUR database only (no Airtable):
  - item_master: the changed columns + edited_fields (JSONB {column: when}) so a later Airtable load
    (/item-master/load?secret=) never overwrites what an admin changed here (see item_master.WEBSITE_COLS_SQL)
  - competitor_table: brand + barcode are copied there too (same product = same fi_record_id)
Brand, Product type, Type, Category, Size are PICK LISTS (like Airtable single select): type to search, click one.
  Product type / Type / Category / Size: "Add option: xxx" adds a new value -> saved in table item_options on Save.
  Brand: no "Add option" (only brands we already have: brand_sku = Airtable brands + brands used in item_master).
  A list = values already used in item_master + the ones added here (item_options).
Check an edit:  python sql.py "SELECT sku, brand, barcode, edited_fields FROM item_master WHERE sku = 'ASQ1043'"

Where things are (change them here):
  SECTION 1  settings      -> EDIT_COLS (editable columns, label, kind), CT_SYNC (also written to competitor_table)
  SECTION 2  pick lists    -> load_options() (item_master page calls it), table item_options
  SECTION 3  card parts    -> EDIT_CSS, EDIT_BTN (button), EDIT_FORM (the boxes), EDIT_JS (pick list + Save -> POST)
  SECTION 4  save          -> save_edit()   (route POST /item-master/edit is in item_master.py)
  SECTION 5  Update in shopify -> shopify_update(): Shopify Admin API directly (same env as shopify_create.py)
  SECTION 6  Competitor table 'Update price' -> shopify_set_uae_price() (no Airtable)
"""

import json
from decimal import Decimal, InvalidOperation


def log(*a):
    """Print to the Render log (lines start with [ITEM-EDIT])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
# (column in item_master, label, kind) - product_name and sku are NOT here = never editable
#   kind: "text" = free text box · "notes" = big text box · "pick" = pick list + Add option · "pick_only" = no Add option
EDIT_COLS = [
    ("brand",        "Brand",        "pick_only"),
    ("barcode",      "Barcode",      "text"),
    ("perfume_name", "Perfume name", "text"),
    ("product_type", "Product type", "pick"),
    ("type",         "Type",         "pick"),
    ("category",     "Category",     "pick"),
    ("size",         "Size",         "pick"),
    ("top_notes",    "Top notes",    "notes"),
    ("middle_notes", "Middle notes", "notes"),
    ("base_notes",   "Base notes",   "notes"),
    # prices (numbers). Sent to Shopify ONLY when changed in this Save (SECTION 5) - see MARKET_NAMES
    ("uae_price",     "UAE price",     "money"),
    ("asia_price",    "Asia price",    "money"),
    ("kuwait_price",  "Kuwait price",  "money"),
    ("america_price", "America price", "money"),
    ("qatar_price",   "Qatar price",   "money"),
]
EDITABLE = [c for c, _, _ in EDIT_COLS]
PICK_COLS = [c for c, _, k in EDIT_COLS if k.startswith("pick")]
ADD_COLS = [c for c, _, k in EDIT_COLS if k == "pick"]          # these may get new options
MONEY_COLS = [c for c, _, k in EDIT_COLS if k == "money"]
CT_SYNC = ["brand", "barcode", "uae_price", "product_name"]   # also changed in competitor_table (the Competitor table cards: our price)
# price column -> Shopify Markets CATALOG TITLE (same names as Combined_codes syncing_my_price.py MARKET_NAMES).
# The price is sent in that catalog's price list currency, AS IS (no conversion - e.g. Qatar = QAR).
# UAE price is also the product's normal price (variant price), like the Airtable price sync did.
MARKET_NAMES = {"uae_price": "United Arab Emirates", "asia_price": "Asia Market with 55 rate",
                "america_price": "America catlog", "kuwait_price": "Kuwait", "qatar_price": "Qatar"}
SAVE_URL = "/item-master/edit"
# "Update in shopify" (ticked by default): after Save ONLY these go to Shopify (Admin API directly, Render env
# SHOPIFY_SHOP + SHOPIFY_API_TOKEN, same as "Create in website" / shopify_create.py), and only when CHANGED in that Save:
#   Barcode      -> variant barcode
#   Product name -> product TITLE. The product name is REBUILT when brand / perfume name / size / category / type /
#                   product type changed (NAME_COLS, same rule as Create a new item: item_create.product_name)
#   Prices       -> ONLY when SEND_PRICES = True (now OFF, user's choice): Shopify Markets price lists (MARKET_NAMES);
#                   UAE price also = variant price. OFF -> prices are saved in our database only.
# Everything else (notes, brand, type, category, size ...) is saved in OUR database only.
SEND_PRICES = False                       # True = Edit also sends changed prices to Shopify (code below is ready)
NAME_COLS = ["brand", "perfume_name", "size", "category", "type", "product_type"]   # change -> new product name
KEEP_COLS = EDITABLE + ["product_name"]   # item_master.WEBSITE_COLS_SQL: Airtable load keeps these once edited here
EDIT_SQL = """ALTER TABLE item_master ADD COLUMN IF NOT EXISTS edited_fields JSONB;
CREATE TABLE IF NOT EXISTS item_options (           -- options added with "Add option:" (item_edit.py)
    field      TEXT NOT NULL,                       -- product_type / type / category / size
    value      TEXT NOT NULL,
    added_by   TEXT,
    added_at   TIMESTAMP DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS item_options_uq ON item_options (field, LOWER(value));"""   # run by item_master.IM_CREATE


# ============================================================ SECTION 2: pick lists
def load_options(cur):
    """{column: [values A-Z]} for every pick list. Works with a normal or a dict cursor."""
    def first(row):
        return list(row.values())[0] if isinstance(row, dict) else row[0]
    out = {}
    for col in PICK_COLS:
        sql = f"SELECT DISTINCT TRIM({col}) AS v FROM item_master WHERE TRIM(COALESCE({col}, '')) <> ''"
        if col == "brand":
            cur.execute("SELECT to_regclass('brand_sku') IS NOT NULL AS ok")
            if first(cur.fetchone()):                      # Airtable brands list (brand_sku.py)
                sql += " UNION SELECT TRIM(brand) FROM brand_sku WHERE TRIM(COALESCE(brand, '')) <> ''"
        else:
            sql += " UNION SELECT value FROM item_options WHERE field = %s"
        cur.execute(sql, () if col == "brand" else (col,))
        seen, vals = set(), []
        for v in sorted((first(r) for r in cur.fetchall()), key=str.lower):
            if v.lower() not in seen:                      # same value in other capitals -> once
                seen.add(v.lower())
                vals.append(v)
        out[col] = vals
    log("[ITEM-EDIT] Pick lists: " + ", ".join(f"{c} {len(v)}" for c, v in out.items()))
    return out


# ============================================================ SECTION 3: card parts (item_master.py puts them in)
EDIT_CSS = """ /* Edit (admins only, item_edit.py) */
 .edit-btn{background:transparent;color:#f2d675;border:1px solid #f2d675;border-radius:10px;padding:7px 14px;
           font-weight:600;font-size:14px;cursor:pointer}
 .btnrow .edit-btn{order:0}
 .editbox{border-top:1px solid var(--line);margin-top:12px;padding-top:12px}
 .editgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px 14px}
 .editgrid label{display:block;color:var(--muted);font-size:13px}
 .editgrid input,.editgrid textarea,.pick-btn{width:100%;box-sizing:border-box;margin-top:3px;background:var(--bg);color:var(--text);
           border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:15px;font-family:inherit}
 .editgrid textarea{min-height:64px;resize:vertical}
 /* pick list (like Airtable single select) */
 .pick{position:relative}
 .pick-btn{display:flex;justify-content:space-between;align-items:center;gap:8px;cursor:pointer;text-align:left;min-height:38px}
 .pick-btn .ph{color:var(--grey)} .pick-btn::after{content:"\\25BE";color:var(--muted)}
 .pick-pop{position:absolute;left:0;right:0;top:100%;margin-top:4px;z-index:50;background:#fff;color:#1c1b19;border-radius:10px;
           box-shadow:0 8px 28px rgba(0,0,0,.45);overflow:hidden}
 .pick-pop input{margin:0;border:0;border-bottom:1px solid #ddd;border-radius:0;background:#fff;color:#1c1b19}
 .pick-list{max-height:240px;overflow:auto;padding:4px 0}
 .pick-opt{padding:6px 10px;cursor:pointer;font-size:14px}
 .pick-opt span{display:inline-block;background:#e8eefc;border-radius:12px;padding:2px 10px}
 .pick-opt:hover,.pick-opt.on{background:#eef2fb}
 .pick-opt.clear span{background:none;color:#888;padding:2px 0}
 .pick-opt.add span{background:#f6c344}
 .pick-none{padding:8px 10px;color:#888;font-size:14px}
 .editbtns{display:flex;gap:8px;justify-content:flex-end;align-items:center;margin-top:10px;flex-wrap:wrap}
 .editbtns .save{background:#f2d675;color:#1c1b19;border:0;border-radius:10px;padding:8px 18px;font-weight:600;cursor:pointer}
 .editbtns .cancel{background:transparent;color:var(--muted);border:1px solid var(--line);border-radius:10px;padding:8px 14px;cursor:pointer}
 .editbtns .save:disabled{opacity:.6;cursor:wait}
 .editmsg{font-size:13px;margin-right:auto} .editmsg.err{color:#e0806c}
 .toshop{flex-basis:100%;display:flex;justify-content:flex-end;align-items:center;gap:7px;font-size:14px;color:var(--text);cursor:pointer}
 .toshop input{width:17px;height:17px;accent-color:#f2d675;margin:0}
 @media (max-width:640px){ .editgrid{grid-template-columns:1fr} }
"""

# r = product row; button goes in the card's button row
EDIT_BTN = """{% if admin %}
      <button type="button" class="edit-btn" onclick="editOpen('{{ r.fi_record_id }}')">Edit</button>
      {% endif %}"""


def _box(col, label, kind):
    """One edit box (Jinja). pick = hidden value + button that opens the pick list."""
    val = "{{ r." + col + " or '' }}"
    start = ' style="grid-column-start:1"' if col in ("top_notes", "uae_price") else ""
    if kind == "money":                                  # price: number box, empty when no price
        inner = (f'<input name="{col}" type="number" step="0.01" min="0" inputmode="decimal" '
                 f'value="{{{{ r.{col} if r.{col} is not none else \'\' }}}}">')
    elif kind == "notes":
        inner = f'<textarea name="{col}">{val}</textarea>'
    elif kind == "text":
        inner = f'<input name="{col}" value="{val}">'
    else:
        inner = (f'<div class="pick" data-col="{col}" data-add="{1 if kind == "pick" else 0}">'
                 f'<input type="hidden" name="{col}" value="{val}">'
                 f'<button type="button" class="pick-btn" onclick="pickOpen(this)"></button></div>')
    return f'      <div{start}><label>{label}</label>\n        {inner}</div>\n'


# the boxes (hidden until Edit is clicked); goes at the bottom of the card
EDIT_FORM = """{% if admin %}
  <form class="editbox" id="edit-{{ r.fi_record_id }}" hidden onsubmit="editSave('{{ r.fi_record_id }}', this); return false;">
    <div class="editgrid">
""" + "".join(_box(c, l, k) for c, l, k in EDIT_COLS) + """    </div>
    <div class="editbtns">
      {# ticked = after Save also update the product in Shopify (Shopify API, SECTION 5); no name -> not a field #}
      <label class="toshop"><input type="checkbox" class="toshop-box" checked> Update in shopify</label>
      <span class="editmsg" id="editmsg-{{ r.fi_record_id }}">SKU is not editable. Product title = Brand + Perfume name + Size + Type + Category + Product type.</span>
      <button type="button" class="cancel" onclick="editClose('{{ r.fi_record_id }}')">Cancel</button>
      <button type="submit" class="save">Save</button>
    </div>
  </form>
  {% endif %}"""

# the pick lists, ONCE per page (not per card): ITEM_OPTS = {column: [values]}
EDIT_OPTS = """{% if admin %}<script>var ITEM_OPTS = {{ edit_opts|tojson }};</script>{% endif %}"""

EDIT_JS = """  // ---- Edit (admins only, item_edit.py) ----
  function pickShow(p) {                              // button text = the chosen value (or "Select ...")
    var v = p.querySelector("input[type=hidden]").value, b = p.querySelector(".pick-btn");
    b.textContent = "";
    var s = document.createElement("span"); s.textContent = v || "Select ..."; if (!v) s.className = "ph";
    b.appendChild(s);
  }
  function pickClose() {
    document.querySelectorAll(".pick-pop").forEach(function (x) { x.remove(); });
  }
  function pickOpen(btn) {                            // pop-up: search box + options (+ "Add option: xxx")
    var p = btn.parentNode, col = p.dataset.col, canAdd = p.dataset.add === "1";
    var had = p.querySelector(".pick-pop"); pickClose(); if (had) return;      // 2nd click = close
    var pop = document.createElement("div"); pop.className = "pick-pop";
    var q = document.createElement("input"); q.placeholder = canAdd ? "Find or add an option" : "Find a brand";
    var list = document.createElement("div"); list.className = "pick-list";
    pop.appendChild(q); pop.appendChild(list); p.appendChild(pop);
    function choose(v, isNew) {
      p.querySelector("input[type=hidden]").value = v; pickShow(p); pickClose();
      if (isNew && ITEM_OPTS[col].indexOf(v) < 0) { ITEM_OPTS[col].push(v); ITEM_OPTS[col].sort(); }  // in the list now
      console.log("[ITEM-EDIT] " + col + " = " + JSON.stringify(v) + (isNew ? " (new option, saved on Save)" : ""));
    }
    function row(text, cls, v, isNew) {
      var d = document.createElement("div"); d.className = "pick-opt " + cls;
      if (cls === "add") d.appendChild(document.createTextNode("Add option: "));
      var s = document.createElement("span"); s.textContent = text; d.appendChild(s);
      d.onmousedown = function (e) { e.preventDefault(); choose(v, isNew); };
      list.appendChild(d);
    }
    function draw() {
      var t = q.value.trim(), lt = t.toLowerCase(), cur = p.querySelector("input[type=hidden]").value;
      list.textContent = "";
      if (!t) row("(empty)", "clear", "", false);
      var opts = (ITEM_OPTS[col] || []).filter(function (o) { return o.toLowerCase().indexOf(lt) >= 0; });
      opts.slice(0, 300).forEach(function (o) { row(o, o === cur ? "on" : "", o, false); });
      var exact = (ITEM_OPTS[col] || []).some(function (o) { return o.toLowerCase() === lt; });
      if (t && canAdd && !exact) row(t, "add", t, true);
      if (!list.children.length) {
        var n = document.createElement("div"); n.className = "pick-none";
        n.textContent = canAdd ? "No options" : "No brand found (brands cannot be added here)"; list.appendChild(n);
      }
    }
    q.oninput = draw;
    q.onkeydown = function (e) {
      if (e.key === "Escape") { pickClose(); btn.focus(); }
      if (e.key === "Enter") { e.preventDefault(); var f = list.querySelector(".pick-opt"); if (f) f.onmousedown(e); }
    };
    q.onblur = function () { setTimeout(pickClose, 150); };
    draw(); q.focus();
  }
  function editOpen(fi) {
    var f = document.getElementById("edit-" + fi);
    f.dataset.orig = JSON.stringify(Object.fromEntries(new FormData(f)));   // to put back on Cancel
    f.querySelectorAll(".pick").forEach(pickShow);
    f.hidden = false;
    console.log("[ITEM-EDIT] " + fi + ": edit opened");
  }
  function editClose(fi) {
    var f = document.getElementById("edit-" + fi);
    var orig = JSON.parse(f.dataset.orig || "{}");
    Object.keys(orig).forEach(function (k) { f.elements[k].value = orig[k]; });    // undo changes
    f.querySelectorAll(".pick").forEach(pickShow); pickClose();
    f.hidden = true;
  }
  async function editSave(fi, f) {
    var msg = document.getElementById("editmsg-" + fi), btn = f.querySelector(".save");
    btn.disabled = true; btn.textContent = "Saving ..."; msg.className = "editmsg"; msg.textContent = "";
    try {
      var r = await fetch("@@SAVE_URL@@", {method: "POST", headers: {"Content-Type": "application/json"},
                          body: JSON.stringify({fi_record_id: fi, fields: Object.fromEntries(new FormData(f)),
                                                to_shopify: f.querySelector(".toshop-box").checked})});
      var d = await r.json().catch(function () { return {ok: false, error: "HTTP " + r.status}; });
      if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
      console.log("[ITEM-EDIT] " + fi + ": saved " + (d.changed || []).join(", ") +
                  ((d.new_options || []).length ? " | new options: " + d.new_options.join(", ") : "") +
                  (d.shopify ? " | shopify: " + JSON.stringify(d.shopify) : ""));
      if (d.shopify && !d.shopify.ok) {                   // saved here, but Shopify did not take it -> say so, no reload
        msg.className = "editmsg err";
        msg.textContent = "Saved in our database, but NOT in Shopify: " + d.shopify.error;
        btn.disabled = false; btn.textContent = "Save";
        return;
      }
      location.reload();                                  // card shows the new values
    } catch (e) {
      msg.className = "editmsg err"; msg.textContent = "Not saved: " + e.message;
      console.log("[ITEM-EDIT] " + fi + ": ERROR " + e.message);
      btn.disabled = false; btn.textContent = "Save";
    }
  }
""".replace("@@SAVE_URL@@", SAVE_URL)


# ============================================================ SECTION 4: save
def save_edit(conn, fid, fields, user):
    """Save the admin's changes. Returns (changed column list, new options list, error text or None).
    Only EDITABLE columns are used (product_name / sku / anything else sent is ignored).
    Brand must be one of the brands we have; a new Product type / Type / Category / Size goes into item_options."""
    new = {}
    for col in EDITABLE:
        if col in fields:
            v = str(fields.get(col) or "").strip()
            if col in MONEY_COLS and v:                   # price -> number (2 decimals), refuse nonsense
                try:
                    v = Decimal(v.replace(",", "")).quantize(Decimal("0.01"))
                except InvalidOperation:
                    return [], [], f"{col.replace('_', ' ')}: '{v}' is not a number"
                if v < 0 or v >= 1_000_000:
                    return [], [], f"{col.replace('_', ' ')}: {v} is not a real price"
            new[col] = None if v in ("", None) else v     # empty box -> empty in the database (price 0 stays 0)
    if not new:
        return [], [], "nothing to save"
    cur = conn.cursor()
    cur.execute(f"SELECT {', '.join(EDITABLE)} FROM item_master WHERE fi_record_id = %s FOR UPDATE", (fid,))
    row = cur.fetchone()
    if row is None:
        return [], [], "product not in Item Master"
    old = dict(zip(EDITABLE, row))
    changed = [c for c, v in new.items() if (old.get(c) if old.get(c) not in ("",) else None) != v]
    # product name ALWAYS rebuilt in today's format (= Shopify title): when its parts changed OR the saved name
    # is in an old format (e.g. '100 ml men EDT') -> a plain Save also fixes old names
    import item_create                                # (loaded here: item_create imports item_master)
    cur.execute(f"SELECT product_name, {', '.join(NAME_COLS)} FROM item_master WHERE fi_record_id = %s", (fid,))
    cur_row = cur.fetchone()
    parts = {c: (new[c] if c in new else cur_row[1 + i]) for i, c in enumerate(NAME_COLS)}
    name = item_create.product_name({c: str(v).strip() for c, v in parts.items() if v}) or None
    if name and name != cur_row[0]:
        new["product_name"] = name
        changed.append("product_name")
        log(f"[ITEM-EDIT] {fid}: product name {cur_row[0]!r} -> {name!r}")
    if not changed:
        log(f"[ITEM-EDIT] {fid}: nothing changed")
        return [], [], None
    opts = load_options(cur)
    if "brand" in changed and new["brand"] and new["brand"].lower() not in {b.lower() for b in opts["brand"]}:
        log(f"[ITEM-EDIT] {fid}: refused - brand {new['brand']!r} is not in the brand list")
        return [], [], f"brand '{new['brand']}' is not in the brand list"
    added = []
    for c in changed:                                     # new option -> saved in the list (item_options)
        if c in ADD_COLS and new[c] and new[c].lower() not in {o.lower() for o in opts[c]}:
            cur.execute("INSERT INTO item_options (field, value, added_by) VALUES (%s, %s, %s) "
                        "ON CONFLICT DO NOTHING", (c, new[c], user))
            added.append(f"{c}: {new[c]}")
    if added:
        log(f"[ITEM-EDIT] New options saved in item_options: {', '.join(added)}")
    # changed columns + remember them in edited_fields -> an Airtable load keeps our values
    stamp = json.dumps({c: user or "admin" for c in changed})
    cur.execute(f"UPDATE item_master SET {', '.join(c + ' = %s' for c in changed)}, "
                f"edited_fields = COALESCE(edited_fields, '{{}}'::jsonb) || %s::jsonb WHERE fi_record_id = %s",
                [new[c] for c in changed] + [stamp, fid])
    ct = [c for c in changed if c in CT_SYNC]
    if ct:                                                # same product in the Competitor table
        cur.execute("SELECT to_regclass('competitor_table') IS NOT NULL")
        if cur.fetchone()[0]:
            web = ", uae_price_website = TRUE" if "uae_price" in ct else ""   # app.py: Airtable load keeps it
            if "product_name" in ct:                      # card's code line "SKU/Product name" too
                web += (", french_inventory_code = CASE WHEN COALESCE(sku, '') <> '' THEN sku || '/' || %s "
                        "ELSE %s END")
            cur.execute(f"UPDATE competitor_table SET {', '.join(c + ' = %s' for c in ct)}{web} WHERE fi_record_id = %s",
                        [new[c] for c in ct] + ([new["product_name"]] * 2 if "product_name" in ct else []) + [fid])
            log(f"[ITEM-EDIT] {fid}: competitor_table {', '.join(ct)} updated ({cur.rowcount} row)")
    conn.commit()
    log(f"[ITEM-EDIT] {user} changed {fid}: " + "; ".join(f"{c}: {old.get(c)!r} -> {new[c]!r}" for c in changed))
    return changed, added, None


# ============================================================ SECTION 5: Update in shopify (after Save, box ticked)
CATALOGS = """{ catalogs(first: 50, type: MARKET) { nodes { title status priceList { id currency } } } }"""
PRICE_ADD = """mutation($pl: ID!, $prices: [PriceListPriceInput!]!) {
  priceListFixedPricesAdd(priceListId: $pl, prices: $prices) { userErrors { field message } } }"""


def _price_lists(sc):
    """{catalog title: {"id", "currency"}} of the ACTIVE market catalogs (same as Combined_codes)."""
    res = sc.shopify_graphql(CATALOGS)
    out = {}
    for c in ((res.get("data") or {}).get("catalogs") or {}).get("nodes") or []:
        if c.get("status") == "ACTIVE" and c.get("priceList"):
            out[c["title"]] = c["priceList"]
    log("[ITEM-SHOPIFY] price lists: " + ", ".join(f"{t} ({v.get('currency')})" for t, v in out.items()))
    return out


def _send_prices(sc, fid, vid, prices):
    """{price column: price} -> Shopify Markets price lists (MARKET_NAMES) of variant vid. Returns error list."""
    bad = []
    if not prices:
        return bad
    lists = _price_lists(sc)
    for col, price in prices.items():
        pl = lists.get(MARKET_NAMES[col])
        if not pl:
            bad.append(f"{col.replace('_', ' ')}: catalog '{MARKET_NAMES[col]}' not found in Shopify")
            continue
        res = sc.shopify_graphql(PRICE_ADD, {"pl": pl["id"], "prices": [{
            "variantId": f"gid://shopify/ProductVariant/{vid}",
            "price": {"amount": str(price), "currencyCode": pl["currency"]}}]})
        errs = ((res.get("data") or {}).get("priceListFixedPricesAdd") or {}).get("userErrors") or res.get("errors")
        if errs:
            bad.append(f"{col.replace('_', ' ')}: {errs}")
        else:
            log(f"[ITEM-SHOPIFY] {fid}: {MARKET_NAMES[col]} price {price} {pl['currency']}")
    if bad:
        log(f"[ITEM-SHOPIFY] {fid}: prices NOT set: {bad}")
    return bad


FIND_BY_SKU = """query($q: String!) { productVariants(first: 10, query: $q) { edges { node {
  id legacyResourceId sku product { id legacyResourceId tags } } } } }"""


def _find_in_shopify(sc, sku, shopify_id):
    """-> (product id, variant id, product tags) of OUR product in Shopify, or None.
    Uses item_master.shopify_id when we have it, else the SKU (products made in Shopify / by Airtable)."""
    if shopify_id:
        r = sc.shopify_rest("GET", f"products/{shopify_id}.json?fields=id,tags,variants")
        if r.ok:
            p = r.json().get("product") or {}
            v = next((v for v in p.get("variants") or [] if (v.get("sku") or "").strip().upper() == sku),
                     (p.get("variants") or [None])[0])
            if v:
                return p["id"], v["id"], p.get("tags") or ""
        log(f"[ITEM-SHOPIFY] shopify_id {shopify_id}: HTTP {r.status_code} -> searching by SKU {sku}")
    res = sc.shopify_graphql(FIND_BY_SKU, {"q": f"sku:'{sku}'"})
    edges = ((res.get("data") or {}).get("productVariants") or {}).get("edges") or []
    for e in edges:                                          # search is loose -> exact SKU only
        n = e.get("node") or {}
        if (n.get("sku") or "").strip().upper() == sku:
            prod = n.get("product") or {}
            return int(prod["legacyResourceId"]), int(n["legacyResourceId"]), ", ".join(prod.get("tags") or [])
    return None


def shopify_update(conn, fid, changed=()):
    """After Save, box ticked: ONLY the changed barcode, product name (= Shopify title) and prices go to Shopify.
    changed = columns changed in this Save (save_edit). Returns {"ok": True, "answer": ...} or {"ok": False, "error": ...}."""
    send = [c for c in changed if c in ("barcode", "product_name") or (SEND_PRICES and c in MONEY_COLS)]
    if not send:
        log(f"[ITEM-SHOPIFY] {fid}: no barcode / product name changed -> nothing sent to Shopify")
        return {"ok": True, "answer": "nothing for Shopify changed"}
    import shopify_create as sc                              # Shopify helpers + env (loaded here: no import loop)
    if not (sc.SHOP and sc.TOKEN):
        log("[ITEM-SHOPIFY] SHOPIFY_SHOP / SHOPIFY_API_TOKEN not set in Render -> not sent")
        return {"ok": False, "error": "SHOPIFY_SHOP / SHOPIFY_API_TOKEN are not set in Render -> Environment"}
    cur = conn.cursor()
    cur.execute(f"SELECT sku, barcode, product_name, shopify_id, {', '.join(MONEY_COLS)} "
                f"FROM item_master WHERE fi_record_id = %s", (fid,))
    row = cur.fetchone()
    conn.commit()                                            # no open transaction while Shopify answers
    if not row or not (row[0] or "").strip():
        return {"ok": False, "error": "product has no SKU"}
    sku, barcode, title, shopify_id = row[:4]
    sku = sku.strip().upper()
    prices = {c: row[4 + i] for i, c in enumerate(MONEY_COLS)
              if c in send and row[4 + i] is not None}       # empty price = nothing to send
    txt = lambda v: str(v).strip() if v not in (None, "") else ""
    try:
        found = _find_in_shopify(sc, sku, txt(shopify_id))
        if not found:
            log(f"[ITEM-SHOPIFY] {fid}: SKU {sku} not found in Shopify")
            return {"ok": False, "error": f"SKU {sku} not found in Shopify"}
        pid, vid, _ = found
        log(f"[ITEM-SHOPIFY] {fid}: SKU {sku} = Shopify product {pid}, variant {vid} | sending {send}")

        if "product_name" in send and txt(title):            # new product name -> Shopify title
            r = sc.shopify_rest("PUT", f"products/{pid}.json", {"product": {"id": pid, "title": txt(title)}})
            if not r.ok:
                return {"ok": False, "error": f"title not changed ({r.status_code}): {r.text[:150]}"}
            log(f"[ITEM-SHOPIFY] {fid}: title = {txt(title)!r}")

        variant = {"id": vid}
        if "barcode" in send:
            variant["barcode"] = txt(barcode)
        if "uae_price" in prices:                            # UAE price = the normal (default) price too
            variant["price"] = str(prices["uae_price"])
        if len(variant) > 1:
            r = sc.shopify_rest("PUT", f"variants/{vid}.json", {"variant": variant})
            if not r.ok:
                return {"ok": False, "error": f"barcode / price not changed ({r.status_code}): {r.text[:150]}"}
            log(f"[ITEM-SHOPIFY] {fid}: variant {variant}")

        bad = _send_prices(sc, fid, vid, prices)              # changed prices -> market price lists
        if bad:
            return {"ok": False, "error": f"prices not set: {'; '.join(bad)[:200]}"}
    except Exception as ex:
        log(f"[ITEM-SHOPIFY] {fid}: ERROR {ex}")
        return {"ok": False, "error": f"Shopify error: {str(ex)[:150]}"}
    log(f"[ITEM-SHOPIFY] {fid}: Shopify product {pid} updated ({', '.join(send)})")
    return {"ok": True, "answer": f"Shopify product {pid} updated: {', '.join(send)}"}


# ============================================================ SECTION 6: "Update price" button (Competitor table)
def shopify_set_uae_price(conn, fid, price):
    """'Update price' on /competitors: UAE price of ONE product straight into Shopify (NO Airtable):
    variant price + 'United Arab Emirates' price list (same as an Item Master UAE price edit).
    Product found by item_master.shopify_id, else its exact SKU (item_master, else competitor_table).
    Returns {"ok": True, "answer": ...} or {"ok": False, "error": ...}."""
    import shopify_create as sc
    if not (sc.SHOP and sc.TOKEN):
        log("[PRICE-SHOPIFY] SHOPIFY_SHOP / SHOPIFY_API_TOKEN not set in Render -> not sent")
        return {"ok": False, "error": "SHOPIFY_SHOP / SHOPIFY_API_TOKEN are not set in Render -> Environment"}
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(NULLIF(TRIM(im.sku), ''), ct.sku), im.shopify_id FROM competitor_table ct "
                "LEFT JOIN item_master im ON im.fi_record_id = ct.fi_record_id WHERE ct.fi_record_id = %s", (fid,))
    row = cur.fetchone()
    conn.commit()                                            # no open transaction while Shopify answers
    sku = ((row or [None])[0] or "").strip().upper()
    if not sku:
        return {"ok": False, "error": "product has no SKU"}
    try:
        found = _find_in_shopify(sc, sku, ((row[1] or "") if row else "").strip())
        if not found:
            log(f"[PRICE-SHOPIFY] {fid}: SKU {sku} not found in Shopify")
            return {"ok": False, "error": f"SKU {sku} not found in Shopify"}
        pid, vid, _ = found
        r = sc.shopify_rest("PUT", f"variants/{vid}.json", {"variant": {"id": vid, "price": f"{price:.2f}"}})
        if not r.ok:
            return {"ok": False, "error": f"Shopify price not changed ({r.status_code}): {r.text[:150]}"}
        log(f"[PRICE-SHOPIFY] {fid}: SKU {sku} variant {vid} price = {price:.2f}")
        bad = _send_prices(sc, fid, vid, {"uae_price": f"{price:.2f}"})
        if bad:
            return {"ok": False, "error": f"normal price changed, but UAE price list not: {'; '.join(bad)[:150]}"}
    except Exception as ex:
        log(f"[PRICE-SHOPIFY] {fid}: ERROR {ex}")
        return {"ok": False, "error": f"Shopify error: {str(ex)[:150]}"}
    return {"ok": True, "answer": f"Shopify product {pid} price {price:.2f}"}


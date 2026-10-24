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
"""

import json


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
]
EDITABLE = [c for c, _, _ in EDIT_COLS]
PICK_COLS = [c for c, _, k in EDIT_COLS if k.startswith("pick")]
ADD_COLS = [c for c, _, k in EDIT_COLS if k == "pick"]          # these may get new options
CT_SYNC = ["brand", "barcode"]            # also changed in competitor_table (the Competitor table cards)
SAVE_URL = "/item-master/edit"
# "Update in shopify" (ticked by default): after Save the product is ALSO changed in Shopify, DIRECTLY with the
# Shopify Admin API (Render env SHOPIFY_SHOP + SHOPIFY_API_TOKEN, same as "Create in website" / shopify_create.py).
# (The Combined_codes webhook was used before, but it only changes SKU / Title / Barcode / Size -> brand, type,
#  category ... never reached Shopify.)  Same field places as "Create in website":
#   Brand    -> Vendor + metafield custom.brands        Barcode  -> variant barcode
#   Type     -> Shopify "Type" (product_type, e.g. EDT)  Size     -> metafield custom.size
#   Category -> tag (old category tag swapped) + variant google_shopping.gender (male / female / unisex)
# Perfume name / Product type / notes have NO Shopify field (notes are inside the description) -> not sent.
# Title, prices, stock, description are NOT changed.
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
    start = ' style="grid-column-start:1"' if col == "top_notes" else ""
    if kind == "notes":
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
      <span class="editmsg" id="editmsg-{{ r.fi_record_id }}">Product title and SKU are not editable.</span>
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
            new[col] = v or None                          # empty box -> empty in the database
    if not new:
        return [], [], "nothing to save"
    cur = conn.cursor()
    cur.execute(f"SELECT {', '.join(EDITABLE)} FROM item_master WHERE fi_record_id = %s FOR UPDATE", (fid,))
    row = cur.fetchone()
    if row is None:
        return [], [], "product not in Item Master"
    old = dict(zip(EDITABLE, row))
    changed = [c for c, v in new.items() if (old.get(c) or None) != v]
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
            cur.execute(f"UPDATE competitor_table SET {', '.join(c + ' = %s' for c in ct)} WHERE fi_record_id = %s",
                        [new[c] for c in ct] + [fid])
            log(f"[ITEM-EDIT] {fid}: competitor_table {', '.join(ct)} updated ({cur.rowcount} row)")
    conn.commit()
    log(f"[ITEM-EDIT] {user} changed {fid}: " + "; ".join(f"{c}: {old.get(c)!r} -> {new[c]!r}" for c in changed))
    return changed, added, None


# ============================================================ SECTION 5: Update in shopify (after Save, box ticked)
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


def shopify_update(conn, fid):
    """Change THIS product in Shopify from item_master (brand, barcode, type, size, category - see SECTION 1).
    Returns {"ok": True, "answer": ...} or {"ok": False, "error": ...}. Nothing is retried automatically."""
    import shopify_create as sc                              # Shopify helpers + env (loaded here: no import loop)
    if not (sc.SHOP and sc.TOKEN):
        log("[ITEM-SHOPIFY] SHOPIFY_SHOP / SHOPIFY_API_TOKEN not set in Render -> not sent")
        return {"ok": False, "error": "SHOPIFY_SHOP / SHOPIFY_API_TOKEN are not set in Render -> Environment"}
    cur = conn.cursor()
    cur.execute("SELECT sku, brand, barcode, type, size, category, shopify_id FROM item_master "
                "WHERE fi_record_id = %s", (fid,))
    row = cur.fetchone()
    if not row or not (row[0] or "").strip():
        return {"ok": False, "error": "product has no SKU"}
    sku, brand, barcode, ptype, size, category, shopify_id = row
    sku = sku.strip().upper()
    txt = lambda v: str(v).strip() if v not in (None, "") else ""
    try:
        found = _find_in_shopify(sc, sku, txt(shopify_id))
        if not found:
            log(f"[ITEM-SHOPIFY] {fid}: SKU {sku} not found in Shopify")
            return {"ok": False, "error": f"SKU {sku} not found in Shopify"}
        pid, vid, tags = found
        log(f"[ITEM-SHOPIFY] {fid}: SKU {sku} = Shopify product {pid}, variant {vid}")

        # tags: take out every tag that is a category (Male / Female / Unisex ... = our category list), add the new one
        cats = {c.lower() for c in load_options(cur).get("category", [])}
        new_tags = [t.strip() for t in tags.split(",") if t.strip() and t.strip().lower() not in cats]
        if txt(category):
            new_tags.append(txt(category))
        product = {"id": pid, "tags": ", ".join(new_tags)}
        if txt(brand):
            product["vendor"] = txt(brand)
        if txt(ptype):
            product["product_type"] = txt(ptype)
        r = sc.shopify_rest("PUT", f"products/{pid}.json", {"product": product})
        if not r.ok:
            return {"ok": False, "error": f"product not changed ({r.status_code}): {r.text[:150]}"}
        log(f"[ITEM-SHOPIFY] {fid}: product {product}")

        r = sc.shopify_rest("PUT", f"variants/{vid}.json", {"variant": {"id": vid, "barcode": txt(barcode)}})
        if not r.ok:
            return {"ok": False, "error": f"barcode not changed ({r.status_code}): {r.text[:150]}"}
        log(f"[ITEM-SHOPIFY] {fid}: variant barcode {txt(barcode)!r}")

        gender = {"men": "male", "man": "male", "women": "female", "woman": "female"}.get(
            txt(category).lower(), txt(category).lower())   # our "Men" / "Women" -> Google's male / female
        metas = [(f"gid://shopify/Product/{pid}", "custom", "brands", txt(brand)),
                 (f"gid://shopify/Product/{pid}", "custom", "size", txt(size)),
                 (f"gid://shopify/ProductVariant/{vid}", "google_shopping", "gender",
                  gender if gender in ("male", "female", "unisex") else "unisex")]
        bad = []
        for owner, ns, key, value in metas:
            if not value:                                    # empty -> leave Shopify's value
                continue
            ok, errs = sc.set_metafield(owner, ns, key, value)
            if not ok:
                bad.append(f"{ns}.{key}: {errs}")
        if bad:
            log(f"[ITEM-SHOPIFY] {fid}: metafields NOT set: {bad}")
            return {"ok": False, "error": f"metafields not set: {'; '.join(bad)[:150]}"}
    except Exception as ex:
        log(f"[ITEM-SHOPIFY] {fid}: ERROR {ex}")
        return {"ok": False, "error": f"Shopify error: {str(ex)[:150]}"}
    log(f"[ITEM-SHOPIFY] {fid}: Shopify product {pid} updated")
    return {"ok": True, "answer": f"Shopify product {pid} updated"}

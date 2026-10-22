"""
"Edit" on Item Master cards  (ADMIN ONLY)
=========================================
Every field shown on the card can be changed EXCEPT the product title (product_name) and the SKU.
Saved in OUR database only (no Airtable):
  - item_master: the changed columns + edited_fields (JSONB {column: when}) so a later Airtable load
    (/item-master/load?secret=) never overwrites what an admin changed here (see item_master.WEBSITE_COLS_SQL)
  - competitor_table: brand + barcode are copied there too (same product = same fi_record_id)
Check an edit:  python sql.py "SELECT sku, brand, barcode, edited_fields FROM item_master WHERE sku = 'ASQ1043'"

Where things are (change them here):
  SECTION 1  settings      -> EDIT_COLS (editable columns + labels), CT_SYNC (also written to competitor_table)
  SECTION 2  card parts    -> EDIT_CSS, EDIT_BTN (button), EDIT_FORM (the boxes), EDIT_JS (Save -> POST)
  SECTION 3  save          -> save_edit()   (route POST /item-master/edit is in item_master.py)
"""

import json


def log(*a):
    """Print to the Render log (lines start with [ITEM-EDIT])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
# (column in item_master, label, big box?) - product_name and sku are NOT here = never editable
EDIT_COLS = [
    ("brand",        "Brand",        False),
    ("barcode",      "Barcode",      False),
    ("perfume_name", "Perfume name", False),
    ("product_type", "Product type", False),
    ("type",         "Type",         False),
    ("category",     "Category",     False),
    ("size",         "Size",         False),
    ("top_notes",    "Top notes",    True),
    ("middle_notes", "Middle notes", True),
    ("base_notes",   "Base notes",   True),
]
EDITABLE = [c for c, _, _ in EDIT_COLS]
CT_SYNC = ["brand", "barcode"]            # also changed in competitor_table (the Competitor table cards)
SAVE_URL = "/item-master/edit"
EDIT_SQL = "ALTER TABLE item_master ADD COLUMN IF NOT EXISTS edited_fields JSONB;"   # run by item_master.IM_CREATE


# ============================================================ SECTION 2: card parts (item_master.py puts them in)
EDIT_CSS = """ /* Edit (admins only, item_edit.py) */
 .edit-btn{background:transparent;color:#f2d675;border:1px solid #f2d675;border-radius:10px;padding:7px 14px;
           font-weight:600;font-size:14px;cursor:pointer}
 .btnrow .edit-btn{order:0}
 .editbox{border-top:1px solid var(--line);margin-top:12px;padding-top:12px}
 .editgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px 14px}
 .editgrid label{display:block;color:var(--muted);font-size:13px}
 .editgrid input,.editgrid textarea{width:100%;box-sizing:border-box;margin-top:3px;background:var(--bg);color:var(--text);
           border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:15px;font-family:inherit}
 .editgrid textarea{min-height:64px;resize:vertical}
 .editbtns{display:flex;gap:8px;justify-content:flex-end;align-items:center;margin-top:10px;flex-wrap:wrap}
 .editbtns .save{background:#f2d675;color:#1c1b19;border:0;border-radius:10px;padding:8px 18px;font-weight:600;cursor:pointer}
 .editbtns .cancel{background:transparent;color:var(--muted);border:1px solid var(--line);border-radius:10px;padding:8px 14px;cursor:pointer}
 .editbtns .save:disabled{opacity:.6;cursor:wait}
 .editmsg{font-size:13px;margin-right:auto} .editmsg.err{color:#e0806c}
 @media (max-width:640px){ .editgrid{grid-template-columns:1fr} }
"""

# r = product row; button goes in the card's button row
EDIT_BTN = """{% if admin %}
      <button type="button" class="edit-btn" onclick="editOpen('{{ r.fi_record_id }}')">Edit</button>
      {% endif %}"""

# the boxes (hidden until Edit is clicked); goes at the bottom of the card
EDIT_FORM = """{% if admin %}
  <form class="editbox" id="edit-{{ r.fi_record_id }}" hidden onsubmit="editSave('{{ r.fi_record_id }}', this); return false;">
    <div class="editgrid">
""" + "".join(
    f"""      <div{' style="grid-column-start:1"' if col == 'top_notes' else ''}><label>{label}
        {'<textarea' if big else '<input'} name="{col}" {'' if big else 'value="{{ r.' + col + ' or \'\' }}"'}>{('{{ r.' + col + " or '' }}</textarea>") if big else ''}</label></div>
""" for col, label, big in EDIT_COLS) + """    </div>
    <div class="editbtns">
      <span class="editmsg" id="editmsg-{{ r.fi_record_id }}">Product title and SKU are not editable.</span>
      <button type="button" class="cancel" onclick="editClose('{{ r.fi_record_id }}')">Cancel</button>
      <button type="submit" class="save">Save</button>
    </div>
  </form>
  {% endif %}"""

EDIT_JS = """  // ---- Edit (admins only, item_edit.py) ----
  function editOpen(fi) {
    var f = document.getElementById("edit-" + fi);
    f.dataset.orig = JSON.stringify(Object.fromEntries(new FormData(f)));   // to put back on Cancel
    f.hidden = false; f.querySelector("input,textarea").focus();
    console.log("[ITEM-EDIT] " + fi + ": edit opened");
  }
  function editClose(fi) {
    var f = document.getElementById("edit-" + fi);
    var orig = JSON.parse(f.dataset.orig || "{}");
    Object.keys(orig).forEach(function (k) { f.elements[k].value = orig[k]; });    // undo typing
    f.hidden = true;
  }
  async function editSave(fi, f) {
    var msg = document.getElementById("editmsg-" + fi), btn = f.querySelector(".save");
    btn.disabled = true; btn.textContent = "Saving ..."; msg.className = "editmsg"; msg.textContent = "";
    try {
      var r = await fetch("@@SAVE_URL@@", {method: "POST", headers: {"Content-Type": "application/json"},
                          body: JSON.stringify({fi_record_id: fi, fields: Object.fromEntries(new FormData(f))})});
      var d = await r.json().catch(function () { return {ok: false, error: "HTTP " + r.status}; });
      if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
      console.log("[ITEM-EDIT] " + fi + ": saved " + (d.changed || []).join(", "));
      location.reload();                                  // card shows the new values
    } catch (e) {
      msg.className = "editmsg err"; msg.textContent = "Not saved: " + e.message;
      console.log("[ITEM-EDIT] " + fi + ": ERROR " + e.message);
      btn.disabled = false; btn.textContent = "Save";
    }
  }
""".replace("@@SAVE_URL@@", SAVE_URL)


# ============================================================ SECTION 3: save
def save_edit(conn, fid, fields, user):
    """Save the admin's changes. Returns (changed column list, error text or None).
    Only EDITABLE columns are used (product_name / sku / anything else sent is ignored)."""
    new = {}
    for col in EDITABLE:
        if col in fields:
            v = str(fields.get(col) or "").strip()
            new[col] = v or None                          # empty box -> empty in the database
    if not new:
        return [], "nothing to save"
    cur = conn.cursor()
    cur.execute(f"SELECT {', '.join(EDITABLE)} FROM item_master WHERE fi_record_id = %s FOR UPDATE", (fid,))
    row = cur.fetchone()
    if row is None:
        return [], "product not in Item Master"
    old = dict(zip(EDITABLE, row))
    changed = [c for c, v in new.items() if (old.get(c) or None) != v]
    if not changed:
        log(f"[ITEM-EDIT] {fid}: nothing changed")
        return [], None
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
    return changed, None

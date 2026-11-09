"""
Sync the stock  (ADMIN ONLY - button "Sync the stock" on every Brands Store shop tab, e.g. ?tab=jz)
=====================================================================================================
Click -> popup with OUR products whose stock on fragrantsouq.com is DIFFERENT from that brand store:
   - our product must be on our website (competitor_table.our_link) and have a known stock
     (our_stock_qty, else our_stock from the website)
   - brand-store product = the row of that shop's list linked to our product (<p>_link),
     else the suggestion (<p>_suggestion) - user's choice 2026-11-09: "Linked + suggestions"
   - only brand rows 'In stock' / 'Out of stock' are compared ('Not on site' left out, user's choice)
Each line: our product, the brand product, Ours: In / Out -> Brand store: In / Out, a tick (+ "Tick all").
"Update ticked" -> one product at a time through the SAME route as the card's stock Edit
(stock_edit.py POST /stock/edit): Shopify FIRST, then competitor_table.our_stock_qty / our_stock + item_master.stock_qty.
   Brand store Out of stock -> qty 0.  Brand store In stock -> the number in the line's box: the brand store's
   stock number when its list has one (column stock_qty, none of the shop lists saves a number yet) else
   DEFAULT_IN_STOCK_QTY (1); the admin can change it before Update.
Shopify refuses a product -> that line says why, nothing saved for it, the others go on.

Where things are (change them here):
  SECTION 1  settings      -> LIST_URL, DEFAULT_IN_STOCK_QTY, COMPARE_STOCK
  SECTION 2  page parts    -> SYNC_CSS, SYNC_BUTTON (put on the page by competitors_page.py), SYNC_JS (popup)
  SECTION 3  list (route)  -> GET /stock-sync/list?shop=jz
  SECTION 4  hooks         -> init(): database connection from app.py
"""

from flask import Blueprint, request, jsonify, g

from auth import is_admin

stock_sync_bp = Blueprint("stock_sync", __name__)


def log(*a):
    """Print to the Render log (lines start with [STOCK-SYNC])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
LIST_URL = "/stock-sync/list"
DEFAULT_IN_STOCK_QTY = 1            # qty for "In stock" when the brand store's list has no stock number
COMPARE_STOCK = ("In stock", "Out of stock")    # brand rows compared ('Not on site' left out)


# ============================================================ SECTION 2: page parts (competitors_page.py puts them in)
SYNC_CSS = """ /* Sync the stock (admins, Brands Store tabs, stock_sync.py) */
 .sync-btn{background:#f2d675;color:#1c1b19;border:0;border-radius:8px;padding:7px 14px;font-weight:700;cursor:pointer;
           font-size:13px;margin-left:8px}
 .sync-bg{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;
          z-index:1000;padding:16px}
 .sync-box{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:12px;width:min(900px,100%);
           max-height:90vh;display:flex;flex-direction:column}
 .sync-head{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:10px;
            align-items:center;flex-wrap:wrap}
 .sync-head h3{margin:0;font-size:17px}
 .sync-list{overflow:auto;padding:6px 16px;flex:1}
 .sync-row{display:grid;grid-template-columns:28px 1fr auto;gap:10px;align-items:center;padding:9px 0;
           border-bottom:1px solid var(--line);font-size:13px}
 .sync-row .nm b{display:block;font-size:14px} .sync-row .nm small{color:var(--muted)}
 .sync-row .nm a{color:var(--muted)}
 .sync-row .st{text-align:right;white-space:nowrap}
 .sync-row input[type=number]{width:70px;background:var(--bg);color:var(--text);border:1px solid var(--line);
           border-radius:6px;padding:4px 6px;margin-left:6px}
 .sync-in{color:#8fd19e;font-weight:700} .sync-out{color:#e0806c;font-weight:700}
 .sync-tag{font-size:11px;border:1px solid var(--line);border-radius:6px;padding:1px 5px;margin-left:4px;color:var(--muted)}
 .sync-msg{font-size:12px;margin-top:3px} .sync-msg.err{color:#e0806c} .sync-msg.ok{color:#8fd19e}
 .sync-foot{padding:12px 16px;border-top:1px solid var(--line);display:flex;gap:8px;justify-content:flex-end;
            align-items:center;flex-wrap:wrap}
 .sync-foot button{border-radius:8px;padding:7px 14px;font-weight:600;cursor:pointer;font-size:13px}
 .sync-go{background:#f2d675;color:#1c1b19;border:0} .sync-go:disabled{opacity:.5;cursor:default}
 .sync-close{background:none;color:var(--muted);border:1px solid var(--line)}
 .sync-info{color:var(--muted);font-size:13px;margin-right:auto}
"""

# button next to the "N products" line of a Brands Store tab (admins only - competitors_page.py checks)
SYNC_BUTTON = """<button type="button" class="sync-btn" data-shop="{{ src.key }}" data-label="{{ src.label }}"
          onclick="syncOpen(this.dataset.shop, this.dataset.label)">Sync the stock</button>"""

SYNC_JS = r"""  // ---- Sync the stock (admins, Brands Store tabs, stock_sync.py) ----
  // popup = our products whose stock differs from this brand store; ticked ones -> POST /stock/edit one at a time
  function syncEl(tag, cls, text) {
    var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e;
  }
  function syncState(inStock) { return syncEl("span", inStock ? "sync-in" : "sync-out", inStock ? "In stock" : "Out of stock"); }
  async function syncOpen(shop, label) {
    console.log("[STOCK-SYNC] " + shop + ": reading products with different stock");
    var bg = syncEl("div", "sync-bg"), box = syncEl("div", "sync-box");
    var head = syncEl("div", "sync-head"), list = syncEl("div", "sync-list"), foot = syncEl("div", "sync-foot");
    head.appendChild(syncEl("h3", "", "Sync the stock - " + label));
    var allLbl = syncEl("label", ""), all = document.createElement("input");
    all.type = "checkbox"; allLbl.appendChild(all); allLbl.appendChild(document.createTextNode(" Tick all"));
    head.appendChild(allLbl);
    var info = syncEl("span", "sync-info", "Loading ...");
    var go = syncEl("button", "sync-go", "Update ticked"), cl = syncEl("button", "sync-close", "Close");
    go.type = cl.type = "button"; go.disabled = true;
    foot.appendChild(info); foot.appendChild(cl); foot.appendChild(go);
    box.appendChild(head); box.appendChild(list); box.appendChild(foot); bg.appendChild(box);
    document.body.appendChild(bg);
    var busy = false, changed = false;
    function close() { if (busy) return; bg.remove(); if (changed) location.reload(); }
    cl.onclick = close;
    bg.onclick = function (e) { if (e.target === bg) close(); };
    var ticks = [];
    function count() {
      var n = ticks.filter(function (t) { return t.box.checked && !t.done; }).length;
      go.disabled = busy || n === 0; go.textContent = n ? "Update ticked (" + n + ")" : "Update ticked";
      all.checked = ticks.length > 0 && ticks.every(function (t) { return t.box.checked || t.done; });
    }
    all.onchange = function () { ticks.forEach(function (t) { if (!t.done) t.box.checked = all.checked; }); count(); };
    var d;
    try {
      var r = await fetch("@@LIST_URL@@?shop=" + encodeURIComponent(shop));
      d = await r.json().catch(function () { return {ok: false, error: "HTTP " + r.status + " (logged out? reload the page)"}; });
      if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
    } catch (e) {
      info.textContent = "Could not read the list: " + e.message; info.className = "sync-info sync-out"; return;
    }
    info.textContent = d.items.length ? d.items.length + " product(s) with a different stock" :
                       "No differences - our stock matches " + label + ".";
    d.items.forEach(function (it) {
      var row = syncEl("div", "sync-row"), box = document.createElement("input"), nm = syncEl("div", "nm"),
          st = syncEl("div", "st"), msg = syncEl("div", "sync-msg");
      box.type = "checkbox"; box.onchange = count;
      nm.appendChild(syncEl("b", "", (it.sku ? it.sku + " - " : "") + (it.name || it.fi)));
      var sub = syncEl("small", "");
      var a = syncEl("a", "", it.brand_name || it.brand_url); a.href = it.brand_url; a.target = "_blank";
      sub.appendChild(document.createTextNode(label + ": ")); sub.appendChild(a);
      sub.appendChild(syncEl("span", "sync-tag", it.linked ? "linked" : "suggestion"));
      nm.appendChild(sub); nm.appendChild(msg);
      st.appendChild(document.createTextNode("Ours: ")); st.appendChild(syncState(it.our_in));
      st.appendChild(document.createTextNode(" (" + (it.our_qty == null ? "-" : it.our_qty) + ") -> " + label + ": "));
      st.appendChild(syncState(it.brand_in));
      var qty = null;
      if (it.brand_in) {                                   // In stock -> number to set (brand's number, else default)
        qty = document.createElement("input"); qty.type = "number"; qty.min = "1"; qty.step = "1";
        qty.value = it.new_qty; qty.title = "Stock number to set in Shopify"; st.appendChild(qty);
      } else { st.appendChild(document.createTextNode(" (0)")); }
      row.appendChild(box); row.appendChild(nm); row.appendChild(st); list.appendChild(row);
      ticks.push({it: it, box: box, qty: qty, msg: msg, done: false});
    });
    count();
    go.onclick = async function () {
      var todo = ticks.filter(function (t) { return t.box.checked && !t.done; });
      for (var i = 0; i < todo.length; i++) {              // check the numbers first
        var v = todo[i].qty ? todo[i].qty.value.trim() : "0";
        if (!/^[0-9]+$/.test(v) || (todo[i].qty && parseInt(v, 10) < 1)) {
          todo[i].msg.className = "sync-msg err"; todo[i].msg.textContent = "Enter a whole number (1 or more)."; return;
        }
      }
      busy = true; count(); cl.disabled = true; all.disabled = true;
      var ok = 0, bad = 0;
      for (var j = 0; j < todo.length; j++) {
        var t = todo[j], q = t.qty ? parseInt(t.qty.value, 10) : 0;
        info.textContent = "Updating " + (j + 1) + " of " + todo.length + " ...";
        t.msg.className = "sync-msg"; t.msg.textContent = "Updating Shopify...";
        try {
          var r = await fetch("@@SAVE_URL@@", {method: "POST", headers: {"Content-Type": "application/json"},
                                               body: JSON.stringify({fi_record_id: t.it.fi, qty: q})});
          var res = await r.json().catch(function () { return {ok: false, error: "HTTP " + r.status}; });
          if (!res.ok) throw new Error(res.error || ("HTTP " + r.status));
          t.done = true; t.box.checked = false; t.box.disabled = true; if (t.qty) t.qty.disabled = true;
          t.msg.className = "sync-msg ok"; t.msg.textContent = "Updated in Shopify + Competitor table: stock " + res.qty;
          ok++; changed = true;
          console.log("[STOCK-SYNC] " + t.it.fi + ": stock " + res.qty);
        } catch (e) {
          t.msg.className = "sync-msg err"; t.msg.textContent = "Not updated: " + e.message; bad++;
          console.log("[STOCK-SYNC] " + t.it.fi + ": ERROR " + e.message);
        }
      }
      busy = false; cl.disabled = false; all.disabled = false; count();
      info.textContent = ok + " updated" + (bad ? ", " + bad + " not updated (see the red lines)" : "") + ".";
    };
  }
"""


def sync_js():
    """SYNC_JS with the route addresses filled in (stock_edit.SAVE_URL = the card's stock Edit route)."""
    import stock_edit
    return SYNC_JS.replace("@@LIST_URL@@", LIST_URL).replace("@@SAVE_URL@@", stock_edit.SAVE_URL)


# ============================================================ SECTION 3: list (route)
_hooks = {}                                          # filled by init() (SECTION 4)

# our in-stock: the stock NUMBER when we have one, else the website's in/out flag
OUR_IN_SQL = "(CASE WHEN ct.our_stock_qty IS NOT NULL THEN ct.our_stock_qty > 0 ELSE ct.our_stock END)"


def diff_rows(cur, key, table):
    """Our products whose stock differs from brand store <key> (its list = <table>). Returns a list of dicts."""
    cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = 'stock_qty'", (table,))
    qty_col = "cat.stock_qty" if cur.fetchone() else "NULL"      # brand store's stock number, when its list has one
    cur.execute(f"""
        SELECT DISTINCT ON (ct.fi_record_id)
               ct.fi_record_id, ct.sku, ct.product_name, ct.our_stock_qty, {OUR_IN_SQL} AS our_in,
               ct.{key}_link IS NOT NULL AS linked, cat.name, cat.product_url, cat.stock = 'In stock' AS brand_in,
               {qty_col} AS brand_qty
          FROM competitor_table ct
          JOIN {table} cat ON cat.product_url = COALESCE(ct.{key}_link, ct.{key}_suggestion)
         WHERE ct.our_link IS NOT NULL
           AND (ct.our_stock_qty IS NOT NULL OR ct.our_stock IS NOT NULL)
           AND cat.stock IN %s
           AND {OUR_IN_SQL} <> (cat.stock = 'In stock')
         ORDER BY ct.fi_record_id, cat.updated_at DESC NULLS LAST""", (COMPARE_STOCK,))
    items = []
    for fid, sku, name, our_qty, our_in, linked, bname, burl, brand_in, brand_qty in cur.fetchall():
        new_qty = 0
        if brand_in:
            new_qty = int(brand_qty) if brand_qty and int(brand_qty) > 0 else DEFAULT_IN_STOCK_QTY
        items.append({"fi": fid, "sku": sku, "name": name, "our_qty": our_qty, "our_in": bool(our_in),
                      "linked": bool(linked), "brand_name": bname, "brand_url": burl, "brand_in": bool(brand_in),
                      "new_qty": new_qty})
    items.sort(key=lambda x: (not x["linked"], (x["name"] or "").lower()))   # linked first, then by name
    return items


@stock_sync_bp.get(LIST_URL)
def sync_list():
    """?shop=jz -> {"ok": true, "items": [...]} = our products whose stock differs from that Brands Store shop."""
    if not is_admin():
        log(f"[STOCK-SYNC] Refused: {g.get('user')} is not an admin")
        return jsonify({"ok": False, "error": "admins only"}), 403
    import competitors_page as cp                    # shop list (SOURCES); imported here = no import loop
    key = (request.args.get("shop") or "").strip()
    src = cp.SOURCE_BY_KEY.get(key)
    if not src or src.get("menu") != "brands" or not src.get("table"):
        return jsonify({"ok": False, "error": "not a Brands Store shop"}), 400
    conn = _hooks["connect"]()
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (src["table"],))
        if not cur.fetchone()[0]:
            return jsonify({"ok": True, "items": []})
        items = diff_rows(cur, key, src["table"])
        conn.commit()
    except Exception as ex:
        conn.rollback()
        log(f"[STOCK-SYNC] {key}: ERROR {ex}")
        return jsonify({"ok": False, "error": str(ex)[:200]}), 500
    finally:
        conn.close()
    log(f"[STOCK-SYNC] {g.get('user')} -> {src['label']}: {len(items)} product(s) with a different stock "
        f"({sum(1 for i in items if i['linked'])} linked, {sum(1 for i in items if not i['linked'])} suggestions)")
    return jsonify({"ok": True, "items": items})


# ============================================================ SECTION 4: hooks
def init(connect):
    """connect() -> new Postgres connection (app.pg_conn)."""
    _hooks["connect"] = connect
    log("[STOCK-SYNC] Linked to app.py: 'Sync the stock' on the Brands Store tabs")

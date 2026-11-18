r"""
Bundle menu: list of bundles + Create bundle form (2026-11-12/13)
=================================================================
User: menu "Bundle" after Item Master with "Create bundle form"; then the form:
  1. pick products: minimum 2, up to 5 (prod1 + prod2 + ...), each with a QUANTITY (user's choice: "yes, with quantity")
  2. Bundle title - default "1st + 2nd + ..." (product names), can be edited
  3. Bundle discount - an AMOUNT (AED) or a %
  4. Auto SKU - the product SKUs joined together, e.g. AFN1057ARFM1002 (user: "for now ... can change later")
  5. Add - ONLY when EVERY product has enough stock in SHOPIFY (live check at Add; out of stock -> not added)
  Saved in OUR database (table `bundles`), shown in the list "Bundles" like Item Master (user's choice) - NOT in Shopify.
  Price = sum of each product's UAE price x quantity (user's choice: UAE price), minus the discount.

Menu Bundle ▾ -> Bundles (/bundle) | Create bundle form (/bundle/new). Creating = admins only; everyone logged in sees the list.

Where things are (change them here):
  SECTION 1  settings        -> BUNDLE_OPTIONS (menu), MIN_ITEMS / MAX_ITEMS / MAX_QTY, SKU_JOIN
  SECTION 2  table           -> BUNDLES_CREATE (bundles)
  SECTION 3  products        -> product_rows(), GET /bundle/products?q= (search for the form)
  SECTION 4  Shopify stock   -> shopify_stock() (live 'available' of one SKU)
  SECTION 5  create / edit   -> POST /bundle/create, POST /bundle/update (same checks, _save_bundle)
  SECTION 6  pages           -> FORM_HTML (/bundle/new, /bundle/edit/<id>), LIST_HTML (/bundle)
  SECTION 7  hooks           -> init(): database connection from app.py
"""

import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, ROUND_HALF_UP

import psycopg2.extras
from flask import Blueprint, render_template_string, request, jsonify, g, redirect

from auth import is_admin

bundle_bp = Blueprint("bundle", __name__)


def log(*a):
    """Print to the Render log (lines start with [BUNDLE])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
# Bundle dropdown: (key = cur_key of that page, link, text). New pages = one more line here.
BUNDLE_OPTIONS = [
    ("bundle-list", "/bundle", "Bundles"),
    ("bundle-new", "/bundle/new", "Create bundle form"),
]
MIN_ITEMS, MAX_ITEMS = 2, 5          # products in one bundle
MAX_QTY = 20                         # quantity of one product in a bundle
SKU_JOIN = ""                        # AFN1057 + ARM1002 -> "AFN1057ARM1002" (change to "-" / "+" later if wanted)
PICK_LIMIT = 50                      # products listed in the "Select products" popup (search narrows it down)


# ============================================================ SECTION 2: table
BUNDLES_CREATE = """
CREATE TABLE IF NOT EXISTS bundles (
    id              SERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    sku             TEXT NOT NULL,
    items           JSONB NOT NULL,          -- [{fi, sku, name, qty, price, image}] in the order picked
    total_price     NUMERIC(10,2),           -- sum of UAE price x qty
    discount_type   TEXT,                    -- 'amount' (AED) or 'percent'
    discount_value  NUMERIC(10,2),
    discount_aed    NUMERIC(10,2),           -- the discount in AED
    bundle_price    NUMERIC(10,2),           -- total_price - discount_aed
    stock_check     JSONB,                   -- Shopify stock of every product at Add: {sku: available}
    created_by      TEXT,
    created_at      TIMESTAMP DEFAULT NOW()
);
ALTER TABLE bundles ADD COLUMN IF NOT EXISTS updated_by TEXT;       -- Edit (2026-11-13): last change by
ALTER TABLE bundles ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;  -- and when
ALTER TABLE bundles ADD COLUMN IF NOT EXISTS active_until DATE;     -- listing period (2026-11-13): last day; NULL = no end
"""


def money(x):
    """Decimal with 2 places (AED)."""
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---- LISTING PERIOD (2026-11-13, user: "if date given the stock will be zero after the date, otherwise infinity")
LISTING_TZ = ZoneInfo("Asia/Dubai")        # the day changes at midnight UAE time


def today_ae():
    return datetime.now(LISTING_TZ).date()


def bundle_stock(items, stock_of, active_until, today=None):
    """How many bundles can be sold: 0 after active_until (that day itself still counts), else the
    smallest (product stock // qty) over its products, from OUR database (our_stock_qty). None = a stock not known."""
    if active_until and (today or today_ae()) > active_until:
        return 0
    n = None
    for it in items or []:
        st = stock_of.get(it.get("fi"))
        if st is None:
            return None
        k = max(0, int(st)) // max(1, int(it.get("qty") or 1))
        n = k if n is None else min(n, k)
    return n


# ============================================================ SECTION 3: products
_hooks = {}

PRODUCT_SQL = """
SELECT im.fi_record_id AS fi, im.sku, COALESCE(NULLIF(im.product_name, ''), ct.product_name) AS name, im.brand,
       COALESCE(ct.uae_price, im.uae_price) AS price, im.image_urls, im.shopify_id,
       ct.our_stock_qty AS stock, ct.our_link
  FROM item_master im LEFT JOIN competitor_table ct ON ct.fi_record_id = im.fi_record_id
"""


def first_image(urls):
    """First link of image_urls (one link per line) or None."""
    for line in (urls or "").splitlines():
        if line.strip().startswith("http"):
            return line.strip()
    return None


def product_rows(cur, where, params, limit=15):
    cur.execute(PRODUCT_SQL + where + " LIMIT %s", (*params, limit))
    out = []
    for r in cur.fetchall():
        out.append({"fi": r["fi"], "sku": (r["sku"] or "").strip(), "name": r["name"] or r["sku"] or r["fi"],
                    "brand": r["brand"], "price": float(r["price"]) if r["price"] is not None else None,
                    "image": first_image(r["image_urls"]), "shopify_id": (r["shopify_id"] or "").strip(),
                    "stock": float(r["stock"]) if r["stock"] is not None else None, "on_site": bool(r["our_link"])})
    return out


@bundle_bp.get("/bundle/products")
def bundle_products():
    """?q=words -> up to PICK_LIMIT products for the "Select products" popup (every word in SKU / brand / name /
    perfume / barcode, like the Item Master search). Empty q -> the first products (popup opens with a list)."""
    q = (request.args.get("q") or "").strip()
    words = [w for w in re.split(r"\s+", q) if w][:6]
    where = (" WHERE " + " AND ".join(
        "(COALESCE(im.sku,'') || ' ' || COALESCE(im.brand,'') || ' ' || COALESCE(im.product_name,'') || ' ' || "
        "COALESCE(im.perfume_name,'') || ' ' || COALESCE(im.barcode,'')) ILIKE %s" for _ in words)) if words else ""
    where += " ORDER BY (ct.our_link IS NULL), im.brand, im.product_name"
    conn = _hooks["connect"]()
    try:
        items = product_rows(conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor), where,
                             [f"%{w}%" for w in words], limit=PICK_LIMIT)
    finally:
        conn.close()
    log(f"[BUNDLE] search {q!r}: {len(items)} products")
    return jsonify({"ok": True, "items": items})


# ============================================================ SECTION 4: Shopify stock
def shopify_stock(sku, shopify_id):
    """Live Shopify 'available' of this SKU (all locations) -> (number, None) or (None, reason)."""
    import shopify_create as sc
    import item_edit
    if not (sc.SHOP and sc.TOKEN):
        return None, "Shopify is not set up (SHOPIFY_SHOP / token missing in Render)"
    found = item_edit._find_in_shopify(sc, sku.upper(), shopify_id)
    if not found:
        return None, "not found in Shopify"
    _, vid, _ = found
    r = sc.shopify_rest("GET", f"variants/{vid}.json?fields=id,sku,inventory_quantity")
    if not r.ok:
        return None, f"Shopify answered HTTP {r.status_code}"
    return int((r.json().get("variant") or {}).get("inventory_quantity") or 0), None


# ============================================================ SECTION 5: create / edit
@bundle_bp.post("/bundle/create")
def bundle_create():
    """Add bundle (Create bundle form)."""
    return _save_bundle(None)


@bundle_bp.post("/bundle/update")
def bundle_update():
    """Save changes (Edit on the Bundles list, 2026-11-13). Body = create's + "id". SAME checks as Add
    (live Shopify stock too); title / SKU / prices made again from the products now picked."""
    try:
        bid = int((request.get_json(silent=True) or {}).get("id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "no bundle given"}), 400
    return _save_bundle(bid)


def _save_bundle(bid):
    """Body: {items: [{fi, qty}], title, discount_type: 'amount'|'percent', discount_value}.
    Prices / SKUs / names come from OUR database (not from the page). Every product must have stock in Shopify
    (>= its quantity), else nothing is saved and the answer lists the products that block it.
    bid None = INSERT a new bundle, else UPDATE bundle bid."""
    if not is_admin():
        return jsonify({"ok": False, "error": "admins only"}), 403
    body = request.get_json(silent=True) or {}
    what = "Add" if bid is None else f"Edit #{bid}"
    lines = body.get("items") or []
    # ---- check what was sent
    picked, seen = [], set()
    for it in lines:
        fi = str((it or {}).get("fi") or "").strip()
        try:
            qty = int((it or {}).get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if not fi:
            continue
        if fi in seen:
            return jsonify({"ok": False, "error": "The same product is picked twice - use its quantity instead"}), 400
        if not 1 <= qty <= MAX_QTY:
            return jsonify({"ok": False, "error": f"Quantity must be 1 - {MAX_QTY}"}), 400
        seen.add(fi)
        picked.append((fi, qty))
    if not MIN_ITEMS <= len(picked) <= MAX_ITEMS:
        return jsonify({"ok": False, "error": f"Pick {MIN_ITEMS} to {MAX_ITEMS} products"}), 400
    dtype = body.get("discount_type") if body.get("discount_type") in ("amount", "percent") else "amount"
    until_txt = str(body.get("active_until") or "").strip()            # 'YYYY-MM-DD' or '' = no end date
    try:
        until = date.fromisoformat(until_txt) if until_txt else None
    except ValueError:
        return jsonify({"ok": False, "error": "Active until: not a valid date"}), 400
    if until and bid is None and until < today_ae():
        return jsonify({"ok": False, "error": "Active until cannot be a past date"}), 400
    try:
        dval = money(body.get("discount_value") or 0)
    except Exception:
        return jsonify({"ok": False, "error": "Discount must be a number"}), 400
    if dval < 0 or (dtype == "percent" and dval > 100):
        return jsonify({"ok": False, "error": "Discount must be 0 - 100 %" if dtype == "percent"
                        else "Discount cannot be below 0"}), 400

    conn = _hooks["connect"]()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(BUNDLES_CREATE)
        rows = {r["fi"]: r for r in product_rows(cur, " WHERE im.fi_record_id = ANY(%s)",
                                                 [[fi for fi, _ in picked]], limit=MAX_ITEMS)}
        conn.commit()
        problems, items = [], []
        for fi, qty in picked:
            r = rows.get(fi)
            if not r:
                problems.append(f"{fi}: not in Item Master")
                continue
            if not r["sku"]:
                problems.append(f"{r['name']}: has no SKU")
            if r["price"] is None or r["price"] <= 0:
                problems.append(f"{r['name']}: has no UAE price")
            items.append({"fi": fi, "sku": r["sku"], "name": r["name"], "qty": qty, "price": r["price"],
                          "image": r["image"], "shopify_id": r["shopify_id"]})
        if problems:
            return jsonify({"ok": False, "error": "Cannot add: " + "; ".join(problems)}), 400
        # ---- 5. every product must have stock in SHOPIFY (>= quantity)
        stock, out = {}, []
        for it in items:
            try:
                avail, why = shopify_stock(it["sku"], it["shopify_id"])
            except Exception as ex:
                avail, why = None, f"Shopify error: {str(ex)[:120]}"
            stock[it["sku"]] = avail
            if avail is None:
                out.append(f"{it['name']} ({it['sku']}): {why}")
            elif avail < it["qty"]:
                out.append(f"{it['name']} ({it['sku']}): only {avail} in stock in Shopify, bundle needs {it['qty']}")
        log(f"[BUNDLE] {g.get('user')} {what}: Shopify stock {stock}")
        if out:
            log(f"[BUNDLE] {what} NOT saved - out of stock: {out}")
            return jsonify({"ok": False, "error": ("Not added" if bid is None else "Not saved")
                            + " - out of stock in Shopify: " + "; ".join(out),
                            "stock": stock}), 409
        # ---- price, title, SKU
        total = sum(money(it["price"]) * it["qty"] for it in items)
        disc = money(total * dval / 100) if dtype == "percent" else dval
        if disc >= total:
            return jsonify({"ok": False, "error": f"Discount (AED {disc}) must be smaller than the total (AED {total})"}), 400
        default_title = " + ".join(it["name"] for it in items)
        title = re.sub(r"\s+", " ", str(body.get("title") or "")).strip()[:300] or default_title
        sku = SKU_JOIN.join(it["sku"].upper() for it in items)
        for it in items:
            it.pop("shopify_id", None)
        vals = (title, sku, json.dumps(items), total, dtype, dval, disc, total - disc, json.dumps(stock), until,
                g.get("user"))
        if bid is None:
            cur.execute("""INSERT INTO bundles (title, sku, items, total_price, discount_type, discount_value, discount_aed,
                                                bundle_price, stock_check, active_until, created_by)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""", vals)
        else:
            cur.execute("""UPDATE bundles SET title = %s, sku = %s, items = %s, total_price = %s, discount_type = %s,
                                              discount_value = %s, discount_aed = %s, bundle_price = %s,
                                              stock_check = %s, active_until = %s, updated_by = %s, updated_at = NOW()
                           WHERE id = %s RETURNING id""", vals + (bid,))
        got = cur.fetchone()
        if not got:
            conn.rollback()
            return jsonify({"ok": False, "error": "bundle not found (removed?)"}), 404
        bid = got["id"]
        conn.commit()
    except Exception as ex:
        conn.rollback()
        log(f"[BUNDLE] ERROR {ex}")
        return jsonify({"ok": False, "error": str(ex)[:200]}), 500
    finally:
        conn.close()
    log(f"[BUNDLE] {what} saved #{bid} '{title}' SKU {sku}: total {total} - {disc} = {total - disc}, "
        f"active until {until or 'no end date'}")
    return jsonify({"ok": True, "id": bid, "sku": sku, "title": title, "bundle_price": float(total - disc)})


# ============================================================ SECTION 6: pages
PAGE_HEAD = """<!doctype html><html><head><title>@@TITLE@@ - Bundle</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#1c1b19;--card:#252420;--line:#3a3833;--text:#f2efe9;--muted:#a9a397;--gold:#d8b76e;--yellow:#f2d675}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--text);font-family:Roboto,-apple-system,Segoe UI,Arial,sans-serif}
 .page{max-width:920px;margin:0 auto;padding:22px 18px 40px}
 h1{font-size:26px;margin:4px 0 18px}
 .topbar{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
 .who{color:var(--muted);font-size:14px} .who b{color:var(--text)} .who a{color:var(--gold)}
 {{ nav_css|safe }}
 .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px 22px;margin-top:14px}
 .card h2{font-size:20px;margin:0 0 4px}
 .sub{color:var(--muted);font-size:14px;margin:0 0 14px}
 input,select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:10px 12px;
              font-size:15px;font-family:inherit}
 .btn{background:var(--yellow);color:#1c1b19;border:0;border-radius:10px;padding:11px 20px;font-weight:700;font-size:15px;cursor:pointer}
 .btn:disabled{opacity:.5;cursor:default}
 .ghost{background:none;color:var(--text);border:1px solid var(--line);border-radius:10px;padding:9px 14px;cursor:pointer;font-size:14px}
 .thumb{width:46px;height:46px;border-radius:8px;object-fit:cover;background:#111;flex:0 0 46px;display:flex;align-items:center;
        justify-content:center;color:var(--muted);font-size:20px}
 .muted{color:var(--muted)} .ok{color:#8fd19e} .err{color:#e0806c}
</style>"""

FORM_HTML = PAGE_HEAD.replace("@@TITLE@@", "Create bundle") + r"""
<style>
 .lines{display:flex;flex-direction:column;gap:10px}
 .line{display:flex;gap:10px;align-items:center;border:1px solid var(--line);border-radius:12px;padding:10px;position:relative}
 .line .num{width:24px;height:24px;border-radius:50%;background:var(--line);display:flex;align-items:center;justify-content:center;
            font-size:13px;flex:0 0 24px}
 .chosen{flex:1;display:flex;gap:10px;align-items:center;min-width:0}
 .chosen .t{flex:1;min-width:0} .chosen .t b{display:block;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .chosen small{color:var(--muted)}
 .qty{display:flex;align-items:center;gap:6px} .qty input{width:64px;text-align:center}
 .x{background:none;border:0;color:var(--muted);font-size:20px;cursor:pointer;padding:4px 8px}
 .x:hover{color:#e0806c}
 .row2{display:grid;grid-template-columns:1fr;gap:6px;margin-top:6px}
 label.l{font-weight:600;font-size:14px}
 .title-in{width:100%}
 .reset{background:none;border:0;color:var(--gold);cursor:pointer;font-size:13px;padding:0;text-decoration:underline}
 .seg{display:inline-flex;border:1px solid var(--line);border-radius:10px;overflow:hidden}
 .seg button{background:none;border:0;color:var(--text);padding:9px 16px;cursor:pointer;font-weight:600}
 .seg button.on{background:var(--yellow);color:#1c1b19}
 .disc{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 .disc input{width:130px}
 .sum{display:grid;grid-template-columns:1fr auto;gap:6px 18px;font-size:15px;max-width:380px}
 .sum .big{font-size:22px;font-weight:700;color:var(--yellow)}
 .skubox{font-family:ui-monospace,Consolas,monospace;background:var(--bg);border:1px solid var(--line);border-radius:10px;
         padding:10px 12px;word-break:break-all}
 .msg{margin-top:12px;font-size:14px}
 .empty{color:var(--muted);font-size:14px;padding:14px;border:1px dashed var(--line);border-radius:12px;text-align:center}
 /* "Select products" popup (like Shopify's) */
 .pick-bg{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:200;display:flex;align-items:center;justify-content:center;padding:16px}
 .pick-bg[hidden]{display:none}
 .pick-box{background:var(--card);border:1px solid var(--line);border-radius:16px;width:min(760px,100%);max-height:88vh;
           display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.6)}
 .pick-head{display:flex;justify-content:space-between;align-items:center;padding:16px 20px;font-size:18px}
 .pclose{background:none;border:1px solid var(--line);color:var(--text);width:34px;height:34px;border-radius:50%;font-size:20px;cursor:pointer}
 .pick-search{padding:0 20px 12px} .pick-search input{width:100%}
 .pcols,.prow{display:grid;grid-template-columns:28px 52px 1fr 90px 110px;gap:10px;align-items:center}
 .pcols{padding:8px 20px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);color:var(--muted);font-size:13px}
 .pick-list{overflow:auto;flex:1;min-height:200px}
 .prow{padding:10px 20px;border-bottom:1px solid var(--line);cursor:pointer}
 .prow:hover{background:#2d2b26} .prow.off{opacity:.45;cursor:default}
 .prow input{width:18px;height:18px;padding:0;accent-color:#f2d675}
 .pname span{display:block;font-size:14px} .pname small{color:var(--muted);font-size:12px}
 .pavail{font-size:14px} .pavail.zero{color:#f0a35e}
 .pprice{text-align:right;font-size:14px;white-space:nowrap}
 .pempty{padding:24px 20px;color:var(--muted);text-align:center}
 .pick-foot{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:12px 20px;border-top:1px solid var(--line)}
 .pcount{background:var(--bg);border-radius:20px;padding:6px 12px;color:var(--muted);font-size:13px}
 @media (max-width:600px){ .pcols,.prow{grid-template-columns:24px 44px 1fr 70px} .pprice,.pcols span:last-child{display:none} }
</style></head><body><div class="page">
<div class="topbar"><h1>{{ 'Edit bundle' if edit else 'Create bundle' }}</h1>
  {% if g.user %}<span class="who">Logged in as <b>{{ g.user }}</b> · <a href="/logout">Log out</a></span>{% endif %}</div>
{{ nav_html|safe }}
{% if not admin %}
<div class="card"><p class="sub">Only admins can create or edit bundles. <a href="/bundle" style="color:var(--gold)">See the bundles</a></p></div>
{% else %}
<div class="card">
  <h2>1. Products</h2>
  <p class="sub">Pick {{ min_items }} to {{ max_items }} products and set each quantity.</p>
  <div class="lines" id="lines"></div>
  <div style="margin-top:10px"><button type="button" class="ghost" id="openPick" onclick="openPicker()">Select products</button>
    <span class="muted" id="lineInfo" style="font-size:13px;margin-left:8px"></span></div>
</div>
{# "Select products" popup like Shopify's (search, tick, picture, Available, Price; N/5 selected; Cancel / Add) #}
<div class="pick-bg" id="pickBg" hidden onclick="if (event.target === this) closePicker()">
  <div class="pick-box" role="dialog" aria-label="Select products">
    <div class="pick-head"><b>Select products</b><button type="button" class="pclose" onclick="closePicker()" aria-label="Close">&times;</button></div>
    <div class="pick-search"><input id="pickSearch" placeholder="Search products (name, brand, SKU, barcode)" oninput="loadPick(this.value)"
         onkeydown="if (event.key === 'Escape') closePicker()"></div>
    <div class="pcols"><span></span><span></span><span>Product</span><span>Available</span><span style="text-align:right">Price</span></div>
    <div class="pick-list" id="pickList"></div>
    <div class="pick-foot"><span class="pcount" id="pickN"></span>
      <span><button type="button" class="ghost" onclick="closePicker()">Cancel</button>
            <button type="button" class="btn" id="pickAdd" onclick="pickDone()">Add</button></span></div>
  </div>
</div>
<div class="card">
  <h2>2. Bundle title</h2>
  <div class="row2">
    <input class="title-in" id="title" placeholder="Pick products first" oninput="titleTouched=true; refresh()">
    <span><button type="button" class="reset" onclick="titleTouched=false; refresh()">Use the default title</button>
      <span class="muted" style="font-size:13px"> (product names joined with +)</span></span>
  </div>
</div>
<div class="card">
  <h2>3. Bundle discount</h2>
  <div class="disc">
    <div class="seg"><button type="button" id="segAmt" class="on" onclick="setType('amount')">Amount (AED)</button>
      <button type="button" id="segPct" onclick="setType('percent')">Percent (%)</button></div>
    <input type="number" id="dval" min="0" step="0.01" value="0" oninput="refresh()">
  </div>
  <div class="sum" style="margin-top:14px">
    <span class="muted">Products total (UAE price x qty)</span><span id="sTotal">AED 0.00</span>
    <span class="muted">Discount</span><span id="sDisc">- AED 0.00</span>
    <span>Bundle price</span><span class="big" id="sPrice">AED 0.00</span>
  </div>
</div>
<div class="card">
  <h2>4. Auto SKU</h2>
  <div class="skubox" id="sku">-</div>
</div>
<div class="card">
  <h2>5. Listing period</h2>
  <p class="sub">Last day the bundle is sold. After this date its stock becomes 0. Empty = no end date.</p>
  <div class="row2">
    <span class="muted">Active until</span>
    <input type="date" id="until" min="{{ today }}" oninput="refresh()" style="max-width:200px">
    <button type="button" class="reset" onclick="document.getElementById('until').value=''; refresh()">No end date</button>
    <span class="muted" id="untilInfo" style="font-size:13px"></span>
  </div>
</div>
<div class="card">
  <h2>6. {{ 'Save' if edit else 'Add' }}</h2>
  <p class="sub">The bundle is {{ 'saved' if edit else 'added' }} only when EVERY product has enough stock in Shopify ({{ shop or 'Shopify not set up' }}).</p>
  <button type="button" class="btn" id="addBtn" onclick="addBundle()" disabled>{{ 'Save changes' if edit else 'Add bundle' }}</button>
  {% if edit %}<a class="ghost" href="/bundle" style="text-decoration:none;margin-left:8px">Cancel</a>{% endif %}
  <div class="msg" id="msg"></div>
</div>
<script>
  // ---- Create bundle form (bundle.py): pick products -> title / discount / SKU follow -> Add (Shopify stock check) ----
  var MIN = {{ min_items }}, MAX = {{ max_items }}, MAXQ = {{ max_qty }}, JOIN = {{ sku_join|tojson }};
  var lines = [], titleTouched = false, dtype = "amount", timer = null;
  var EDIT = {{ edit|tojson }};                        // Edit (2026-11-13): saved bundle, else null = new bundle
  function el(t, c, txt) { var e = document.createElement(t); if (c) e.className = c; if (txt != null) e.textContent = txt; return e; }
  function aed(n) { return "AED " + (Math.round(n * 100) / 100).toFixed(2); }
  function thumb(src) {
    if (src) { var i = el("img", "thumb"); i.src = src; i.alt = ""; i.loading = "lazy"; return i; }
    return el("div", "thumb", "□");
  }
  // ---- chosen products (lines) ----
  function removeLine(i) { console.log("[BUNDLE] removed " + lines[i].p.sku); lines.splice(i, 1); draw(); }
  function draw() {
    var box = document.getElementById("lines"); box.innerHTML = "";
    if (!lines.length) box.appendChild(el("div", "empty", "No products yet - click “Select products”."));
    lines.forEach(function (ln, i) {
      var row = el("div", "line"); row.appendChild(el("span", "num", String(i + 1)));
      var c = el("div", "chosen"); c.appendChild(thumb(ln.p.image));
      var t = el("div", "t"); t.appendChild(el("b", "", ln.p.name));
      t.appendChild(el("small", "", (ln.p.sku || "no SKU") + " · " + (ln.p.price != null ? aed(ln.p.price) : "no UAE price") +
                       " · Available " + (ln.p.stock != null ? ln.p.stock : "-")));
      c.appendChild(t); row.appendChild(c);
      var q = el("div", "qty"); q.appendChild(el("span", "muted", "Qty"));
      var qi = el("input"); qi.type = "number"; qi.min = 1; qi.max = MAXQ; qi.value = ln.qty;
      qi.oninput = function () { ln.qty = Math.max(1, Math.min(MAXQ, parseInt(qi.value || "1", 10))); refresh(); };
      q.appendChild(qi); row.appendChild(q);
      var x = el("button", "x", "×"); x.type = "button"; x.title = "Remove"; x.onclick = function () { removeLine(i); };
      row.appendChild(x); box.appendChild(row);
    });
    document.getElementById("lineInfo").textContent = lines.length + " of " + MAX + " products" +
        (lines.length < MIN ? " · pick at least " + MIN : "");
    refresh();
  }
  // ---- "Select products" popup (like Shopify's): search + list with tick, picture, Available, Price ----
  var pickSel = {};                                    // fi -> product, ticked in the popup
  function openPicker() {
    pickSel = {}; lines.forEach(function (l) { pickSel[l.p.fi] = l.p; });   // already chosen = ticked
    document.getElementById("pickBg").hidden = false;
    var s = document.getElementById("pickSearch"); s.value = ""; s.focus();
    loadPick(""); pickCount();
    console.log("[BUNDLE] Select products opened");
  }
  function closePicker() { document.getElementById("pickBg").hidden = true; }
  function pickCount() {
    var n = Object.keys(pickSel).length;
    document.getElementById("pickN").textContent = n + "/" + MAX + " products selected";
    document.getElementById("pickAdd").disabled = n === 0;
    document.querySelectorAll("#pickList input[type=checkbox]").forEach(function (cb) {
      cb.disabled = !cb.checked && n >= MAX;                 // 5 ticked -> the others are locked
      cb.closest(".prow").classList.toggle("off", cb.disabled);
    });
  }
  function loadPick(q) {
    clearTimeout(timer);
    timer = setTimeout(async function () {
      var list = document.getElementById("pickList"); list.innerHTML = "";
      list.appendChild(el("div", "pempty", "Loading ..."));
      try {
        var r = await fetch("/bundle/products?q=" + encodeURIComponent(q)), d = await r.json();
        list.innerHTML = "";
        if (!d.items.length) { list.appendChild(el("div", "pempty", "No product found")); return; }
        d.items.forEach(function (p) {
          var row = el("label", "prow"), cb = el("input");
          cb.type = "checkbox"; cb.checked = !!pickSel[p.fi];
          cb.onchange = function () { if (cb.checked) pickSel[p.fi] = p; else delete pickSel[p.fi]; pickCount(); };
          row.appendChild(cb); row.appendChild(thumb(p.image));
          var t = el("div", "pname"); t.appendChild(el("span", "", p.name));
          t.appendChild(el("small", "", (p.sku || "no SKU") + (p.on_site ? "" : " · not on fragrantsouq.com")));
          row.appendChild(t);
          var av = el("div", "pavail" + (p.stock != null && p.stock <= 0 ? " zero" : ""),
                      (p.stock != null && p.stock <= 0 ? "⚠ " : "") + (p.stock != null ? p.stock : "-"));
          row.appendChild(av);
          row.appendChild(el("div", "pprice", p.price != null ? aed(p.price) : "-"));
          list.appendChild(row);
        });
        pickCount();
      } catch (e) { list.innerHTML = ""; list.appendChild(el("div", "pempty", "Could not load: " + e.message)); }
    }, q ? 250 : 0);
  }
  function pickDone() {                               // Add: ticked products become the bundle lines (order kept)
    var keep = {}; lines.forEach(function (l) { keep[l.p.fi] = l; });
    var next = [];
    lines.forEach(function (l) { if (pickSel[l.p.fi]) next.push(l); });          // still ticked: keep qty
    Object.keys(pickSel).forEach(function (fi) { if (!keep[fi]) next.push({p: pickSel[fi], qty: 1}); });
    lines = next.slice(0, MAX);
    console.log("[BUNDLE] products: " + lines.map(function (l) { return l.p.sku; }).join(", "));
    closePicker(); draw();
  }
  function setType(t) {
    dtype = t;
    document.getElementById("segAmt").className = t === "amount" ? "on" : "";
    document.getElementById("segPct").className = t === "percent" ? "on" : "";
    refresh();
  }
  function refresh() {
    var chosen = lines.filter(function (l) { return l.p; });
    var total = chosen.reduce(function (s, l) { return s + (l.p.price || 0) * l.qty; }, 0);
    var v = parseFloat(document.getElementById("dval").value || "0") || 0;
    var disc = dtype === "percent" ? total * v / 100 : v;
    document.getElementById("sTotal").textContent = aed(total);
    document.getElementById("sDisc").textContent = "- " + aed(disc);
    document.getElementById("sPrice").textContent = aed(Math.max(0, total - disc));
    var ti = document.getElementById("title");
    if (!titleTouched) ti.value = chosen.map(function (l) { return l.p.name; }).join(" + ");
    document.getElementById("sku").textContent = chosen.length ? chosen.map(function (l) { return (l.p.sku || "?").toUpperCase(); }).join(JOIN) : "-";
    var bad = chosen.some(function (l) { return !l.p.sku || l.p.price == null; });
    var okDisc = v >= 0 && (dtype === "amount" ? disc < total : v <= 100 && disc < total);
    document.getElementById("addBtn").disabled = chosen.length < MIN || bad || !okDisc || !ti.value.trim();
    var u = document.getElementById("until").value;                  // listing period text
    document.getElementById("untilInfo").textContent = u ? "Stock 0 from the day after " + u : "No end date (always active)";
    var m = document.getElementById("msg");
    if (!m.dataset.keep) m.textContent = chosen.length < MIN ? "Pick at least " + MIN + " products." :
        bad ? "A product has no SKU or no UAE price." : !okDisc ? "The discount must be smaller than the total." : "";
    m.className = "msg muted";
  }
  async function addBundle() {
    var btn = document.getElementById("addBtn"), m = document.getElementById("msg");
    var items = lines.filter(function (l) { return l.p; }).map(function (l) { return {fi: l.p.fi, qty: l.qty}; });
    btn.disabled = true; m.dataset.keep = "1"; m.className = "msg"; m.textContent = "Checking the stock in Shopify ...";
    try {
      var r = await fetch(EDIT ? "/bundle/update" : "/bundle/create", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({id: EDIT ? EDIT.id : null, items: items, title: document.getElementById("title").value,
                              discount_type: dtype, discount_value: document.getElementById("dval").value,
                              active_until: document.getElementById("until").value})});
      var d = await r.json().catch(function () { return {ok: false, error: "HTTP " + r.status}; });
      if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
      m.className = "msg ok"; m.textContent = (EDIT ? "Saved: " : "Bundle added: ") + d.title + " (SKU " + d.sku + ", " + aed(d.bundle_price) + "). ";
      var a = el("a", "", "See all bundles"); a.href = "/bundle"; a.style.color = "var(--gold)"; m.appendChild(a);
      console.log("[BUNDLE] " + (EDIT ? "saved" : "added") + " #" + d.id + " " + d.sku);
      if (EDIT) btn.disabled = false;                 // Edit: can save again
    } catch (e) {
      m.className = "msg err"; m.textContent = e.message; btn.disabled = false;
      console.log("[BUNDLE] not added: " + e.message);
    }
    setTimeout(function () { delete m.dataset.keep; }, 0);
  }
  if (EDIT) {                                           // Edit: fill the form with the saved bundle
    lines = EDIT.lines;
    document.getElementById("title").value = EDIT.title; titleTouched = EDIT.title_touched;
    document.getElementById("dval").value = EDIT.discount_value; setType(EDIT.discount_type);
    document.getElementById("until").value = EDIT.active_until || "";
    console.log("[BUNDLE] editing #" + EDIT.id);
  }
  draw();
</script>
{% endif %}
</div></body></html>
"""

LIST_HTML = PAGE_HEAD.replace("@@TITLE@@", "Bundles") + r"""
<style>
 .bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 .bar input{flex:1;min-width:220px}
 .bcard .top{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}
 .bcard h2{font-size:18px}
 .tag{display:inline-block;font-size:12px;border-radius:10px;padding:2px 9px;margin:6px 6px 0 0;border:1px solid var(--line);color:var(--muted)}
 .tag.on{border-color:#6cc28a;color:#6cc28a} .tag.off{border-color:#e0806c;color:#e0806c}
 .ed{border:1px solid var(--gold);color:var(--gold);border-radius:10px;padding:7px 14px;font-size:14px;text-decoration:none;margin-right:6px}
 .ed:hover{background:rgba(242,214,117,.1)}
 .items{margin:6px 0 0;padding:0;list-style:none;font-size:14px}
 .items li{padding:4px 0;border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:10px}
 .price{font-size:22px;font-weight:700;color:var(--yellow);text-align:right}
 .price small{display:block;font-size:13px;font-weight:400;color:var(--muted)}
 .sku{font-family:ui-monospace,Consolas,monospace;font-size:13px;color:var(--muted);word-break:break-all}
 .foot{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:10px;flex-wrap:wrap}
 .rm{background:none;border:1px solid #e0806c;color:#e0806c;border-radius:10px;padding:7px 14px;cursor:pointer;font-size:14px}
 .rm:hover{background:rgba(224,128,108,.12)} .rm:disabled{opacity:.5;cursor:default}
 .rmmsg{font-size:13px;margin-top:6px}
</style></head><body><div class="page">
<div class="topbar"><h1>Bundles</h1>
  {% if g.user %}<span class="who">Logged in as <b>{{ g.user }}</b> · <a href="/logout">Log out</a></span>{% endif %}</div>
{{ nav_html|safe }}
<form class="bar" method="get" style="margin-top:12px">
  <input type="text" name="q" value="{{ q }}" placeholder="Search bundles by title or SKU">
  <button class="btn" type="submit">Go</button>
  {% if admin %}<a class="ghost" href="/bundle/new" style="text-decoration:none">+ Create bundle</a>{% endif %}
</form>
<p class="muted" style="font-size:14px" id="bcount">{{ rows|length }} bundle{{ '' if rows|length == 1 else 's' }}{% if q %} matching "{{ q }}"{% endif %}.</p>
{% for b in rows %}
<div class="card bcard" id="bundle-{{ b.id }}">
  <div class="top">
    <div style="flex:1;min-width:0">
      <h2>{{ b.title }}</h2>
      <div class="sku">SKU {{ b.sku }}</div>
      {# listing period + bundle stock (0 after Active until) #}
      <span class="tag {{ 'off' if b.ended else 'on' }}">{% if b.ended %}Ended {{ b.active_until.strftime('%d %b %Y') }}{% elif b.active_until %}Active until {{ b.active_until.strftime('%d %b %Y') }}{% else %}Active · no end date{% endif %}</span>
      <span class="tag {{ 'off' if b.stock == 0 else '' }}">Stock {{ b.stock if b.stock is not none else '-' }}</span>
    </div>
    <div class="price">AED {{ '%.2f' % b.bundle_price }}
      <small>was AED {{ '%.2f' % b.total_price }} · discount {% if b.discount_type == 'percent' %}{{ '%g' % b.discount_value }}% = {% endif %}AED {{ '%.2f' % b.discount_aed }}</small></div>
  </div>
  <ul class="items">
    {% for it in b['items'] %}<li><span>{{ it.qty }} x {{ it.name }} <span class="sku">({{ it.sku }})</span></span>
      <span class="muted">AED {{ '%.2f' % (it.price * it.qty) }}</span></li>{% endfor %}
  </ul>
  <div class="foot">
    <p class="muted" style="font-size:13px;margin:0">Added by {{ b.created_by or '-' }} on {{ b.created_at.strftime('%d %b %Y, %H:%M') }}{% if b.updated_at %} · changed by {{ b.updated_by or '-' }} on {{ b.updated_at.strftime('%d %b %Y, %H:%M') }}{% endif %}</p>
    {# Edit (admins) -> /bundle/edit/<id> = the form filled in; Remove: confirm box -> POST /bundle/delete -> card disappears #}
    {% if admin %}<span><a class="ed" href="/bundle/edit/{{ b.id }}">Edit</a><button type="button" class="rm" data-id="{{ b.id }}" data-title="{{ b.title }}" onclick="removeBundle(this)">Remove</button></span>{% endif %}
  </div>
  <div class="rmmsg err" id="rmmsg-{{ b.id }}"></div>
</div>
{% else %}
<div class="card"><p class="sub" style="margin:0">No bundles yet.{% if admin %} <a href="/bundle/new" style="color:var(--gold)">Create the first one</a>.{% endif %}</p></div>
{% endfor %}
<script>
  // ---- Remove a bundle (admins): our database only (bundles are not in Shopify) ----
  async function removeBundle(btn) {
    var id = btn.dataset.id, msg = document.getElementById("rmmsg-" + id);
    if (!confirm("Remove this bundle?\n\n" + btn.dataset.title + "\n\nIt is deleted from our list (nothing changes in Shopify).")) return;
    btn.disabled = true; btn.textContent = "Removing ...";
    try {
      var r = await fetch("/bundle/delete", {method: "POST", headers: {"Content-Type": "application/json"},
                                             body: JSON.stringify({id: parseInt(id, 10)})});
      var d = await r.json().catch(function () { return {ok: false, error: "HTTP " + r.status}; });
      if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
      document.getElementById("bundle-" + id).remove();
      var c = document.getElementById("bcount"), n = document.querySelectorAll(".bcard").length;
      if (c) c.textContent = n + " bundle" + (n === 1 ? "" : "s") + ".";
      console.log("[BUNDLE] removed #" + id);
    } catch (e) {
      btn.disabled = false; btn.textContent = "Remove"; msg.textContent = "Not removed: " + e.message;
      console.log("[BUNDLE] remove #" + id + " failed: " + e.message);
    }
  }
</script>
</div></body></html>
"""


def _nav(cur_key):
    """Menu bar + its counts (the page still opens when the counts cannot be read)."""
    import competitors_page                                  # imported here = no import loop
    counts = {}
    try:
        conn = _hooks["connect"]()
        try:
            counts = competitors_page.tab_counts(conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor))
        finally:
            conn.close()
    except Exception as ex:
        log(f"[BUNDLE] menu counts not read: {ex}")
    return competitors_page.render_nav("bundle", cur_key, counts), competitors_page.NAV_CSS


@bundle_bp.get("/bundle/new")
def bundle_new():
    """Create bundle form."""
    import shopify_create as sc
    nav_html, nav_css = _nav("bundle-new")
    log(f"[BUNDLE] {g.get('user')} opened Create bundle form")
    return render_template_string(FORM_HTML, nav_html=nav_html, nav_css=nav_css, admin=is_admin(),
                                  min_items=MIN_ITEMS, max_items=MAX_ITEMS, max_qty=MAX_QTY, sku_join=SKU_JOIN,
                                  shop=sc.SHOP, edit=None, today=today_ae().isoformat())


@bundle_bp.get("/bundle/edit/<int:bid>")
def bundle_edit(bid):
    """Edit button on the Bundles list: the Create bundle form filled with bundle bid. Products show their
    CURRENT price / stock from our database (a product no longer in Item Master keeps its saved values)."""
    import shopify_create as sc
    edit = None
    if is_admin():
        conn = _hooks["connect"]()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(BUNDLES_CREATE)
            conn.commit()
            cur.execute("SELECT * FROM bundles WHERE id = %s", (bid,))
            b = cur.fetchone()
            if not b:
                return redirect("/bundle")
            saved = b["items"] or []
            now = {r["fi"]: r for r in product_rows(cur, " WHERE im.fi_record_id = ANY(%s)",
                                                    [[it.get("fi") for it in saved]], limit=MAX_ITEMS)}
        finally:
            conn.close()
        lines = []
        for it in saved:
            p = now.get(it.get("fi")) or {"fi": it.get("fi"), "sku": it.get("sku"), "name": it.get("name"),
                                          "price": it.get("price"), "image": it.get("image"), "stock": None}
            p = {k: (float(v) if k == "price" and v is not None else v) for k, v in p.items() if k != "shopify_id"}
            lines.append({"p": p, "qty": int(it.get("qty") or 1)})
        default_title = " + ".join(l["p"]["name"] or "" for l in lines)
        edit = {"id": b["id"], "lines": lines, "title": b["title"], "title_touched": b["title"] != default_title,
                "discount_type": b["discount_type"] or "amount", "discount_value": float(b["discount_value"] or 0),
                "active_until": b["active_until"].isoformat() if b.get("active_until") else ""}
    nav_html, nav_css = _nav("bundle-list")
    log(f"[BUNDLE] {g.get('user')} opened Edit #{bid}")
    return render_template_string(FORM_HTML, nav_html=nav_html, nav_css=nav_css, admin=is_admin(),
                                  min_items=MIN_ITEMS, max_items=MAX_ITEMS, max_qty=MAX_QTY, sku_join=SKU_JOIN,
                                  shop=sc.SHOP, edit=edit, today="")


@bundle_bp.get("/bundle")
def bundle_list():
    """List of all bundles (newest first), search by title / SKU."""
    q = (request.args.get("q") or "").strip()
    conn = _hooks["connect"]()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(BUNDLES_CREATE)
        conn.commit()
        if q:
            cur.execute("SELECT * FROM bundles WHERE title ILIKE %s OR sku ILIKE %s ORDER BY id DESC LIMIT 500",
                        (f"%{q}%", f"%{q}%"))
        else:
            cur.execute("SELECT * FROM bundles ORDER BY id DESC LIMIT 500")
        rows = cur.fetchall()
        fis = sorted({it.get("fi") for b in rows for it in (b["items"] or []) if it.get("fi")})
        stock_of = {}
        if fis:                                              # our stock of every product in the bundles
            cur.execute("SELECT fi_record_id, our_stock_qty FROM competitor_table WHERE fi_record_id = ANY(%s)", (fis,))
            stock_of = {r["fi_record_id"]: r["our_stock_qty"] for r in cur.fetchall()}
    finally:
        conn.close()
    today = today_ae()
    for b in rows:
        b["ended"] = bool(b.get("active_until") and today > b["active_until"])
        b["stock"] = bundle_stock(b["items"], stock_of, b.get("active_until"), today)
    for b in rows:                                           # numbers for the template
        for k in ("total_price", "discount_value", "discount_aed", "bundle_price"):
            b[k] = float(b[k] or 0)
    nav_html, nav_css = _nav("bundle-list")
    log(f"[BUNDLE] list: {len(rows)} bundles{f' for {q!r}' if q else ''}")
    return render_template_string(LIST_HTML, nav_html=nav_html, nav_css=nav_css, rows=rows, q=q, admin=is_admin())


@bundle_bp.post("/bundle/delete")
def bundle_delete():
    """Remove button on the Bundles list (admins). Body {"id": 12}. Deletes the row in table bundles (no Shopify)."""
    if not is_admin():
        return jsonify({"ok": False, "error": "admins only"}), 403
    try:
        bid = int((request.get_json(silent=True) or {}).get("id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "no bundle given"}), 400
    conn = _hooks["connect"]()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM bundles WHERE id = %s RETURNING title, sku", (bid,))
        row = cur.fetchone()
        conn.commit()
    except Exception as ex:
        conn.rollback()
        log(f"[BUNDLE] remove #{bid}: ERROR {ex}")
        return jsonify({"ok": False, "error": str(ex)[:200]}), 500
    finally:
        conn.close()
    if not row:
        return jsonify({"ok": False, "error": "bundle not found (already removed?)"}), 404
    log(f"[BUNDLE] {g.get('user')} removed #{bid} '{row[0]}' SKU {row[1]}")
    return jsonify({"ok": True, "id": bid})


# ============================================================ SECTION 7: hooks
def init(connect):
    """connect() -> new Postgres connection (app.pg_conn)."""
    _hooks["connect"] = connect
    try:
        conn = connect()
        try:
            conn.cursor().execute(BUNDLES_CREATE)
            conn.commit()
        finally:
            conn.close()
    except Exception as ex:
        log(f"[BUNDLE] table bundles not checked at start: {ex}")
    log("[BUNDLE] Linked to app.py: menu Bundle -> Bundles (/bundle) + Create bundle form (/bundle/new), table bundles")

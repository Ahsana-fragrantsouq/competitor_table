"""
"Create a new item"  (ADMIN ONLY - menu Item Master -> Create a new item -> /item-master/new)
=============================================================================================
Same fields as the Airtable form "New Perfume Entry", WITHOUT the prices, + Product type:
  Brand (required, pick one or "+ Add brand") · Perfume Name (required) · Size · Type · Category · Product type · Barcode
Saved straight into OUR database (table item_master) - NOT in Airtable.
Made automatically (like Airtable did), shown live on the form before Submit:
  * SKU          = the brand's SKU prefix + next free number      e.g. ASQ1047 is the highest ASQ -> ASQ1048
                   (prefix = the letters of that brand's existing SKUs; a NEW brand -> type its prefix)
  * Product Name = Brand + Perfume Name + Size + Type + Category + Product type
                   e.g. "Abdul Samad Al Qurashi Amber touch 30 ml EDP Unisex Spray"
The new row: id "new" + 14 letters/digits (Airtable ids start with "rec"), source = 'website'
-> "Load from Airtable now" never deletes or changes it. After saving, the Item Master shows it and
"Create product description" / "Create in website" work for it like for every other product.

Where things are (change them here):
  SECTION 1  settings      -> SELECT_COLS (dropdown boxes), FIRST_NUMBER (first SKU number of a new prefix)
  SECTION 2  page HTML     -> NEW_HTML (form + live SKU / product name preview)
  SECTION 3  database      -> brand_skus(), choices(), next_sku(), product_name(), save_item()
  SECTION 4  routes        -> GET / POST /item-master/new
  SECTION 5  hooks         -> init(connect)
"""

import re
import secrets
import string

import psycopg2.extras
from flask import Blueprint, request, render_template_string, redirect, g

import competitors_page                     # menu bar (render_nav, NAV_CSS) + menu counts
import item_master                          # IM_CREATE (table + columns)
from auth import is_admin

new_item_bp = Blueprint("item_create", __name__)


def log(*a):
    """Print to the Render log (lines start with [NEW-ITEM])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
# dropdown boxes: (item_master column, label). Their choices = the values already used in Item Master.
SELECT_COLS = [("size", "Size"), ("type", "Type"), ("category", "Category"), ("product_type", "Product type")]
FIRST_NUMBER = 1001                          # first SKU number for a brand-new prefix (ACF1007, ATR1002 ...)
SOURCE_SQL = "ALTER TABLE item_master ADD COLUMN IF NOT EXISTS source TEXT;"   # 'website' = made by this form
SKU_RE = re.compile(r"^([A-Z]+)(\d+)$")      # "ASQ1047" -> prefix "ASQ", number 1047


# ============================================================ SECTION 2: page HTML
NEW_HTML = """<!doctype html><html><head><title>Create a new item - Item Master</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#1c1b19;--card:#252420;--line:#3a3833;--text:#f2efe9;--muted:#a9a397;--gold:#d8b76e}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--text);font-family:Roboto,-apple-system,Segoe UI,Arial,sans-serif}
 .page{max-width:920px;margin:0 auto;padding:22px 18px 40px}
 h1{font-size:26px;margin:4px 0 18px}
 .topbar{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
 .who{color:var(--muted);font-size:14px} .who b{color:var(--text)} .who a{color:var(--gold)}
 {{ nav_css|safe }}
 .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px 22px;margin-top:14px}
 .card h2{font-size:24px;margin:0 0 4px}
 .sub{color:var(--muted);font-size:14px;margin:0 0 16px;padding-bottom:14px;border-bottom:1px solid var(--line)}
 .err{background:rgba(224,128,108,.12);border:1px solid #e0806c;color:#e0806c;border-radius:12px;padding:12px 14px;margin:0 0 14px}
 .err ul{margin:4px 0 0 18px;padding:0}
 .f{margin-bottom:14px}
 label{display:flex;justify-content:space-between;font-weight:600;font-size:14px;margin-bottom:6px}
 label .req{color:#e0806c}
 input,select{width:100%;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:10px;
              padding:10px 12px;font-size:15px;font-family:inherit}
 input:focus,select:focus{outline:2px solid var(--gold);outline-offset:1px}
 .addbrand{background:none;border:1px solid var(--line);color:var(--text);border-radius:8px;padding:6px 12px;
           font-size:13px;cursor:pointer;margin-top:8px}
 .newbrand{display:grid;grid-template-columns:2fr 1fr;gap:10px;margin-top:8px}
 .newbrand[hidden]{display:none}
 .small{color:var(--muted);font-size:12px;margin-top:4px}
 .preview{background:var(--bg);border:1px solid #f2d675;border-radius:12px;padding:12px 14px;margin:6px 0 16px}
 .preview div{margin:2px 0} .preview .lbl{color:var(--muted);font-size:13px;display:inline-block;width:110px}
 .preview b{color:#f2d675}
 .actions{display:flex;justify-content:space-between;align-items:center;border-top:1px solid var(--line);padding-top:14px}
 .clear{background:none;border:0;color:var(--gold);font-size:14px;cursor:pointer}
 .save{background:#f2d675;color:#1c1b19;border:0;border-radius:10px;padding:11px 22px;font-size:15px;font-weight:700;cursor:pointer}
 .noadmin{background:var(--card);border:1px dashed var(--line);border-radius:16px;padding:28px 20px;text-align:center;color:var(--muted)}
</style></head><body><div class="page">
<div class="topbar"><h1>Create a new item</h1>
  {% if g.user %}<span class="who">Logged in as <b>{{ g.user }}</b> · <a href="/logout">Log out</a></span>{% endif %}</div>
{{ nav_html|safe }}
{% if not admin %}
<div class="noadmin">Only admins can create new items.</div>
{% else %}
<form class="card" id="newform" method="post" action="/item-master/new" autocomplete="off">
  <h2>New Perfume Entry</h2>
  <div class="sub">For adding a new perfume to the Item Master (our database - not Airtable).</div>
  {% if errors %}<div class="err">Not saved:<ul>{% for e in errors %}<li>{{ e }}</li>{% endfor %}</ul></div>{% endif %}

  <div class="f"><label for="f-brand">Brand <span class="req">*</span></label>
    <select id="f-brand" name="brand">
      <option value="">- choose a brand -</option>
      {% for b in brands %}<option value="{{ b }}" {% if v.brand == b %}selected{% endif %}>{{ b }}</option>{% endfor %}
    </select>
    <button type="button" class="addbrand" id="addbrand">+ Add brand</button>
    <div class="newbrand" id="newbrand" {% if not v.new_brand %}hidden{% endif %}>
      <input name="new_brand" id="f-newbrand" placeholder="New brand name" value="{{ v.new_brand or '' }}">
      <input name="new_prefix" id="f-newprefix" placeholder="SKU prefix e.g. ABC" value="{{ v.new_prefix or '' }}" maxlength="6">
    </div>
    <div class="small" id="newbrandhint" {% if not v.new_brand %}hidden{% endif %}>New brand: its SKUs will start with the prefix you type (letters only).</div>
  </div>

  <div class="f"><label for="f-perfume">Perfume Name <span class="req">*</span></label>
    <input id="f-perfume" name="perfume_name" value="{{ v.perfume_name or '' }}"></div>

  {% for col, label in selects %}
  <div class="f"><label for="f-{{ col }}">{{ label }}</label>
    <select id="f-{{ col }}" name="{{ col }}">
      <option value=""></option>
      {% for c in choices[col] %}<option value="{{ c }}" {% if v[col] == c %}selected{% endif %}>{{ c }}</option>{% endfor %}
    </select></div>
  {% endfor %}

  <div class="f"><label for="f-barcode">Barcode</label>
    <input id="f-barcode" name="barcode" inputmode="numeric" placeholder="8-14 digits" value="{{ v.barcode or '' }}"></div>

  <div class="preview">
    <div><span class="lbl">SKU</span><b id="pv-sku">-</b></div>
    <div><span class="lbl">Product name</span><b id="pv-name">-</b></div>
  </div>

  <div class="actions">
    <button type="button" class="clear" id="clearform">&#8634; Clear form</button>
    <button type="submit" class="save">Submit</button>
  </div>
</form>
<script>
  // ---- Create a new item: live SKU + product name preview (the server makes the real ones on Submit) ----
  var SKUS = {{ sku_info|tojson }};          // brand -> [prefix, next number]
  var ORDER = ["perfume_name", "size", "type", "category", "product_type"];
  function el(id) { return document.getElementById(id); }
  function brandNow() { return el("newbrand").hidden ? el("f-brand").value : el("f-newbrand").value.trim(); }
  function preview() {
    var brand = brandNow(), sku = "-";
    if (!el("newbrand").hidden) {
      var p = el("f-newprefix").value.toUpperCase().replace(/[^A-Z]/g, "");
      sku = p ? p + "{{ first_number }}" : "(type the SKU prefix)";
    } else if (brand) {
      sku = SKUS[brand] ? SKUS[brand][0] + SKUS[brand][1] : "(this brand has no SKU yet - use + Add brand to give a prefix)";
    }
    var parts = [brand].concat(ORDER.map(function (k) { var x = el(k === "perfume_name" ? "f-perfume" : "f-" + k); return x.value.trim(); }));
    el("pv-sku").textContent = sku;
    el("pv-name").textContent = parts.filter(Boolean).join(" ") || "-";
  }
  el("addbrand").addEventListener("click", function () {
    var nb = el("newbrand"); nb.hidden = !nb.hidden; el("newbrandhint").hidden = nb.hidden;
    el("f-brand").disabled = !nb.hidden;
    el("addbrand").textContent = nb.hidden ? "+ Add brand" : "Choose an existing brand instead";
    if (!nb.hidden) el("f-newbrand").focus(); else { el("f-newbrand").value = ""; el("f-newprefix").value = ""; }
    console.log("[NEW-ITEM] " + (nb.hidden ? "existing brand" : "adding a new brand"));
    preview();
  });
  el("clearform").addEventListener("click", function () {
    if (!confirm("Clear the whole form?")) return;
    el("newform").querySelectorAll("input").forEach(function (i) { i.value = ""; });
    el("newform").querySelectorAll("select").forEach(function (s) { s.selectedIndex = 0; });
    if (!el("newbrand").hidden) el("addbrand").click();
    console.log("[NEW-ITEM] form cleared");
    preview();
  });
  el("newform").addEventListener("input", preview);
  el("newform").addEventListener("change", preview);
  if (!el("newbrand").hidden) { el("f-brand").disabled = true; el("addbrand").textContent = "Choose an existing brand instead"; }
  preview();
</script>
{% endif %}
</div></body></html>"""


# ============================================================ SECTION 3: database
_hooks = {}
_ready = False


def _conn():
    """Database connection; item_master + its columns (+ source) exist (once after each start)."""
    global _ready
    conn = _hooks["connect"]()
    if not _ready:
        cur = conn.cursor()
        cur.execute(item_master.IM_CREATE)
        cur.execute(SOURCE_SQL)
        conn.commit()
        _ready = True
    return conn


def brand_skus(cur):
    """{brand: [prefix, next free number]} from the SKUs already in Item Master.
    prefix = the letters most used in that brand's SKUs; next = highest number with that prefix (any brand) + 1."""
    cur.execute("SELECT brand, UPPER(TRIM(sku)) FROM item_master WHERE COALESCE(brand, '') <> '' AND sku IS NOT NULL")
    per_brand, highest = {}, {}
    for brand, sku in cur.fetchall():
        m = SKU_RE.match(sku or "")
        if not m:
            continue
        p, n = m.group(1), int(m.group(2))
        per_brand.setdefault(brand, {}).setdefault(p, 0)
        per_brand[brand][p] += 1
        highest[p] = max(highest.get(p, 0), n)
    return {b: [max(ps, key=ps.get), highest[max(ps, key=ps.get)] + 1] for b, ps in per_brand.items()}


def next_for_prefix(cur, prefix):
    """Next free number for a SKU prefix (FIRST_NUMBER when the prefix is new)."""
    cur.execute("SELECT UPPER(TRIM(sku)) FROM item_master WHERE UPPER(TRIM(sku)) LIKE %s", (prefix + "%",))
    nums = [int(m.group(2)) for (s,) in cur.fetchall() if (m := SKU_RE.match(s or "")) and m.group(1) == prefix]
    return max(nums) + 1 if nums else FIRST_NUMBER


def choices(cur):
    """Brands + values already used for each dropdown box (like Airtable's single-select options)."""
    out = {}
    for col in ["brand"] + [c for c, _ in SELECT_COLS]:
        cur.execute(f"SELECT DISTINCT {col} FROM item_master WHERE COALESCE({col}, '') <> '' ORDER BY {col}")
        out[col] = [r[0] for r in cur.fetchall()]
    return out


def product_name(v):
    """Brand + Perfume Name + Size + Type + Category + Product type (empty ones left out)."""
    return " ".join(x for x in [v.get("brand"), v.get("perfume_name"), v.get("size"), v.get("type"),
                                v.get("category"), v.get("product_type")] if x)


def new_id():
    """Id for a product made here: 'new' + 14 letters/digits (17 long, like Airtable's 'rec...' ids)."""
    return "new" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(14))


def clean_form(form, opts, sku_info):
    """Form -> (values, problems). Brand: an existing one, or a new one + its SKU prefix."""
    v = {k: (form.get(k) or "").strip() for k in
         ["brand", "new_brand", "new_prefix", "perfume_name", "barcode"] + [c for c, _ in SELECT_COLS]}
    errors = []
    if v["new_brand"]:
        v["new_prefix"] = re.sub(r"[^A-Z]", "", v["new_prefix"].upper())
        if not v["new_prefix"]:
            errors.append("New brand: type its SKU prefix (letters, e.g. ABC)")
        same = next((b for b in opts["brand"] if b.lower() == v["new_brand"].lower()), None)
        if same and same in sku_info:                   # existing brand WITH a SKU prefix -> pick it from the list
            errors.append(f"Brand '{same}' already exists - choose it from the list")
        elif same:                                      # existing brand without SKU yet -> keep its spelling
            v["new_brand"] = same
        v["brand"] = v["new_brand"]
    elif not v["brand"]:
        errors.append("Brand is required")
    if not v["perfume_name"]:
        errors.append("Perfume Name is required")
    if v["barcode"] and not re.fullmatch(r"\d{8,14}", v["barcode"]):
        errors.append("Barcode must be 8 to 14 digits")
    for col, label in SELECT_COLS:                       # only the offered choices (like an Airtable single select)
        if v[col] and v[col] not in opts[col]:
            errors.append(f"{label}: '{v[col]}' is not one of the choices")
    return v, errors


def save_item(v, sku_info, user):
    """Make SKU + product name, check SKU / barcode not used yet, insert. Returns (sku, None) or (None, problem)."""
    conn = _conn()
    try:
        cur = conn.cursor()
        if v["new_brand"]:
            prefix = v["new_prefix"]
        elif v["brand"] in sku_info:
            prefix = sku_info[v["brand"]][0]
        else:
            return None, (f"Brand '{v['brand']}' has no SKU yet - click '+ Add brand', type '{v['brand']}' "
                          f"and its SKU prefix")
        sku = f"{prefix}{next_for_prefix(cur, prefix)}"             # read again now: never a used number
        if v["barcode"]:
            cur.execute("SELECT sku, product_name FROM item_master WHERE barcode = %s LIMIT 1", (v["barcode"],))
            same = cur.fetchone()
            if same:
                return None, f"Barcode {v['barcode']} is already used by {same[0]} '{same[1]}'"
        name = product_name(v)
        fid = new_id()
        cur.execute("""INSERT INTO item_master (fi_record_id, source, loaded_at, sku, brand, perfume_name, size, type,
                                                category, product_type, barcode, product_name)
                       VALUES (%s, 'website', NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (fid, sku, v["brand"], v["perfume_name"], v["size"] or None, v["type"] or None,
                     v["category"] or None, v["product_type"] or None, v["barcode"] or None, name))
        conn.commit()
        log(f"[NEW-ITEM] {user} created {sku} '{name}' -> item_master {fid}")
        return sku, None
    finally:
        conn.close()


# ============================================================ SECTION 4: routes
def _page(v=None, errors=None, status=200):
    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        counts = competitors_page.tab_counts(cur)
        plain = conn.cursor()
        opts = choices(plain) if is_admin() else {}
        sku_info = brand_skus(plain) if is_admin() else {}
    finally:
        conn.close()
    return render_template_string(NEW_HTML, nav_html=competitors_page.render_nav("item", "im-new", counts),
                                  nav_css=competitors_page.NAV_CSS, admin=is_admin(), v=v or {}, errors=errors or [],
                                  brands=opts.get("brand", []), choices=opts, selects=SELECT_COLS,
                                  sku_info=sku_info, first_number=FIRST_NUMBER), status


@new_item_bp.route("/item-master/new", methods=["GET", "POST"])
def new_item():
    """GET = empty form. POST = check + save -> Item Master showing the new item."""
    if request.method == "GET":
        log(f"[NEW-ITEM] Form opened by {g.get('user')}")
        return _page()
    if not is_admin():
        log(f"[NEW-ITEM] Refused: {g.get('user')} is not an admin")
        return _page(errors=["Only admins can create new items"], status=403)
    conn = _conn()
    try:
        cur = conn.cursor()
        opts, sku_info = choices(cur), brand_skus(cur)
    finally:
        conn.close()
    v, errors = clean_form(request.form, opts, sku_info)
    if not errors:
        sku, problem = save_item(v, sku_info, g.get("user"))
        if problem:
            errors.append(problem)
    if errors:
        log(f"[NEW-ITEM] Not saved for {g.get('user')}: {errors}")
        return _page(v, errors, status=400)
    return redirect(f"/item-master?q={sku}&msg=New+item+{sku}+created.")


# ============================================================ SECTION 5: hooks (called once from app.py)
def init(connect):
    """connect() -> new Postgres connection (app.pg_conn)."""
    _hooks["connect"] = connect
    log("[NEW-ITEM] Linked to app.py: Item Master -> Create a new item (/item-master/new, saved in item_master)")

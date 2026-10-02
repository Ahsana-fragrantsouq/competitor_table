"""
Competitors page - tabs like the extractor "Saved data" screen.
Plugged into app.py as a Blueprint -> open /competitors

Tabs come from SOURCES below. To add a new competitor later:
  1. load its products into a new table in french_fragrance_db
  2. add ONE entry to SOURCES (key, label, table, column mapping)

Env var:
  FF_DATABASE_URL = Postgres URL ending in /french_fragrance_db
"""

import os
import math
import psycopg2
import psycopg2.extras
from urllib.parse import urlencode
from flask import Blueprint, request, render_template_string

competitors_bp = Blueprint("competitors", __name__)

PER_PAGE = 50

# ---------------------------------------------------------------- tabs
# Each "cols" entry is the SQL used for that field (None = column not available for this source)
SOURCES = [
    # Competitor table: one row per French Inventories product + Samawa match (filled by migrate_competitor_table.py)
    {"key": "competitor", "label": "Competitor table", "table": "competitor_table", "kind": "competitor",
     "search": ["product_name", "french_inventory_code", "sku", "brand", "barcode"],
     # shops shown on each card: (label, column prefix in competitor_table). Add V Perfumes here later.
     "shops": [("Samawa", "samawa"), ("French Fragrance", "ff"), ("Branded Perfume", "bp"), ("Essenzi", "es"),
               ("V Perfumes", "vp")],
     "cols": {"code": "french_inventory_code", "sku": "sku", "brand": "brand", "barcode": "barcode", "name": "product_name",
              "uae_price": "uae_price",
              "samawa_url": "samawa_link", "samawa_price": "samawa_price", "samawa_stock": "samawa_stock",
              "samawa_suggestion": "samawa_suggestion",
              "ff_url": "ff_link", "ff_price": "ff_price", "ff_stock": "ff_stock", "ff_suggestion": "ff_suggestion",
              # Branded Perfume / Essenzi come through the French Fragrance match -> no suggestion column
              "bp_url": "bp_link", "bp_price": "bp_price", "bp_stock": "bp_stock", "bp_suggestion": None,
              "es_url": "es_link", "es_price": "es_price", "es_stock": "es_stock", "es_suggestion": None,
              "vp_url": "vp_link", "vp_price": "vp_price", "vp_stock": "vp_stock", "vp_suggestion": "vp_suggestion",
              # competitor's own product name, used as the link text
              "samawa_title": "samawa_title", "ff_title": "ff_title", "bp_title": "bp_title",
              "es_title": "es_title", "vp_title": "vp_title",
              "least_price": "least_price", "least_site": "least_priced_website",
              "suggested_price": "suggested_price", "fi": "fi_record_id",
              "stock": None, "updated": "updated_at"}},
    {"key": "samawa", "label": "Samawa", "table": "samawa_catalog",
     "cols": {"name": "name", "brand": "brand", "gtin": "gtin", "url": "product_url",
              "price": "price", "stock": "stock", "volume": "volume", "updated": "updated_at"}},
    {"key": "ff", "label": "French Fragrance", "table": "french_fragrance_catalog",
     "cols": {"name": "name", "brand": None, "gtin": "gtin", "url": "product_url",
              "price": "COALESCE(price_inc_tax, price)", "stock": "stock", "volume": "volume",
              "updated": "updated_at"}},
    # Branded Perfume = same products as French Fragrance, prices/stock read from brandedperfume.com
    {"key": "bp", "label": "Branded Perfume", "table": "branded_perfume_catalog",
     "cols": {"name": "name", "brand": None, "gtin": "gtin", "url": "bp_url",
              "price": "COALESCE(bp_price_inc_tax, bp_price)", "stock": "bp_stock",
              "volume": "COALESCE(bp_size, volume)",
              "updated": "checked_at"}},
    # Essenzi = same products as French Fragrance, prices/stock read from essenzi.com (es_scraper.py on the PC)
    {"key": "es", "label": "Essenzi", "table": "essenzi_catalog",
     "cols": {"name": "name", "brand": None, "gtin": "gtin", "url": "es_url",
              "price": "COALESCE(es_price_inc_tax, es_price)", "stock": "es_stock",
              "volume": "COALESCE(es_size, volume)", "updated": "checked_at"}},
    # V Perfumes = perfumes from the vperfumes.com UAE "Perfumes" category (no gift sets)
    {"key": "vp", "label": "V Perfumes", "table": "vperfumes_catalog",
     "cols": {"name": "name", "brand": None, "gtin": "gtin", "url": "product_url",
              "price": "COALESCE(price_inc_tax, price)", "stock": "stock", "volume": "volume",
              "updated": "updated_at"}},
    # {"key": "shop3", "label": "Shop 3", "table": "shop3_catalog", "cols": {...}},   # add more here
]
SOURCE_BY_KEY = {s["key"]: s for s in SOURCES}

# Price filters for the Competitor table tab. Only IN-STOCK competitor prices count:
#   suggested_price is filled only when at least one competitor has the product in stock,
#   least_priced_website lists everyone with the lowest price (ours + in-stock competitors).
PRICE_FILTERS = {
    # we are the cheapest (ties with a competitor count as low)
    "low": ("Low price (we're cheapest)",
            "suggested_price IS NOT NULL AND uae_price > 0 AND least_priced_website LIKE '%%Fragrant Souq%%'"),
    # an in-stock competitor is cheaper than us
    "high": ("High price (competitor cheaper)",
             "suggested_price IS NOT NULL AND uae_price > 0 AND least_priced_website NOT LIKE '%%Fragrant Souq%%'"),
    # no competitor has it in stock
    "none": ("No competitor price", "suggested_price IS NULL"),
}

# Airtable-style filter (Competitor table tab): "Where <field> <operator> <value>", several rows = AND.
# Only the fields / operators below are allowed (safe SQL, values always passed as parameters).
FILTER_FIELDS = {
    "price":     {"label": "Price status (High / Low / No competitor)", "type": "choice",
                  "choices": {k: v[0] for k, v in PRICE_FILTERS.items()}},
    "brand":     {"label": "Brand", "col": "brand", "type": "multi"},      # pick several brands (chips)
    "name":      {"label": "Product name", "col": "product_name", "type": "text"},
    "code":      {"label": "Item ID", "col": "french_inventory_code", "type": "text"},
    "sku":       {"label": "SKU", "col": "sku", "type": "text"},
    "barcode":   {"label": "Barcode", "col": "barcode", "type": "text"},
    "uae_price": {"label": "Our UAE price", "col": "uae_price", "type": "number"},
    "least":     {"label": "Least price", "col": "least_price", "type": "number"},
    "suggested": {"label": "Suggested price", "col": "suggested_price", "type": "number"},
    "site":      {"label": "Least priced website", "col": "least_priced_website", "type": "text"},
}
FILTER_OPS = {
    "text":   [("is", "is"), ("isnot", "is not"), ("contains", "contains"), ("notcontains", "does not contain"),
               ("empty", "is empty"), ("notempty", "is not empty")],
    "number": [("eq", "="), ("ne", "≠"), ("gt", ">"), ("lt", "<"), ("ge", "≥"), ("le", "≤"),
               ("empty", "is empty"), ("notempty", "is not empty")],
    "choice": [("is", "is"), ("isnot", "is not")],
    # brand: like Airtable "has any of..." with chips; several brands are sent as "Armaf||Afnan"
    "multi":  [("anyof", "has any of..."), ("noneof", "has none of..."), ("contains", "contains..."),
               ("notcontains", "does not contain..."), ("empty", "is empty"), ("notempty", "is not empty")],
}
NUM_SQL = {"eq": "=", "ne": "<>", "gt": ">", "lt": "<", "ge": ">=", "le": "<="}


def read_filters(args):
    """URL ?ff=brand&fo=is&fv=Armaf&ff=price&fo=is&fv=high ... -> list of valid (field, op, value)."""
    out = []
    for f, o, v in zip(args.getlist("ff"), args.getlist("fo"), args.getlist("fv")):
        fd = FILTER_FIELDS.get(f)
        if not fd or o not in dict(FILTER_OPS[fd["type"]]):
            continue
        v = (v or "").strip()
        if o not in ("empty", "notempty") and v == "":
            continue                                   # condition without a value -> ignored
        out.append((f, o, v))
    return out


def filter_sql(conds):
    """(field, op, value) list -> (list of SQL pieces, params). Unknown values are skipped."""
    where, params = [], []
    for f, o, v in conds:
        fd = FILTER_FIELDS[f]
        if fd["type"] == "choice":                     # Price status: Low / High / No competitor price
            if v not in PRICE_FILTERS:
                continue
            cond = PRICE_FILTERS[v][1]
            where.append(f"({cond})" if o == "is" else f"NOT ({cond})")
            continue
        col = fd["col"]
        if o == "empty":
            where.append(f"({col} IS NULL OR {col}::text = '')")
        elif o == "notempty":
            where.append(f"({col} IS NOT NULL AND {col}::text <> '')")
        elif o in ("anyof", "noneof"):
            picked = [x.strip().lower() for x in v.split("||") if x.strip()]   # chips -> list of brands
            if not picked:
                continue
            if o == "anyof":
                where.append(f"LOWER({col}) = ANY(%s)")
            else:
                where.append(f"({col} IS NULL OR NOT (LOWER({col}) = ANY(%s)))")
            params.append(picked)
        elif fd["type"] == "number":
            try:
                params.append(float(v))
            except ValueError:
                continue
            where.append(f"{col} {NUM_SQL[o]} %s")
        elif o == "is":
            where.append(f"LOWER({col}) = LOWER(%s)"); params.append(v)
        elif o == "isnot":
            where.append(f"({col} IS NULL OR LOWER({col}) <> LOWER(%s))"); params.append(v)
        elif o == "contains":
            where.append(f"{col} ILIKE %s"); params.append(f"%{v}%")
        elif o == "notcontains":
            where.append(f"({col} IS NULL OR {col} NOT ILIKE %s)"); params.append(f"%{v}%")
    return where, params


def db_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    print("[competitors] Opening connection to french_fragrance_db", flush=True)
    return psycopg2.connect(url, sslmode="require")


def table_exists(cur, table):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (table,))
    return cur.fetchone()["ok"]


def tab_counts(cur):
    """Row count for every tab (0 when the table is not created yet)."""
    counts = {}
    for s in SOURCES:
        if s["table"] and table_exists(cur, s["table"]):
            cur.execute(f"SELECT COUNT(*) AS c FROM {s['table']}")
            counts[s["key"]] = cur.fetchone()["c"]
        else:
            counts[s["key"]] = 0
    print(f"[competitors] Tab counts: {counts}", flush=True)
    return counts


HTML = """
<!doctype html><html><head><title>{{ src.label }} - Competitors</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#1c1b19;--card:#252420;--line:#3a3833;--text:#f2efe9;--muted:#a9a397;
       --gold:#d8b76e;--goldbtn:#7c6a45;--green:#8cc58c;--red:#e0806c;--grey:#8b877f}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--text);font-family:Roboto,-apple-system,Segoe UI,Arial,sans-serif}
 .page{max-width:760px;margin:0 auto;padding:22px 18px 40px}
 h1{font-size:26px;margin:4px 0 18px}
 .tabs{display:flex;gap:4px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:5px;
       overflow-x:auto;scrollbar-width:none}
 .tabs::-webkit-scrollbar{display:none}
 .tabwrap{display:flex;align-items:center;gap:6px}
 .tabwrap .tabs{flex:1;min-width:0;scroll-behavior:smooth}
 .arrow{flex:0 0 auto;width:40px;height:40px;border-radius:50%;border:1px solid var(--line);background:var(--card);
        color:var(--text);font-size:26px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center}
 .arrow:hover{background:#f2d675;color:#1c1b19}
 .arrow.hide{visibility:hidden}
 .tab{flex:0 0 auto;padding:11px 16px;border-radius:12px;color:var(--muted);text-decoration:none;white-space:nowrap;font-size:16px}
 .tab.on{background:var(--bg);color:var(--text);font-weight:600}
 .bar{display:flex;gap:8px;margin:14px 0 6px;flex-wrap:wrap}
 .bar select{max-width:100%}
 .clear{align-self:center;color:var(--gold);padding:0 6px}
 .fbtn{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:12px;padding:0 16px;
       font-size:16px;cursor:pointer;min-height:48px}
 .fquick{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
 .fquick a{border:1px solid var(--line);border-radius:999px;padding:7px 14px;color:var(--text);text-decoration:none;font-size:14px}
 .fquick a:hover{background:#f2d675;color:#1c1b19;border-color:#f2d675}
 .fbtn.on{background:#e3f5e1;color:#1c5a24;border-color:#b9e3b4;font-weight:600}
 .fpanel{flex-basis:100%;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-top:6px}
 .ftitle{color:var(--muted);font-size:14px;margin-bottom:10px}
 .frow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
 .fwhere{width:52px;color:var(--muted);font-size:14px}
 .frow select,.frow input{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:10px;
       padding:10px 12px;font-size:15px}
 .fval input,.fval select{min-width:180px}
 .chips{display:inline-flex;flex-wrap:wrap;gap:6px;align-items:center;background:var(--bg);border:1px solid var(--line);
        border-radius:10px;padding:6px 8px;min-width:220px}
 .chip{background:#e3f5e1;color:#1c5a24;border-radius:999px;padding:4px 10px;font-size:14px}
 .chip b{cursor:pointer;margin-left:2px}
 .chip-add{border:0 !important;background:transparent !important;padding:4px !important;min-width:120px !important;outline:none}
 .fdel{background:none;border:0;color:var(--muted);font-size:18px;cursor:pointer}
 .fadd{background:none;border:0;color:var(--gold);font-size:15px;cursor:pointer;padding:6px 0}
 .factions{display:flex;justify-content:flex-end;gap:10px;margin-top:8px}
 .fcancel{background:none;border:0;color:var(--text);font-size:15px;cursor:pointer}
 .fapply{background:#f2d675;color:#1c1b19;border:0;border-radius:10px;padding:10px 18px;font-weight:600;cursor:pointer}
 .bar input,.bar select{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:12px;
       padding:13px 14px;font-size:16px}
 .bar input{flex:1;min-width:0}
 .bar > button[type=submit]{background:#f2d675;color:#1c1b19;border:0;border-radius:12px;padding:0 18px;font-size:16px;font-weight:600;cursor:pointer}
 .prices{display:flex;gap:22px;flex-wrap:wrap;margin-top:8px}
 .plabel{color:var(--muted);font-size:13px}
 .diff{font-size:14px;margin-top:8px}
 .code{color:var(--muted);font-size:14px;font-family:Consolas,monospace}
 .shop{border-top:1px solid var(--line);margin-top:12px;padding-top:10px}
 .sugg{color:#f2d675}
 .plink{text-align:right;max-width:65%}
 .meta{color:var(--muted);font-size:14px;margin-top:4px}
 .meta b{color:var(--text);font-weight:600}
 .upd{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:2px}
 .sugg-in{width:110px;background:var(--card);color:#f2d675;border:1px solid var(--line);border-radius:10px;
          padding:8px 10px;font-size:20px;font-weight:600}
 .upd-btn{background:#f2d675;color:#1c1b19;border:0;border-radius:10px;padding:10px 14px;font-weight:600;cursor:pointer}
 .upd-btn:disabled{opacity:.6;cursor:wait}
 .upd-msg{font-size:13px;margin-top:6px;min-height:16px}
 .sitelink{color:inherit;text-decoration:underline;text-underline-offset:3px}
 .sitelink:hover{color:#f2d675}
 .least{display:flex;flex-wrap:wrap;align-items:flex-end;gap:10px 28px;margin:10px 0 2px;padding:10px 12px;
        border:1px solid #f2d675;border-radius:12px}
 .hint{color:var(--muted);font-size:14px;margin:6px 2px 16px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px;margin-bottom:12px}
 .title{font-size:18px;font-weight:600;margin-bottom:6px;line-height:1.35}
 .sub{color:var(--muted);font-size:15px;margin-bottom:10px}
 .row{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-top:8px}
 .price{font-size:20px;font-weight:600}
 .gtin{color:var(--muted);font-size:14px}
 a.link{color:var(--gold)}
 .in{color:var(--green)} .out{color:var(--red)} .gone{color:var(--grey)}
 .date{color:var(--grey);font-size:13px;margin-top:8px}
 .empty{background:var(--card);border:1px dashed var(--line);border-radius:16px;padding:28px 20px;text-align:center;color:var(--muted)}
 .pager{display:flex;justify-content:space-between;align-items:center;margin-top:16px}
 .pbtn{background:#f2d675;color:#1c1b19;text-decoration:none;border-radius:12px;padding:12px 18px;font-weight:600}
 .pbtn.off{visibility:hidden}
 :focus-visible{outline:2px solid var(--gold);outline-offset:2px}
</style></head><body><div class="page">

<h1>Saved data</h1>

<div class="tabwrap">
  <button type="button" class="arrow left" id="tabLeft" aria-label="Scroll tabs left">&#8249;</button>
  <nav class="tabs" id="tabs">
    {% for s in sources %}
    <a class="tab {% if s.key == src.key %}on{% endif %}" href="?tab={{ s.key }}">{{ s.label }} {{ counts[s.key] }}</a>
    {% endfor %}
  </nav>
  <button type="button" class="arrow right" id="tabRight" aria-label="Scroll tabs right">&#8250;</button>
</div>
<script>
  // Tab bar arrows: scroll the tabs left/right, hide an arrow when there is nothing more on that side
  (function () {
    var tabs = document.getElementById("tabs");
    var left = document.getElementById("tabLeft"), right = document.getElementById("tabRight");
    function update() {
      left.classList.toggle("hide", tabs.scrollLeft <= 2);
      right.classList.toggle("hide", tabs.scrollLeft + tabs.clientWidth >= tabs.scrollWidth - 2);
    }
    left.onclick = function () { tabs.scrollBy({left: -220, behavior: "smooth"}); };
    right.onclick = function () { tabs.scrollBy({left: 220, behavior: "smooth"}); };
    tabs.addEventListener("scroll", update);
    window.addEventListener("resize", update);
    var on = tabs.querySelector(".tab.on");            // open page with the selected tab fully visible
    if (on) tabs.scrollLeft = on.offsetLeft - (tabs.clientWidth - on.offsetWidth) / 2;
    update();
  })();
</script>

{% if not src.table %}
  <p class="hint"></p>
  <div class="empty">The competitor table isn't set up yet. It will show matched prices from every shop side by side.</div>
{% elif not exists %}
  <p class="hint"></p>
  <div class="empty">{% if src.kind == 'competitor' %}The competitor table is empty. Run migrate_competitor_table.py to copy it from Airtable.{% else %}No {{ src.label }} products yet. Run the download for this shop first.{% endif %}</div>
{% else %}
  <form class="bar" method="get">
    <input type="hidden" name="tab" value="{{ src.key }}">
    <input type="text" name="q" value="{{ q }}" placeholder="{% if src.kind == 'competitor' %}Filter by code, SKU, name, brand, barcode{% else %}Filter by {% if src.cols.brand %}brand, {% endif %}name, GTIN{% endif %}">
    {% if src.kind != 'competitor' %}<select name="stock">
      <option value="" {% if not stock %}selected{% endif %}>All</option>
      <option value="In stock" {% if stock=='In stock' %}selected{% endif %}>In stock</option>
      <option value="Out of stock" {% if stock=='Out of stock' %}selected{% endif %}>Out of stock</option>
      <option value="Not on site" {% if stock=='Not on site' %}selected{% endif %}>Not on site</option>
    </select>{% endif %}
    {% if src.kind == 'competitor' %}
    <button type="button" class="fbtn {% if conds %}on{% endif %}" onclick="toggleFilter()">
      &#9776; {% if conds %}Filtered by {{ filt_labels }}{% else %}Filter{% endif %}
    </button>
    {% endif %}
    <button type="submit">Go</button>
    {% if q or stock or conds %}<a class="clear" href="?tab={{ src.key }}">Clear</a>{% endif %}

    {% if src.kind == 'competitor' %}
    {# Airtable-style filter panel: each row = Where <field> <operator> <value>; all rows must match (AND) #}
    <div class="fpanel" id="fpanel" hidden>
      <div class="fquick">
        <span class="ftitle">Quick:</span>
        <a href="?tab=competitor&ff=price&fo=is&fv=high">High price</a>
        <a href="?tab=competitor&ff=price&fo=is&fv=low">Low price</a>
        <a href="?tab=competitor&ff=price&fo=is&fv=none">No competitor price</a>
      </div>
      <div class="ftitle">In this view, show records</div>
      <div id="frows"></div>
      <button type="button" class="fadd" onclick="addRow()">+ Add condition</button>
      <div class="factions">
        <button type="button" class="fcancel" onclick="toggleFilter()">Cancel</button>
        <button type="submit" class="fapply">Apply</button>
      </div>
    </div>
    <datalist id="brandlist">{% for b in brands %}<option value="{{ b }}">{% endfor %}</datalist>
    {% endif %}
  </form>
  <p class="hint">{{ total }} {{ 'product' if total == 1 else 'products' }}{% if q or stock or conds %} match this filter{% endif %}.</p>

  {% for r in rows %}
  {% if src.kind == 'competitor' %}
  <div class="card">
    <div class="title">{{ r.name or r.code or '' }}</div>
    <div class="code">{{ r.code or '' }}</div>
    <div class="meta">{% if r.brand %}Brand <b>{{ r.brand }}</b>{% endif %}{% if r.brand and r.sku %} · {% endif %}{% if r.sku %}SKU <b>{{ r.sku }}</b>{% endif %}{% if r.barcode and (r.brand or r.sku) %} · {% endif %}{% if r.barcode %}Barcode <b>{{ r.barcode }}</b>{% endif %}</div>
    <div class="least">
      {% if r.least_price is not none %}
        <div><div class="plabel">Least price (in stock)</div>
             <span class="price">AED {{ '%.2f' % r.least_price }}</span></div>
        <div><div class="plabel">Least priced website</div>
             {# each website name opens that product on that website #}
             {% set site_links = {
                  'Fragrant Souq': 'https://fragrantsouq.com/search?q=' ~ ((r.sku or r.name or '')|urlencode),
                  'Samawa': r.samawa_url, 'French Fragrance': r.ff_url,
                  'Branded Perfume': r.bp_url, 'Essenzi': r.es_url, 'V Perfumes': r.vp_url} %}
             <span class="{{ 'in' if 'Fragrant Souq' in (r.least_site or '') else 'out' }}">
               {%- for site in (r.least_site or '').split(', ') -%}
                 {%- if site_links.get(site) -%}
                   <a class="sitelink" href="{{ site_links[site] }}" target="_blank">{{ site }}</a>
                 {%- else -%}{{ site }}{%- endif -%}
                 {%- if not loop.last %}, {% endif -%}
               {%- endfor -%}
             </span></div>
        <div><div class="plabel">Suggested price</div>
             {# editable: change the number if needed, then "Update price" writes it to Airtable UAE Price #}
             <div class="upd">
               <span class="sugg">AED</span>
               <input class="sugg-in" type="number" step="0.01" min="0" id="sp-{{ r.fi }}"
                      value="{{ '%g' % r.suggested_price if r.suggested_price is not none else '' }}"
                      placeholder="{{ 'No competitor in stock' if r.suggested_price is none else '' }}">
               <button type="button" class="upd-btn" onclick="updatePrice('{{ r.fi }}', this)">Update price</button>
             </div>
             <div class="upd-msg" id="msg-{{ r.fi }}"></div></div>
      {% else %}<span class="gtin">No price to compare</span>{% endif %}
    </div>
    <div class="prices">
      <div><div class="plabel">Our UAE price</div><span class="price" id="our-{{ r.fi }}">{% if r.uae_price is not none %}AED {{ '%.2f' % r.uae_price }}{% else %}-{% endif %}</span></div>
    </div>
    {# only shops that have this product (a match) or a suggestion to check; "No match" shops are hidden #}
    {% set shown = namespace(n=0) %}
    {% for label, k in src.shops %}
      {% set price = r[k ~ '_price'] %}{% set url = r[k ~ '_url'] %}{% set sug = r[k ~ '_suggestion'] %}
      {% if url or sug %}{% set shown.n = shown.n + 1 %}
      <div class="shop">
        <div class="row">
          <span class="plabel">{{ label }}</span>
          {% if url %}<span class="{{ 'in' if r[k ~ '_stock'] else 'out' }}">{{ 'In stock' if r[k ~ '_stock'] else 'Out of stock' }}</span>{% endif %}
        </div>
        {% if url %}
          <div class="row">
            <span class="price">{% if price is not none %}AED {{ '%.2f' % price }}{% else %}No price{% endif %}</span>
            <a class="link plink" href="{{ url }}" target="_blank" title="Open on {{ label }}">{{ r[k ~ '_title'] or ('View on ' ~ label) }}</a>
          </div>
          {% if r.uae_price is not none and price is not none %}
            {% set d = price - r.uae_price %}
            <div class="diff {{ 'out' if d < 0 else 'in' }}">{% if d < 0 %}{{ label }} is cheaper by AED {{ '%.2f' % (-d) }}{% elif d > 0 %}We are cheaper by AED {{ '%.2f' % d }}{% else %}Same price{% endif %}</div>
          {% endif %}
        {% else %}
          <div class="row"><span class="gtin">No match</span>
          {% if sug %}<a class="link" href="{{ sug }}" target="_blank">Suggestion (check)</a>{% endif %}</div>
        {% endif %}
      </div>
      {% endif %}
    {% endfor %}
    {% if shown.n == 0 %}<div class="shop"><span class="gtin">No competitor has this product</span></div>{% endif %}
    <div class="date">Updated {{ r.updated.strftime('%d %b, %H:%M') if r.updated else '' }}</div>
  </div>
  {% else %}
  <div class="card">
    <div class="title">{% if r.brand %}{{ r.brand }} · {% endif %}{{ r.name or '' }}</div>
    <div class="sub">{{ src.label }}{% if r.volume %} · {{ r.volume }}{% endif %}</div>
    <div class="row">
      <span class="price">{% if r.price is not none %}AED {{ '%.2f' % r.price }}{% else %}No price{% endif %}</span>
      <span class="{{ 'in' if r.stock=='In stock' else ('gone' if r.stock=='Not on site' else 'out') }}">{{ r.stock or '' }}</span>
    </div>
    <div class="row">
      {% if r.url %}<a class="link" href="{{ r.url }}" target="_blank">View product</a>{% else %}<span></span>{% endif %}
      <span class="gtin">{% if r.gtin %}GTIN {{ r.gtin }}{% endif %}</span>
    </div>
    <div class="date">{% if r.updated %}Updated {{ r.updated.strftime('%d %b, %H:%M') }}{% else %}Not checked yet{% endif %}</div>
  </div>
  {% endif %}
  {% else %}
  <div class="empty">No products match this filter. Clear the filter to see everything.</div>
  {% endfor %}

  <div class="pager">
    <a class="pbtn {% if page <= 1 %}off{% endif %}" href="?tab={{ src.key }}&q={{ q|urlencode }}&stock={{ stock|urlencode }}&{{ filt_qs }}&page={{ page-1 }}">Previous</a>
    <span class="gtin">Page {{ page }} of {{ pages }}</span>
    <a class="pbtn {% if page >= pages %}off{% endif %}" href="?tab={{ src.key }}&q={{ q|urlencode }}&stock={{ stock|urlencode }}&{{ filt_qs }}&page={{ page+1 }}">Next</a>
  </div>
{% endif %}

{% if src.kind == 'competitor' %}
<script>
  // ---- Airtable-style filter panel ----
  var FIELD_LIST = {{ field_list|tojson }}, OPS = {{ ops|tojson }}, CONDS = {{ conds|tojson }};
  var FIELDS = {}; FIELD_LIST.forEach(function (f) { FIELDS[f[0]] = f[1]; });   // same order as in Python
  var EMPTY_OPS = ["empty", "notempty"];

  function toggleFilter() {
    var p = document.getElementById("fpanel");
    p.hidden = !p.hidden;
    if (!p.hidden && !document.querySelector("#frows .frow")) addRow();     // start with one empty row
  }

  function chipBox(value) {
    // Airtable-like picker: chosen brands as chips (x to remove) + a box to add more (suggests brand names)
    var wrap = document.createElement("span"); wrap.className = "chips";
    var hidden = document.createElement("input"); hidden.type = "hidden"; hidden.name = "fv";
    var list = (value || "").split("||").filter(Boolean);
    var add = document.createElement("input"); add.type = "text"; add.placeholder = "+ add brand";
    add.setAttribute("list", "brandlist"); add.className = "chip-add";
    function draw() {
      wrap.querySelectorAll(".chip").forEach(function (c) { c.remove(); });
      list.forEach(function (b, i) {
        var c = document.createElement("span"); c.className = "chip"; c.textContent = b + " ";
        var x = document.createElement("b"); x.textContent = "×"; x.title = "Remove";
        x.onclick = function () { list.splice(i, 1); draw(); };
        c.appendChild(x); wrap.insertBefore(c, add);
      });
      hidden.value = list.join("||");
    }
    function addCurrent() {
      var b = add.value.trim();
      if (b && list.indexOf(b) < 0) list.push(b);
      add.value = ""; draw();
    }
    add.onchange = addCurrent;                                         // picked from suggestions
    add.onkeydown = function (e) { if (e.key === "Enter") { e.preventDefault(); addCurrent(); } };
    wrap.appendChild(add); wrap.appendChild(hidden); draw();
    return wrap;
  }

  function valueBox(field, value, op) {
    // choice field -> dropdown, number -> number box, brand "has any/none of" -> chips, text -> text box
    var f = FIELDS[field];
    if (f.type === "multi" && (op === "anyof" || op === "noneof")) return chipBox(value);
    if (f.type === "choice") {
      var sel = document.createElement("select"); sel.name = "fv";
      ["low", "high", "none"].forEach(function (k) {
        var o = new Option(f.choices[k], k); if (k === value) o.selected = true; sel.add(o);
      });
      return sel;
    }
    var inp = document.createElement("input"); inp.name = "fv"; inp.value = value || "";
    inp.type = f.type === "number" ? "number" : "text"; if (f.type === "number") inp.step = "0.01";
    inp.placeholder = "Enter a value";
    if (field === "brand") inp.setAttribute("list", "brandlist");     // brand "contains": suggestions too
    return inp;
  }

  function addRow(field, op, value) {
    field = field || "price";
    var row = document.createElement("div"); row.className = "frow";
    var where = document.createElement("span"); where.className = "fwhere";
    where.textContent = document.querySelector("#frows .frow") ? "and" : "Where";

    var fs = document.createElement("select"); fs.name = "ff";
    FIELD_LIST.forEach(function (f) { var o = new Option(f[1].label, f[0]); if (f[0] === field) o.selected = true; fs.add(o); });

    var os = document.createElement("select"); os.name = "fo";
    var cell = document.createElement("span"); cell.className = "fval";

    function fillOps(selected) {
      os.innerHTML = "";
      OPS[FIELDS[fs.value].type].forEach(function (p) { var o = new Option(p[1], p[0]); if (p[0] === selected) o.selected = true; os.add(o); });
    }
    function fillValue(v) {
      cell.innerHTML = "";
      if (EMPTY_OPS.indexOf(os.value) >= 0) {                 // "is empty" needs no value
        var h = document.createElement("input"); h.type = "hidden"; h.name = "fv"; h.value = "";
        cell.appendChild(h); return;
      }
      var box = valueBox(fs.value, v, os.value);
      cell.appendChild(box);
    }
    fs.onchange = function () { fillOps(); fillValue(); };
    os.onchange = function () {
      // keep what was typed/picked when switching operator (chips <-> text: keep first brand only)
      var cur = cell.querySelector("[name=fv]"); var v = cur ? cur.value : "";
      var multi = os.value === "anyof" || os.value === "noneof";
      fillValue(multi ? v : v.split("||")[0]);
    };

    var del = document.createElement("button"); del.type = "button"; del.className = "fdel"; del.innerHTML = "&#128465;";
    del.title = "Remove condition";
    del.onclick = function () {
      row.remove();
      var first = document.querySelector("#frows .frow .fwhere"); if (first) first.textContent = "Where";
    };

    fillOps(op); fillValue(value);
    [where, fs, os, cell, del].forEach(function (el) { row.appendChild(el); });
    document.getElementById("frows").appendChild(row);
  }

  // rebuild the rows that are active now (so you can see / change them)
  CONDS.forEach(function (c) { addRow(c[0], c[1], c[2]); });
</script>
{% endif %}
<script>
  // "Update price": send the (edited) suggested price to the server -> Airtable UAE Price + competitor table
  async function updatePrice(fi, btn) {
    var input = document.getElementById("sp-" + fi), msg = document.getElementById("msg-" + fi);
    var price = parseFloat(input.value);
    if (!(price > 0)) { msg.className = "upd-msg out"; msg.textContent = "Enter a price first."; return; }
    if (!confirm("Set UAE Price in Airtable to AED " + price + "?")) return;
    btn.disabled = true; msg.className = "upd-msg gtin"; msg.textContent = "Updating Airtable...";
    try {
      var r = await fetch("/competitor/update-price", {method: "POST", headers: {"Content-Type": "application/json"},
                                                      body: JSON.stringify({fi_record_id: fi, price: price})});
      var d = await r.json();
      if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
      document.getElementById("our-" + fi).textContent = "AED " + d.price.toFixed(2);
      msg.className = "upd-msg in";
      msg.textContent = "Updated in Airtable: AED " + d.price.toFixed(2) +
                        (d.least_site ? " | Least now: " + d.least_site : "");
    } catch (e) {
      msg.className = "upd-msg out"; msg.textContent = "Not updated: " + e.message;
    } finally { btn.disabled = false; }
  }
</script>
</div></body></html>
"""


@competitors_bp.route("/competitors")
def competitors():
    tab = request.args.get("tab", "competitor")
    src = SOURCE_BY_KEY.get(tab, SOURCES[1])
    q = request.args.get("q", "").strip()
    stock = request.args.get("stock", "").strip()
    conds = read_filters(request.args)                     # Competitor table: Airtable-style conditions
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1
    print(f"[competitors] tab={src['key']} q={q!r} stock={stock!r} filters={conds} page={page}", flush=True)
    brands = []

    conn = db_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    rows, total, pages, exists = [], 0, 1, False
    try:
        counts = tab_counts(cur)
        if src["table"]:
            exists = table_exists(cur, src["table"])
        if exists:
            c = src["cols"]
            where, params = [], []
            if q:
                search_cols = src.get("search") or [col for col in (c["name"], c["brand"], c["gtin"]) if col]
                where.append("(" + " OR ".join(f"{col} ILIKE %s" for col in search_cols) + ")")
                params += [f"%{q}%"] * len(search_cols)
            if stock and c.get("stock"):
                where.append(f"{c['stock']} = %s")
                params.append(stock)
            if src.get("kind") == "competitor":
                # brand names -> suggestions in the filter's value box
                cur.execute("SELECT DISTINCT brand FROM competitor_table WHERE brand IS NOT NULL ORDER BY brand")
                brands = [r["brand"] for r in cur.fetchall()]
                fw, fp = filter_sql(conds)
                where += fw
                params += fp
            where_sql = ("WHERE " + " AND ".join(where)) if where else ""

            cur.execute(f"SELECT COUNT(*) AS c FROM {src['table']} {where_sql}", params)
            total = cur.fetchone()["c"]
            pages = max(math.ceil(total / PER_PAGE), 1)
            page = min(page, pages)

            # a column that does not exist yet (e.g. before /competitor/update-least ran) -> shown as empty
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (src["table"],))
            existing = {r["column_name"] for r in cur.fetchall()}
            missing = [e for e in c.values() if e and e.isidentifier() and e not in existing]
            if missing:
                print(f"[competitors] Columns not created yet (shown empty): {missing}", flush=True)
            select = ", ".join(
                f"{'NULL' if (not expr or (expr.isidentifier() and expr not in existing)) else expr} AS {alias}"
                for alias, expr in c.items())
            cur.execute(
                f"SELECT {select} FROM {src['table']} {where_sql} ORDER BY id LIMIT %s OFFSET %s",
                params + [PER_PAGE, (page - 1) * PER_PAGE],
            )
            rows = cur.fetchall()
            print(f"[competitors] {total} matching rows, showing {len(rows)} on page {page}/{pages}", flush=True)
    finally:
        cur.close()
        conn.close()

    return render_template_string(HTML, sources=SOURCES, src=src, counts=counts, rows=rows,
                                  total=total, page=page, pages=pages, q=q, stock=stock, exists=exists,
                                  brands=brands, conds=conds, field_list=list(FILTER_FIELDS.items()), ops=FILTER_OPS,
                                  filt_labels=", ".join(dict.fromkeys(FILTER_FIELDS[f]["label"] for f, _, _ in conds)),
                                  filt_qs=urlencode([(k, x) for f, o, v in conds
                                                     for k, x in (("ff", f), ("fo", o), ("fv", v))]))
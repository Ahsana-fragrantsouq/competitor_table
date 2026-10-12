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
from flask import Blueprint, request, render_template_string, g, redirect
from auth import list_users, is_admin, MIN_PASSWORD      # User management (users are saved by auth.py)

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
               ("V Perfumes", "vp"), ("Al Injaz", "aj"),
               ("Ounass", "on"), ("Amberiya", "am"), ("Niche Gallerie", "ng")],
     "cols": {"code": "french_inventory_code", "sku": "sku", "brand": "brand", "barcode": "barcode", "name": "product_name",
              "uae_price": "uae_price",
              "samawa_url": "samawa_link", "samawa_price": "samawa_price", "samawa_stock": "samawa_stock",
              "samawa_suggestion": "samawa_suggestion",
              "ff_url": "ff_link", "ff_price": "ff_price", "ff_stock": "ff_stock", "ff_suggestion": "ff_suggestion",
              # Branded Perfume / Essenzi come through the French Fragrance match -> no suggestion column
              "bp_url": "bp_link", "bp_price": "bp_price", "bp_stock": "bp_stock", "bp_suggestion": None,
              "es_url": "es_link", "es_price": "es_price", "es_stock": "es_stock", "es_suggestion": None,
              "vp_url": "vp_link", "vp_price": "vp_price", "vp_stock": "vp_stock", "vp_suggestion": "vp_suggestion",
              "aj_url": "aj_link", "aj_price": "aj_price", "aj_stock": "aj_stock", "aj_suggestion": "aj_suggestion",
              "aj_title": "aj_title", "aj_confirmed": "aj_confirmed",
              "on_url": "on_link", "on_price": "on_price", "on_stock": "on_stock", "on_suggestion": "on_suggestion",
              "on_title": "on_title", "on_confirmed": "on_confirmed",
              "am_url": "am_link", "am_price": "am_price", "am_stock": "am_stock", "am_suggestion": "am_suggestion",
              "am_title": "am_title", "am_confirmed": "am_confirmed",
              "ng_url": "ng_link", "ng_price": "ng_price", "ng_stock": "ng_stock", "ng_suggestion": "ng_suggestion",
              "ng_title": "ng_title", "ng_confirmed": "ng_confirmed",
              # competitor's own product name, used as the link text
              "samawa_title": "samawa_title", "ff_title": "ff_title", "bp_title": "bp_title",
              "es_title": "es_title", "vp_title": "vp_title",
              "samawa_confirmed": "samawa_confirmed", "ff_confirmed": "ff_confirmed", "vp_confirmed": "vp_confirmed",
              "bp_confirmed": None, "es_confirmed": None,
              "our_link": "our_link", "our_title": "our_title", "our_stock": "our_stock",
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
    # Al Injaz Perfumes = alinjazperfumes.com (WooCommerce product list), alinjaz.py
    {"key": "aj", "label": "Al Injaz", "table": "alinjaz_catalog",
     "cols": {"name": "name", "brand": None, "gtin": "COALESCE(gtin, sku)", "url": "product_url",
              "price": "COALESCE(price_inc_tax, price)", "stock": "stock", "volume": "volume",
              "updated": "updated_at"}},
    # Ounass = ounass.ae women + men fragrance pages, ounass.py
    {"key": "on", "label": "Ounass", "table": "ounass_catalog",
     "cols": {"name": "name", "brand": "brand", "gtin": "sku", "url": "product_url",
              "price": "COALESCE(price_inc_tax, price)", "stock": "stock", "volume": "volume",
              "updated": "updated_at"}},
    # Amberiya = amberiya.ae (Shopify products.json), amberiya.py
    {"key": "am", "label": "Amberiya", "table": "amberiya_catalog",
     "cols": {"name": "name", "brand": "brand", "gtin": "COALESCE(gtin, sku)", "url": "product_url",
              "price": "COALESCE(price_inc_tax, price)", "stock": "stock", "volume": "volume",
              "updated": "updated_at"}},
    # Niche Gallerie = nichegallerie.com (WooCommerce product list), nichegallerie.py
    {"key": "ng", "label": "Niche Gallerie", "table": "nichegallerie_catalog",
     "cols": {"name": "name", "brand": None, "gtin": "COALESCE(gtin, sku)", "url": "product_url",
              "price": "COALESCE(price_inc_tax, price)", "stock": "stock", "volume": "volume",
              "updated": "updated_at"}},
    # {"key": "shop3", "label": "Shop 3", "table": "shop3_catalog", "cols": {...}},   # add more here
]
SOURCE_BY_KEY = {s["key"]: s for s in SOURCES}

# ---------------------------------------------------------------- menu bar (like fragrantsouq.com)
# Competitor table | Competitors ▾ (every shop's own table) | Settings
NAV_CSS = """
 .menu{display:flex;gap:6px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--line);
       margin:0 0 6px;padding:0 0 10px}
 .menu > a,.menu summary{color:var(--text);text-decoration:none;font-weight:600;font-size:17px;
       padding:9px 12px;border-radius:10px;cursor:pointer;list-style:none;white-space:nowrap}
 .menu summary::-webkit-details-marker{display:none}
 .menu > a:hover,.menu summary:hover{background:var(--card)}
 .menu .on{color:#f2d675;box-shadow:inset 0 -2px 0 #f2d675;border-radius:10px 10px 0 0}
 .dd{position:relative}
 .dd .car{font-size:12px;margin-left:4px;display:inline-block;transition:transform .15s}
 .dd[open] .car{transform:rotate(180deg)}
 .ddmenu{position:absolute;left:0;top:calc(100% + 6px);z-index:60;min-width:250px;background:var(--card);
         border:1px solid var(--line);border-radius:12px;padding:6px;box-shadow:0 10px 28px rgba(0,0,0,.45)}
 .ddmenu a{display:flex;justify-content:space-between;gap:16px;padding:10px 12px;border-radius:8px;
           color:var(--text);text-decoration:none;font-size:15px}
 .ddmenu a:hover{background:var(--bg)}
 .ddmenu a.cur{color:#f2d675;font-weight:600}
 .ddmenu .n{color:var(--muted);font-size:13px}
"""

NAV_HTML = """
<nav class="menu" aria-label="Main menu">
  <a href="/competitors?tab=competitor" class="{{ 'on' if active == 'competitor' else '' }}">Competitor table</a>
  <details class="dd" id="ddComp">
    <summary class="{{ 'on' if active == 'shop' else '' }}">Competitors<span class="car">&#9662;</span></summary>
    <div class="ddmenu">
      {% for s in sources if s.key != 'competitor' %}
      <a href="/competitors?tab={{ s.key }}" class="{{ 'cur' if s.key == cur_key else '' }}">
        <span>{{ s.label }}</span><span class="n">{{ counts.get(s.key, 0) }}</span></a>
      {% endfor %}
    </div>
  </details>
  <details class="dd" id="ddSet">
    <summary class="{{ 'on' if active == 'settings' else '' }}">Settings<span class="car">&#9662;</span></summary>
    <div class="ddmenu">
      {% for key, label, about in settings_options %}
      <a href="/settings/{{ key }}" class="{{ 'cur' if key == cur_key else '' }}"><span>{{ label }}</span></a>
      {% endfor %}
    </div>
  </details>
</nav>
<script>
  // Menu logs: open the browser console (F12 -> Console) to see them
  (function () {
    var dds = Array.prototype.slice.call(document.querySelectorAll(".menu details.dd"));
    console.log("[MENU] Page loaded: " + location.pathname + location.search);
    dds.forEach(function (d) {
      var name = d.querySelector("summary").firstChild.textContent.trim();     // "Competitors" / "Settings"
      d.addEventListener("toggle", function () {
        console.log("[MENU] " + name + " dropdown " + (d.open ? "opened" : "closed"));
        if (d.open) dds.forEach(function (o) { if (o !== d) o.removeAttribute("open"); });   // only one open
      });
      d.querySelectorAll(".ddmenu a").forEach(function (a) {
        a.addEventListener("click", function () {
          console.log("[MENU] " + name + " -> " + a.querySelector("span").textContent + " (" + a.href + ")");
        });
      });
    });
    document.querySelectorAll(".menu > a").forEach(function (a) {   // Competitor table
      a.addEventListener("click", function () { console.log("[MENU] Menu click: " + a.textContent.trim()); });
    });
    // close dropdowns when clicking anywhere else or pressing Esc
    document.addEventListener("click", function (e) {
      dds.forEach(function (d) {
        if (d.open && !d.contains(e.target)) { console.log("[MENU] Click outside -> closing dropdown"); d.removeAttribute("open"); }
      });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      dds.forEach(function (d) {
        if (d.open) { console.log("[MENU] Esc pressed -> closing dropdown"); d.removeAttribute("open"); }
      });
    });
  })();
</script>
"""


# Settings options (pages are empty for now - content will be added later)
SETTINGS_OPTIONS = [
    ("users", "User management", "Admin creates users and manages them (add, change password, remove)."),
    ("discount", "Auto discount rate", "How far below the cheapest competitor the suggested price is set."),
    ("automate", "Automate", "Run downloads, matching and price updates automatically on a schedule."),
]


def render_nav(active, cur_key, counts):
    """Menu bar HTML. active = 'competitor' / 'shop' / 'settings'."""
    shops = [s["label"] for s in SOURCES if s["key"] != "competitor"]
    print(f"[MENU] Building menu: active={active!r} current page={cur_key!r} | "
          f"Competitors dropdown has {len(shops)} shops: {', '.join(shops)}", flush=True)
    return render_template_string(NAV_HTML, active=active, cur_key=cur_key, counts=counts or {}, sources=SOURCES,
                                  settings_options=SETTINGS_OPTIONS)
# competitor names for the "Competitor" filter: "Samawa" -> column prefix "samawa" (samawa_link, samawa_stock ...)
COMPETITORS = {label: prefix for label, prefix in SOURCES[0]["shops"]}

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
    # our own website: is the product on fragrantsouq.com? (our_link is filled by /competitor/load-our-links)
    "website":   {"label": "Fragrantsouq.com", "type": "choice", "order": ["no", "yes"],
                  "choices": {"no": "Not on fragrantsouq.com", "yes": "On fragrantsouq.com"},
                  "sql": {"no": "(our_link IS NULL OR our_link = '')", "yes": "(our_link IS NOT NULL AND our_link <> '')"}},
    "brand":     {"label": "Brand", "col": "brand", "type": "multi"},      # pick several brands (chips)
    "comp":      {"label": "Competitor", "type": "comp"},                  # pick competitor shops (chips)
    "name":      {"label": "Product name", "col": "product_name", "type": "text"},
    "sku":       {"label": "SKU", "col": "sku", "type": "text"},
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
    # competitor: which shops SELL the product (= matched link on that shop)
    "comp":   [("anyof", "has any of..."), ("allof", "has all of..."), ("noneof", "has none of..."),
               ("instock", "in stock at any of..."), ("cheapest", "cheapest is any of...")],
}
NUM_SQL = {"eq": "=", "ne": "<>", "gt": ">", "lt": "<", "ge": ">=", "le": "<="}


def read_filters(args):
    """URL ?ff=brand&fo=is&fv=Armaf&ff=price&fo=is&fv=high ... -> list of valid (field, op, value)."""
    print(f"[FILTER] Reading filter rows from the URL: ff={args.getlist('ff')} fo={args.getlist('fo')}", flush=True)
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


def describe_filters(conds):
    """Filter rows -> readable text for the Filter button, e.g.
    'Price status is Low price · SKU contains 123 · Brand has any of Armaf, Afnan'."""
    parts = []
    for f, o, v in conds:
        fd = FILTER_FIELDS[f]
        field = fd["label"].split(" (")[0]                          # "Price status (High / Low ...)" -> "Price status"
        op = dict(FILTER_OPS[fd["type"]]).get(o, o).replace("...", "")
        if o in ("empty", "notempty"):
            value = ""
        elif fd["type"] == "choice":
            value = fd["choices"].get(v, v).split(" (")[0]           # "Low price (we're cheapest)" -> "Low price"
        else:
            value = ", ".join(x for x in v.split("||") if x)         # chips "Armaf||Afnan" -> "Armaf, Afnan"
        if f == "website":
            value = fd["choices"].get(v, v)
            parts.append(value if o == "is" else f"not '{value}'")
            continue
        parts.append(f"{field} {op} {value}".strip())
    text = " · ".join(parts)
    print(f"[FILTER] Button text: {text!r}", flush=True)
    return text


def filter_sql(conds):
    """(field, op, value) list -> (list of SQL pieces, params). Unknown values are skipped."""
    print(f"[FILTER] Turning {len(conds)} filter condition(s) into SQL: {conds}", flush=True)
    where, params = [], []
    for f, o, v in conds:
        fd = FILTER_FIELDS[f]
        if fd["type"] == "choice":                     # Price status / Fragrantsouq.com: fixed choices
            sql_map = fd.get("sql") or {k: c for k, (_, c) in PRICE_FILTERS.items()}
            if v not in sql_map:
                continue
            cond = sql_map[v]
            where.append(f"({cond})" if o == "is" else f"NOT ({cond})")
            continue
        if fd["type"] == "comp":                       # Competitor: which shops have the product
            picked = [n.strip() for n in v.split("||") if n.strip() in COMPETITORS]   # unknown names ignored
            if not picked:
                continue
            if o == "cheapest":                        # shop is (one of) the least priced website(s)
                parts = ["least_priced_website ILIKE %s" for _ in picked]
                params += [f"%{n}%" for n in picked]
                where.append("(" + " OR ".join(parts) + ")")
                continue
            if o == "instock":
                parts = [f"({COMPETITORS[n]}_link IS NOT NULL AND {COMPETITORS[n]}_stock)" for n in picked]
            else:
                parts = [f"{COMPETITORS[n]}_link IS NOT NULL" for n in picked]
            if o == "allof":
                where.append("(" + " AND ".join(parts) + ")")
            elif o == "noneof":
                where.append("NOT (" + " OR ".join(parts) + ")")
            else:                                      # anyof / instock
                where.append("(" + " OR ".join(parts) + ")")
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


def add_next_cheapest(rows, shops):
    """When WE are the cheapest: find the cheapest IN-STOCK competitor (the one closest to our price)
    and put it in row['next_comp'] = {'label', 'price', 'url', 'gap'} for the card."""
    found = 0
    for r in rows:
        r["next_comp"] = None
        if "Fragrant Souq" not in (r.get("least_site") or ""):      # we are not the cheapest -> nothing to show
            continue
        offers = [(float(r[f"{k}_price"]), label, r.get(f"{k}_url"))
                  for label, k in shops
                  if r.get(f"{k}_stock") and r.get(f"{k}_url") and r.get(f"{k}_price") and r[f"{k}_price"] > 0]
        if not offers:
            continue
        price, label, url = min(offers)
        ours = float(r["uae_price"]) if r.get("uae_price") else None
        r["next_comp"] = {"label": label, "price": price, "url": url,
                          "gap": round(price - ours, 2) if ours is not None else None}
        found += 1
    print(f"[NEXT-CHEAPEST] We are cheapest on {found} of {len(rows)} products on this page "
          f"-> showing the next cheapest competitor next to Update price", flush=True)


def db_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    print("[competitors] Opening connection to french_fragrance_db", flush=True)
    return psycopg2.connect(url, sslmode="require")


def table_exists(cur, table):
    print(f"[competitors] Checking table exists: {table}", flush=True)
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
 .page{max-width:920px;margin:0 auto;padding:22px 18px 40px}
 h1{font-size:26px;margin:4px 0 18px}
{{ nav_css|safe }}
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
 .bar .brandchips{flex:1 1 260px;min-height:50px;background:var(--card)}   /* Brand chips on the shop tabs */
 .bar .brandchips .chip-add{flex:1;font-size:16px;color:var(--text)}
 .bar > button[type=submit]{background:#f2d675;color:#1c1b19;border:0;border-radius:12px;padding:0 18px;font-size:16px;font-weight:600;cursor:pointer}
 .prices{display:flex;gap:22px;flex-wrap:wrap;margin-top:8px}
 .plabel{color:var(--muted);font-size:13px}
 .diff{font-size:14px;margin-top:8px}
 .code{color:var(--muted);font-size:14px;font-family:Consolas,monospace}
 .shop{border-top:1px solid var(--line);margin-top:12px;padding-top:10px}
 /* "Show out of stock (N)" button at the bottom of a card + the hidden list it opens */
 .outtoggle{margin-top:10px;background:none;border:1px solid var(--line);color:var(--muted);border-radius:8px;
            padding:7px 12px;font-size:14px;cursor:pointer}
 .outtoggle:hover{color:var(--text);border-color:var(--muted)}
 .outlist[hidden]{display:none}
 .outlist .shop{opacity:.75}
 .sugg{color:#f2d675}
 .prices{align-items:flex-end}
 /* "Out of stock" above OUR product link (shown only when our product is out of stock on fragrantsouq.com).
    Small text so it doesn't compete with the prices. Colour comes from .out (red). */
 .ourstock{font-size:13px;font-weight:500;margin-bottom:3px}
 .ourlink{margin-left:auto;text-align:right;max-width:65%;align-self:flex-end}
 .topbar{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
 .who{color:var(--muted);font-size:14px} .who b{color:var(--text)} .who a{color:var(--gold)}
 .titlerow{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
 .pick{width:20px;height:20px;flex:0 0 auto;margin-top:3px;accent-color:#f2d675;cursor:pointer}
 .pickallrow{display:flex;justify-content:flex-end;padding-right:18px}
 .pickall{display:inline-flex;align-items:center;gap:8px;color:var(--muted);font-size:14px;margin:0 2px 12px;cursor:pointer}
 .pickall input{width:18px;height:18px;accent-color:#f2d675}
 .card.picked{border-color:#f2d675}
 .bulkbar{position:fixed;left:50%;transform:translateX(-50%);bottom:max(16px, env(safe-area-inset-bottom, 0px));
          display:flex;gap:12px;align-items:center;flex-wrap:wrap;background:#0f2b3a;color:#fff;
          border-radius:14px;padding:12px 18px;box-shadow:0 8px 24px rgba(0,0,0,.45);z-index:50;max-width:92vw}
 .bulkbar[hidden]{display:none}            /* without this, display:flex above would keep the bar visible */
 .bulkcount{background:#2f7de1;border-radius:999px;padding:5px 12px;font-weight:600;font-size:14px}
 .bulkcancel{background:#fff;color:#1c1b19;border:0;border-radius:8px;padding:9px 16px;font-weight:600;cursor:pointer}
 .bulkupdate{background:#f2d675;color:#1c1b19;border:0;border-radius:8px;padding:9px 16px;font-weight:700;cursor:pointer}
 .bulkupdate:disabled{opacity:.6;cursor:wait}
 .bulkmsg{font-size:14px;color:#cfe3ff}
 .sugrow{display:inline-flex;gap:8px;align-items:center;flex-wrap:wrap}
 .okbtn,.nobtn{border:0;border-radius:8px;padding:6px 12px;font-weight:600;cursor:pointer}
 .okbtn{background:#8cc58c;color:#10240f}
 .nobtn{background:#e0806c;color:#2a0f09}
 .conf{color:var(--green);font-size:12px;margin-left:6px}
 .plink{text-align:right;max-width:65%}
 .meta{color:var(--muted);font-size:14px;margin-top:4px}
 .meta b{color:var(--text);font-weight:600}
 .upd{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:2px}
 .sugg-in{width:110px;background:var(--card);color:#f2d675;border:1px solid var(--line);border-radius:10px;
          padding:8px 10px;font-size:20px;font-weight:600}
 .upd-btn{background:#f2d675;color:#1c1b19;border:0;border-radius:10px;padding:10px 14px;font-weight:600;cursor:pointer}
 .nextc{color:var(--gold);font-size:15px}
 .upd-btn:disabled{opacity:.6;cursor:wait}
 .upd-msg{font-size:13px;margin-top:6px;min-height:16px}
 .sitelink{color:inherit;text-decoration:underline;text-underline-offset:3px}
 .sitelink:hover{color:#f2d675}
 .least{display:flex;flex-wrap:nowrap;align-items:center;gap:10px 22px;margin:10px 0 2px;padding:10px 14px;
        border:1px solid #f2d675;border-radius:12px}
 /* one line: least price | least priced website | suggested price + Update price + next cheapest */
 .least > div{flex:0 0 auto;min-width:0}
 .least .upd{flex-wrap:nowrap}
 .least .sugg-in{width:92px}
 .least .nextc{white-space:nowrap}
 .least .updcol{margin-left:auto;align-self:center}        /* Update price pushed to the right ... */
 .least .second{text-align:right;padding-left:18px;border-left:1px solid var(--line)}   /* ... next to Second least */
 .least .second .plabel{font-size:12px}
 .least .upd-msg{min-height:0;margin-top:0}
 .least .upd-msg:not(:empty){margin-top:6px}
 @media (max-width:860px){ .least{flex-wrap:wrap} .least > div{flex:0 1 auto;max-width:100%}
                           .least .upd{flex-wrap:wrap}
                           .least .second{border-left:0;padding-left:0;margin-left:auto} }   /* phones: wrap again */
 .hint{color:var(--muted);font-size:14px;margin:6px 2px 16px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px;margin-bottom:12px}
 .title{font-size:18px;font-weight:600;margin-bottom:6px;line-height:1.35}
 .sub{color:var(--muted);font-size:15px;margin-bottom:10px}
 .row{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-top:8px}
 .price{font-size:20px;font-weight:600}
 .gtin{color:var(--muted);font-size:14px}
 /* GTIN with "Missing item" under it (shop tabs) */
 .gtinbox{display:flex;flex-direction:column;align-items:flex-end;gap:3px}
 .missing{color:#e0806c;font-size:13px;font-weight:600}
 a.link{color:var(--gold)}
 .in{color:var(--green)} .out{color:var(--red)} .gone{color:var(--grey)}
 .date{color:var(--grey);font-size:13px;margin-top:8px}
 .empty{background:var(--card);border:1px dashed var(--line);border-radius:16px;padding:28px 20px;text-align:center;color:var(--muted)}
 .pager{display:flex;justify-content:space-between;align-items:center;margin-top:16px}
 .pbtn{background:#f2d675;color:#1c1b19;text-decoration:none;border-radius:12px;padding:12px 18px;font-weight:600}
 .pbtn.off{visibility:hidden}
 :focus-visible{outline:2px solid var(--gold);outline-offset:2px}
</style></head><body><div class="page">

<div class="topbar"><h1>{{ src.label }}</h1>
  {% if g.user %}<span class="who">Logged in as <b>{{ g.user }}</b> · <a href="/logout">Log out</a></span>{% endif %}</div>

{{ nav_html|safe }}

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
    {% if src.kind != 'competitor' %}
    {# Brand filter like the competitor table: pick several brands as chips (x removes one).
         Sent as one value "Armaf||Afnan" in ?brand=. Suggestions = brands of this shop / of our products. #}
    <span class="chips brandchips" id="brandchips">
      <input type="hidden" name="brand" id="brandval" value="{{ brand }}">
      <input type="text" class="chip-add" id="brandadd" list="shopbrands" placeholder="+ add brand" autocomplete="off">
    </span>
    <datalist id="shopbrands">{% for b in shop_brands %}<option value="{{ b }}">{% endfor %}</datalist>
    <script>
      (function () {
        var wrap = document.getElementById("brandchips"), hidden = document.getElementById("brandval"),
            add = document.getElementById("brandadd");
        var list = hidden.value.split("||").filter(Boolean);
        function draw() {
          wrap.querySelectorAll(".chip").forEach(function (c) { c.remove(); });
          list.forEach(function (b, i) {
            var c = document.createElement("span"); c.className = "chip"; c.textContent = b + " ";
            var x = document.createElement("b"); x.textContent = "\u00d7"; x.title = "Remove";
            x.onclick = function () { console.log("[BRAND] Removed: " + b); list.splice(i, 1); draw(); };
            c.appendChild(x); wrap.insertBefore(c, add);
          });
          hidden.value = list.join("||");
        }
        function addCurrent() {
          var b = add.value.trim();
          if (b && list.indexOf(b) < 0) { list.push(b); console.log("[BRAND] Added: " + b + " -> " + list.join(", ")); }
          add.value = ""; draw();
        }
        add.onchange = addCurrent;                                             // picked from the suggestions
        add.onkeydown = function (e) { if (e.key === "Enter") { e.preventDefault(); addCurrent(); } };
        add.form.addEventListener("submit", function () { if (add.value.trim()) addCurrent(); });  // typed, then Go
        draw();
      })();
    </script>
    <select name="stock">
      <option value="" {% if not stock %}selected{% endif %}>All</option>
      <option value="In stock" {% if stock=='In stock' %}selected{% endif %}>In stock</option>
      <option value="Out of stock" {% if stock=='Out of stock' %}selected{% endif %}>Out of stock</option>
      <option value="Not on site" {% if stock=='Not on site' %}selected{% endif %}>Not on site</option>
      {# products not linked to any of our products #}
      <option value="missing" {% if stock=='missing' %}selected{% endif %}>Missing items</option>
    </select>{% endif %}
    {% if src.kind == 'competitor' %}
    <button type="button" class="fbtn {% if conds %}on{% endif %}" onclick="toggleFilter()">
      &#9776; {% if conds %}{{ filt_labels }}{% else %}Filter{% endif %}
    </button>
    {% endif %}
    <button type="submit">Go</button>
    {% if q or stock or conds or brand %}<a class="clear" href="?tab={{ src.key }}">Clear</a>{% endif %}

    {% if src.kind == 'competitor' %}
    {# Airtable-style filter panel: each row = Where <field> <operator> <value>; all rows must match (AND) #}
    <div class="fpanel" id="fpanel" hidden>
      <div class="fquick">
        <span class="ftitle">Quick:</span>
        <a href="?tab=competitor&ff=price&fo=is&fv=high">High price</a>
        <a href="?tab=competitor&ff=price&fo=is&fv=low">Low price</a>
        <a href="?tab=competitor&ff=price&fo=is&fv=none">No competitor price</a>
        <a href="?tab=competitor&ff=website&fo=is&fv=no">Not on fragrantsouq.com</a>
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
    <datalist id="complist">{% for c in competitors %}<option value="{{ c }}">{% endfor %}</datalist>
    {% endif %}
  </form>
  <p class="hint">{{ total }} {{ 'product' if total == 1 else 'products' }}{% if q or stock or conds or brand %} match this filter{% endif %}.</p>
  {% if src.kind == 'competitor' and rows %}
  <div class="pickallrow"><label class="pickall">Select all on this page <input type="checkbox" id="pickAll" onchange="pickAll(this.checked)"></label></div>
  {# bottom bar (like Amazon "Save all"): appears when 2 or more products are ticked #}
  <div class="bulkbar" id="bulkbar" hidden>
    <span class="bulkcount" id="bulkcount">0 selected</span>
    <button type="button" class="bulkcancel" onclick="pickAll(false)">Cancel</button>
    <button type="button" class="bulkupdate" id="bulkupdate" onclick="updateAll()">Update all</button>
    <span class="bulkmsg" id="bulkmsg"></span>
  </div>
  {% endif %}

{# ---- one competitor block inside a product card (used for in-stock and for the hidden out-of-stock list) ---- #}
{% macro shop_block(r, label, k) %}
      {% set price = r[k ~ '_price'] %}{% set url = r[k ~ '_url'] %}{% set sug = r[k ~ '_suggestion'] %}
      <div class="shop">
        <div class="row">
          <span class="plabel">{{ label }}{% if r[k ~ '_confirmed'] %} <span class="conf">✓ confirmed</span>{% endif %}</span>
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
          {% if sug %}<span class="sugrow">
            <a class="link" href="{{ sug }}" target="_blank">Suggestion (check)</a>
            {# OK = make it the real match, Not OK = remove it (both remembered for later match runs) #}
            <button type="button" class="okbtn" onclick="suggestion('{{ r.fi }}', '{{ k }}', 'ok', this)">OK</button>
            <button type="button" class="nobtn" onclick="suggestion('{{ r.fi }}', '{{ k }}', 'notok', this)">Not OK</button>
          </span>{% endif %}</div>
          <div class="upd-msg" id="sg-{{ k }}-{{ r.fi }}"></div>
        {% endif %}
      </div>
{% endmacro %}
  {% for r in rows %}
  {% if src.kind == 'competitor' %}
  <div class="card">
    <div class="title titlerow">
      <span>{{ r.name or r.code or '' }}</span>
      <input type="checkbox" class="pick" data-fi="{{ r.fi }}" onchange="pickChanged()" title="Select"></div>
    <div class="code">{{ r.code or '' }}</div>
    <div class="meta">{% if r.brand %}Brand <b>{{ r.brand }}</b>{% endif %}{% if r.brand and r.sku %} · {% endif %}{% if r.sku %}SKU <b>{{ r.sku }}</b>{% endif %}{% if r.barcode and (r.brand or r.sku) %} · {% endif %}{% if r.barcode %}Barcode <b>{{ r.barcode }}</b>{% endif %}</div>
    <div class="least">
      {% if r.least_price is not none %}
        <div><div class="plabel">Least price (in stock)</div>
             <span class="price">AED {{ '%.2f' % r.least_price }}</span></div>
        <div><div class="plabel">Least priced website</div>
             {# each website name opens that product on that website #}
             {% set site_links = {
                  'Fragrant Souq': r.our_link or ('https://fragrantsouq.com/search?q=' ~ ((r.sku or r.name or '')|urlencode)),
                  'Samawa': r.samawa_url, 'French Fragrance': r.ff_url,
                  'Branded Perfume': r.bp_url, 'Essenzi': r.es_url, 'V Perfumes': r.vp_url, 'Al Injaz': r.aj_url,
                  'Ounass': r.on_url, 'Amberiya': r.am_url,
                  'Niche Gallerie': r.ng_url} %}
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
             </div>
             <div class="upd-msg" id="msg-{{ r.fi }}"></div></div>
        {# right side of the box: Update price button, then Second least #}
        <div class="updcol">
             <button type="button" class="upd-btn" onclick="updatePrice('{{ r.fi }}', this)">Update price</button></div>
        {% if r.next_comp %}
        {# we are the cheapest: the cheapest IN-STOCK competitor (closest to our price), at the right of the box #}
        <div class="second"><div class="plabel">Second least</div>
             <a class="nextc" href="{{ r.next_comp.url }}" target="_blank"
                title="AED {{ '%.2f' % r.next_comp.price }}">{{ r.next_comp.label }}</a></div>
        {% endif %}
      {% else %}<span class="gtin">No price to compare</span>{% endif %}
    </div>
    <div class="prices">
      <div><div class="plabel">Our UAE price</div><span class="price" id="our-{{ r.fi }}">{% if r.uae_price is not none %}AED {{ '%.2f' % r.uae_price }}{% else %}-{% endif %}</span></div>
      {# our own product page on fragrantsouq.com (matched by SKU), like the competitors' links #}
      <div class="ourlink">
        {# OUR stock on fragrantsouq.com (column our_stock, saved by /competitor/load-our-links from Shopify "available"):
             our_stock = false -> "Out of stock" (small, red)
             our_stock = true  -> nothing shown (in stock is normal)
             our_stock = none  -> nothing shown (not on fragrantsouq.com / links not loaded yet) #}
        {% if r.our_link and r.our_stock == false %}<div class="out ourstock">Out of stock</div>{% endif %}
        {% if r.our_link %}<a class="link" href="{{ r.our_link }}" target="_blank" title="Open on fragrantsouq.com">{{ r.our_title or r.name }}</a>
        {% else %}<span class="gtin">Not on fragrantsouq.com</span>{% endif %}</div>
    </div>
    {# only shops that have this product (a match) or a suggestion to check; "No match" shops are hidden.
         IN-STOCK matches + suggestions are shown; OUT-OF-STOCK matches are hidden under "Show out of stock". #}
    {% set shown = namespace(n=0, out=0) %}
    {% for label, k in src.shops %}
      {% set url = r[k ~ '_url'] %}{% set sug = r[k ~ '_suggestion'] %}
      {% if url and not r[k ~ '_stock'] %}{% set shown.out = shown.out + 1 %}   {# out of stock -> hidden list #}
      {% elif url or sug %}{% set shown.n = shown.n + 1 %}{{ shop_block(r, label, k) }}
      {% endif %}
    {% endfor %}
    {% if shown.n == 0 %}<div class="shop"><span class="gtin">{% if shown.out %}No competitor has this product in stock{% else %}No competitor has this product{% endif %}</span></div>{% endif %}
    <div class="date">Updated {{ r.updated.strftime('%d %b, %H:%M') if r.updated else '' }}</div>
    {% if shown.out %}
    {# out-of-stock competitors: hidden until "Show out of stock" is clicked (toggleOut in the script below) #}
    <button type="button" class="outtoggle" onclick="toggleOut(this)" data-n="{{ shown.out }}">Show out of stock ({{ shown.out }})</button>
    <div class="outlist" hidden>
      {% for label, k in src.shops %}
        {% if r[k ~ '_url'] and not r[k ~ '_stock'] %}{{ shop_block(r, label, k) }}{% endif %}
      {% endfor %}
    </div>
    {% endif %}
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
      <span class="gtinbox">
        <span class="gtin">{% if r.gtin %}GTIN {{ r.gtin }}{% endif %}</span>
        {# not linked to any of our products (not in Airtable / competitor table) #}
        {% if r.missing %}<span class="missing">Missing item</span>{% endif %}
      </span>
    </div>
    <div class="date">{% if r.updated %}Updated {{ r.updated.strftime('%d %b, %H:%M') }}{% else %}Not checked yet{% endif %}</div>
  </div>
  {% endif %}
  {% else %}
  <div class="empty">No products match this filter. Clear the filter to see everything.</div>
  {% endfor %}

  <div class="pager">
    <a class="pbtn {% if page <= 1 %}off{% endif %}" href="?tab={{ src.key }}&q={{ q|urlencode }}&stock={{ stock|urlencode }}&brand={{ brand|urlencode }}&{{ filt_qs }}&page={{ page-1 }}">Previous</a>
    <span class="gtin">Page {{ page }} of {{ pages }}</span>
    <a class="pbtn {% if page >= pages %}off{% endif %}" href="?tab={{ src.key }}&q={{ q|urlencode }}&stock={{ stock|urlencode }}&brand={{ brand|urlencode }}&{{ filt_qs }}&page={{ page+1 }}">Next</a>
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

  function chipBox(value, listId, word) {
    // Airtable-like picker: chosen brands as chips (x to remove) + a box to add more (suggests brand names)
    var wrap = document.createElement("span"); wrap.className = "chips";
    var hidden = document.createElement("input"); hidden.type = "hidden"; hidden.name = "fv";
    var list = (value || "").split("||").filter(Boolean);
    var add = document.createElement("input"); add.type = "text"; add.placeholder = "+ add " + (word || "brand");
    add.setAttribute("list", listId || "brandlist"); add.className = "chip-add";
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
    if (f.type === "comp") return chipBox(value, "complist", "competitor");      // competitor names as chips
    if (f.type === "multi" && (op === "anyof" || op === "noneof")) return chipBox(value);
    if (f.type === "choice") {
      var sel = document.createElement("select"); sel.name = "fv";
      (f.order || ["low", "high", "none"]).forEach(function (k) {
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
      var multi = FIELDS[fs.value].type === "comp" || os.value === "anyof" || os.value === "noneof";
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
  // ---- tick products + "Update all" (UAE Price in Airtable = each product's suggested price box) ----
  function picked() { return Array.prototype.slice.call(document.querySelectorAll(".pick:checked")); }
  function pickChanged() {
    var n = picked().length, all = document.querySelectorAll(".pick").length;
    var bar = document.getElementById("bulkbar"); if (!bar) return;
    bar.hidden = n < 2;                                         // bar only from 2 ticked products
    document.getElementById("bulkcount").textContent = n + " selected";
    document.getElementById("bulkmsg").textContent = "";
    var pa = document.getElementById("pickAll"); pa.checked = n === all; pa.indeterminate = n > 0 && n < all;
    document.querySelectorAll(".pick").forEach(function (c) { c.closest(".card").classList.toggle("picked", c.checked); });
  }
  function pickAll(on) {
    document.querySelectorAll(".pick").forEach(function (c) { c.checked = on; });
    pickChanged();
  }
  async function updateAll() {
    var items = [], noPrice = 0;
    picked().forEach(function (c) {
      var box = document.getElementById("sp-" + c.dataset.fi);
      var price = box ? parseFloat(box.value) : NaN;
      if (price > 0) items.push({fi_record_id: c.dataset.fi, price: price}); else noPrice++;
    });
    var msg = document.getElementById("bulkmsg");
    if (!items.length) { msg.textContent = "None of the ticked products has a price in its box."; return; }
    if (!confirm("Update UAE Price in Airtable for " + items.length + " products?" +
                 (noPrice ? " (" + noPrice + " without a price will be skipped)" : ""))) return;
    var btn = document.getElementById("bulkupdate"); btn.disabled = true; msg.textContent = "Updating Airtable...";
    try {
      var r = await fetch("/competitor/update-prices", {method: "POST", headers: {"Content-Type": "application/json"},
                                                       body: JSON.stringify({items: items})});
      var d = await r.json();
      if (d.updated) { msg.textContent = d.updated + " updated" + (d.error ? " | " + d.error : ""); }
      else throw new Error(d.error || ("HTTP " + r.status));
      setTimeout(function () { location.reload(); }, 900);     // show the new prices / least prices
    } catch (e) { msg.textContent = "Not updated: " + e.message; btn.disabled = false; }
  }

  // Suggestion "OK" / "Not OK": tell the server, then reload so the card shows the new match / no suggestion
  // ---- "Show out of stock" / "Hide out of stock" under each product ----
  function toggleOut(btn) {
    var list = btn.nextElementSibling, open = list.hidden;
    list.hidden = !open;
    btn.textContent = (open ? "Hide out of stock (" : "Show out of stock (") + btn.dataset.n + ")";
    console.log("[OUT-OF-STOCK] " + (open ? "shown" : "hidden") + ": " + btn.dataset.n + " competitor(s)");
  }

  async function suggestion(fi, shop, action, btn) {
    var msg = document.getElementById("sg-" + shop + "-" + fi);
    btn.disabled = true; msg.className = "upd-msg gtin"; msg.textContent = action === "ok" ? "Saving match..." : "Removing...";
    try {
      var r = await fetch("/competitor/suggestion", {method: "POST", headers: {"Content-Type": "application/json"},
                                                    body: JSON.stringify({fi_record_id: fi, shop: shop, action: action})});
      var d = await r.json();
      if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
      location.reload();                       // card is rebuilt with the confirmed match (scroll position is kept)
    } catch (e) {
      msg.className = "upd-msg out"; msg.textContent = "Not saved: " + e.message; btn.disabled = false;
    }
  }

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
    brand = request.args.get("brand", "").strip()           # Brand filter (shop tabs)
    shop_brands = []
    conds = read_filters(request.args)                     # Competitor table: Airtable-style conditions
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1
    print(f"[competitors] tab={src['key']} q={q!r} brand={brand!r} stock={stock!r} filters={conds} page={page}", flush=True)
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
            # "Missing item" = this competitor product is not linked to any of our products
            # (its link is not in competitor_table.<tab>_link, e.g. samawa_link, vp_link ...)
            missing_sql = None
            if src.get("kind") != "competitor":
                link_col = f"{src['key']}_link"
                cur.execute("SELECT 1 FROM information_schema.columns "
                            "WHERE table_name = 'competitor_table' AND column_name = %s", (link_col,))
                if cur.fetchone():
                    missing_sql = (f"NOT EXISTS (SELECT 1 FROM competitor_table ct "
                                   f"WHERE ct.{link_col} = {src['table']}.{c['url']})")
                else:
                    print(f"[MISSING] competitor_table has no {link_col} yet -> 'Missing item' not shown", flush=True)
            if src.get("kind") != "competitor":
                # ---- Brand filter ----
                # shops WITH a brand column (Samawa, Ounass, Amberiya): exact brand (any capitals)
                # shops WITHOUT one (French Fragrance, V Perfumes, Al Injaz ...): product name contains the brand
                if c.get("brand"):
                    cur.execute(f"SELECT DISTINCT {c['brand']} AS b FROM {src['table']} "
                                f"WHERE {c['brand']} IS NOT NULL AND {c['brand']} <> '' ORDER BY 1")
                else:
                    cur.execute("SELECT DISTINCT brand AS b FROM competitor_table "
                                "WHERE brand IS NOT NULL AND brand <> '' ORDER BY 1")
                shop_brands = [r["b"] for r in cur.fetchall()]
                print(f"[BRAND] {len(shop_brands)} brand suggestions for {src['label']} "
                      f"({'its brand column' if c.get('brand') else 'our brands - matched in the product name'})", flush=True)
                picked = [b.strip() for b in brand.split("||") if b.strip()]   # chips "Armaf||Afnan"
                if picked:
                    if c.get("brand"):                         # any of the picked brands (exact, any capitals)
                        where.append(f"LOWER({c['brand']}) = ANY(%s)")
                        params.append([b.lower() for b in picked])
                    else:                                      # name contains any of the picked brands
                        where.append("(" + " OR ".join(f"{c['name']} ILIKE %s" for _ in picked) + ")")
                        params += [f"%{b}%" for b in picked]
                    print(f"[BRAND] Filter {src['label']} by {len(picked)} brand(s): {', '.join(picked)}", flush=True)
            if stock == "missing":                          # dropdown option "Missing items"
                if missing_sql:
                    where.append(missing_sql)
                    print(f"[MISSING] Showing only missing items of {src['label']}", flush=True)
            elif stock and c.get("stock"):
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
            select += f", {missing_sql or 'FALSE'} AS missing"     # True -> card shows "Missing item"
            cur.execute(
                f"SELECT {select} FROM {src['table']} {where_sql} ORDER BY id LIMIT %s OFFSET %s",
                params + [PER_PAGE, (page - 1) * PER_PAGE],
            )
            rows = cur.fetchall()
            print(f"[competitors] {total} matching rows, showing {len(rows)} on page {page}/{pages}", flush=True)
            if missing_sql:
                print(f"[MISSING] {src['label']} page {page}: "
                      f"{sum(1 for r in rows if r['missing'])} of {len(rows)} are missing items", flush=True)
            if src.get("kind") == "competitor":
                add_next_cheapest(rows, src["shops"])
                # out-of-stock competitors are hidden on the card behind "Show out of stock"
                hidden = sum(1 for r in rows for _, k in src["shops"] if r.get(f"{k}_url") and not r.get(f"{k}_stock"))
                print(f"[OUT-OF-STOCK] page {page}: {hidden} out-of-stock competitor(s) hidden "
                      f"behind 'Show out of stock'", flush=True)
    finally:
        cur.close()
        conn.close()

    active = "competitor" if src["key"] == "competitor" else "shop"
    print(f"[MENU] Showing page '{src['label']}' (menu item: "
          f"{'Competitor table' if active == 'competitor' else 'Competitors'})", flush=True)
    return render_template_string(HTML, sources=SOURCES, src=src, counts=counts, rows=rows,
                                  nav_html=render_nav(active, src["key"], counts), nav_css=NAV_CSS,
                                  total=total, page=page, pages=pages, q=q, stock=stock, exists=exists,
                                  brands=brands, competitors=list(COMPETITORS), conds=conds,
                                  brand=brand, shop_brands=shop_brands,
                                  field_list=list(FILTER_FIELDS.items()), ops=FILTER_OPS,
                                  filt_labels=describe_filters(conds),
                                  filt_qs=urlencode([(k, x) for f, o, v in conds
                                                     for k, x in (("ff", f), ("fo", o), ("fv", v))]))

# ---------------------------------------------------------------- Settings (empty for now)
SETTINGS_HTML = """<!doctype html><html><head><title>Settings - Competitors</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#1c1b19;--card:#252420;--line:#3a3833;--text:#f2efe9;--muted:#a9a397;--gold:#d8b76e}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--text);font-family:Roboto,-apple-system,Segoe UI,Arial,sans-serif}
 .page{max-width:760px;margin:0 auto;padding:22px 18px 40px}
 h1{font-size:26px;margin:4px 0 18px}
 .topbar{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
 .who{color:var(--muted);font-size:14px} .who b{color:var(--text)} .who a{color:var(--gold)}
 .empty{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px;color:var(--muted);margin-top:16px}
 .empty b{color:var(--text);font-size:18px}
 .opts{display:grid;gap:12px;margin-top:16px}
 .opt{display:flex;flex-direction:column;gap:6px;background:var(--card);border:1px solid var(--line);border-radius:16px;
      padding:18px 20px;color:var(--text);text-decoration:none}
 .opt:hover{border-color:#f2d675}
 .opt b{font-size:18px} .opt span{color:var(--muted);font-size:14px}
 .back{display:inline-block;margin-top:14px;color:var(--gold)}
 .hint{color:var(--muted);font-size:14px;margin:10px 2px}
 .hint b{color:var(--text)}
 .dform{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px;margin-top:14px}
 .dform label{display:block;font-weight:600;margin-bottom:10px}
 .drow{display:flex;align-items:center;gap:8px}
 .drow input{width:130px;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:10px;
             padding:11px 12px;font-size:18px}
 .drow .pct{font-size:18px;color:var(--muted)}
 .drow button{margin-left:6px;background:#f2d675;color:#1c1b19;border:0;border-radius:10px;padding:11px 20px;
              font-size:16px;font-weight:700;cursor:pointer}
 .ok{color:#8cc58c;margin:10px 2px} .err{color:#e0806c;margin:10px 2px}
 .note{color:var(--muted);font-size:13px;border-top:1px solid var(--line);padding-top:12px;margin:14px 0 0}
 .bsearch{display:flex;gap:8px;margin-top:14px}
 .bsearch input{flex:1;min-width:0;background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:12px;
                padding:13px 14px;font-size:16px}
 .bsearch button{background:#f2d675;color:#1c1b19;border:0;border-radius:12px;padding:0 20px;font-size:16px;font-weight:600;cursor:pointer}
 .blist{background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden}
 .brow{display:flex;align-items:center;gap:12px;padding:11px 18px;border-top:1px solid var(--line)}
 .brow a{flex:1;color:var(--text);text-decoration:none}
 .brow a:hover{text-decoration:underline}
 .auto{background:#e3f5e1;color:#1c5a24;border-radius:999px;padding:2px 9px;font-size:12px;font-weight:700}
 .apick{width:20px;height:20px;accent-color:#f2d675;cursor:pointer;flex:0 0 auto}
 .bulkbar{position:fixed;left:50%;transform:translateX(-50%);bottom:16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;
          background:#0f2b3a;color:#fff;border-radius:14px;padding:12px 18px;box-shadow:0 8px 24px rgba(0,0,0,.45);z-index:50;max-width:92vw}
 .bulkbar[hidden]{display:none}
 .bulkcount{background:#2f7de1;border-radius:999px;padding:5px 12px;font-weight:600;font-size:14px}
 .bulkcancel{background:#fff;color:#1c1b19;border:0;border-radius:8px;padding:9px 16px;font-weight:600;cursor:pointer}
 .bulkupdate{background:#f2d675;color:#1c1b19;border:0;border-radius:8px;padding:9px 16px;font-weight:700;cursor:pointer}
 .bulkupdate:disabled{opacity:.6;cursor:wait}
 .bulkmsg{font-size:14px;color:#cfe3ff}
 .brow:first-child{border-top:0}
 .brow:hover{background:var(--bg)}
 .blist{margin-bottom:90px}
 .brow .n{color:var(--muted);font-variant-numeric:tabular-nums}
 /* ---- Daily competitor update box (Automate) ---- */
 .daily .drow{flex-wrap:wrap}
 .daily .drow input{font-size:16px;padding:9px 10px}
 .daily .drow button{padding:9px 16px;font-size:15px}
 .daily .drow select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:9px 10px;font-size:15px}
 .dshops{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px;color:var(--muted);margin:4px 2px}
 .dshops > span{white-space:nowrap}
 .okc{color:#8cc58c} .errc{color:#e0806c}
 /* ---- User management ---- */
 .urow{display:flex;gap:8px;flex-wrap:wrap}
 .urow input,.urow select,.inl input{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:10px;
            padding:10px 12px;font-size:15px}
 .urow input[name=username]{width:170px}
 .urow button[type=submit]{background:#f2d675;color:#1c1b19;border:0;border-radius:10px;padding:10px 18px;font-size:15px;font-weight:700;cursor:pointer}
 .pwwrap{position:relative;display:inline-block}
 .pwwrap input{padding-right:40px;width:180px}
 .eye{position:absolute;right:4px;top:50%;transform:translateY(-50%);background:none;border:0;font-size:17px;cursor:pointer;color:var(--muted)}
 .ulist{background:var(--card);border:1px solid var(--line);border-radius:16px;margin-top:16px;overflow:hidden}
 .urow2{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;padding:14px 18px;border-top:1px solid var(--line)}
 .urow2:first-child{border-top:0}
 .urow2.off .uname b{color:var(--muted);text-decoration:line-through}
 .umeta{color:var(--muted);font-size:13px;margin-top:4px}
 .role{border-radius:999px;padding:2px 9px;font-size:12px;font-weight:700;margin-left:6px}
 .role.admin{background:#f2d675;color:#1c1b19} .role.user{background:var(--line);color:var(--text)}
 .you{color:var(--gold);font-size:12px} .dis{color:#e0806c;font-size:12px;font-weight:700}
 .uact{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
 .inl{display:inline-flex;gap:6px;margin:0}
 .inl input{padding:8px 10px;font-size:14px}
 .inl .pwwrap input{width:150px;padding-right:36px}
 .sbtn{background:none;border:1px solid var(--line);color:var(--text);border-radius:8px;padding:8px 12px;font-size:14px;cursor:pointer}
 .sbtn:hover{border-color:var(--muted)} .sbtn.red{color:#e0806c}
{{ nav_css|safe }}
</style></head><body><div class="page">
<div class="topbar"><h1>{{ title }}</h1>
  {% if g.user %}<span class="who">Logged in as <b>{{ g.user }}</b> · <a href="/logout">Log out</a></span>{% endif %}</div>
{{ nav_html|safe }}
{% if option and option[0] == 'automate' %}
<a class="back" href="/settings">&larr; All settings</a>
{# ---- Daily + weekly competitor update (daily_update.py on the PC, Monday-Friday at 11:30 AM) ---- #}
<div class="dform daily">
  {% set st = daily.state %}
  <form method="post" action="/settings/daily" class="drow" onsubmit="console.log('[DAILY] Save: ' + this.per_day.value + ' brands/day, weekly day ' + this.weekly_day.value)">
    <label for="perday" style="margin:0">Daily update:</label>
    <input id="perday" name="per_day" type="number" min="1" max="100" step="1" value="{{ daily.per_day }}" style="width:80px">
    <span class="pct">brands a day &middot; full lists every</span>
    <select name="weekly_day">{% for d in daily.days_list %}<option value="{{ loop.index0 }}" {% if loop.index0 == daily.weekly_day %}selected{% endif %}>{{ d }}</option>{% endfor %}</select>
    <button type="submit">Save</button>
  </form>
  <p class="hint"><b>Monday-Friday 11:30 AM</b> (no update on Saturday / Sunday): French Fragrance, Branded Perfume, Essenzi &mdash; price + stock of ALL products of the
     next {{ daily.per_day }} brands of their own brand list{% if st and st.total_brands %} ({{ st.total_brands }} brands &rarr; one round every
     {{ (st.total_brands / daily.per_day)|round(0, 'ceil')|int }} working days){% endif %}, then again from A.<br>
     <b>Every {{ daily.days_list[daily.weekly_day] }}:</b> Samawa, Amberiya, V Perfumes, Al Injaz, Niche Gallerie, Ounass &mdash; whole
     product list (all brands), then matching.</p>
  {% if daily.saved %}<p class="ok">Saved (used from the next run).</p>{% endif %}
  {% if st and st.date %}
  <p class="hint"><b>Last daily run {{ st.date }}</b> &middot;
     {% if st.daily_status == 'done' %}<span class="okc">done{% if st.finished %} at {{ st.finished[11:16] }}{% endif %}</span>
     {% else %}<span class="errc">{{ st.daily_status or 'running' }}</span>{% endif %}<br>
     Brands: {{ (st.brands or [])|join(', ') }}</p>
  {% if st.shops %}<div class="dshops">
    {% for shop, r in st.shops.items() %}
    <span>{{ shop }}: {% if r.error %}<span class="errc">failed</span>{% else %}{{ r.checked }} checked, {{ r.changed }} changed{% if r.gone %}, {{ r.gone }} gone{% endif %}{% if r.errors %}, <span class="errc">{{ r.errors }} errors</span>{% endif %}{% endif %}</span>
    {% endfor %}</div>{% endif %}
  {% if st.next %}<p class="hint">Next: {{ st.next|join(', ') }}</p>{% endif %}
  {% else %}<p class="hint">Not run yet - the first daily run starts at brand A.</p>{% endif %}
  <p class="hint"><b>Last full lists:</b> {% if st and st.weekly_date %}{{ st.weekly_date }}
     {% elif st and st.weekly_status == 'running' %}<span class="errc">running now</span>{% else %}not run yet{% endif %}
  {% if st and st.weekly_results %}</p><div class="dshops">
    {% for shop, r in st.weekly_results.items() %}<span>{{ shop }}: {% if r.error %}<span class="errc">{{ r.error }}</span>{% else %}<span class="okc">done</span>{% endif %}</span>{% endfor %}
  </div>{% else %}</p>{% endif %}
</div>
{# Automate: search brands at the top, all brands with their product count below #}
<form class="bsearch" method="get" onsubmit="return true">
  <input type="search" name="q" id="bq" value="{{ q }}" placeholder="Search brands" autocomplete="off">
  <button type="submit">Search</button>
</form>
<p class="hint" id="bcount">{{ brands|length }} brand{{ '' if brands|length == 1 else 's' }}{% if q %} matching "{{ q }}"{% endif %}
   · {{ brands|sum(attribute='n') }} products</p>
<p class="hint">Tick a brand = <b>automatic</b>: its UAE Price in Airtable follows the suggested price (now, and after every
   price update). Untick = <b>manual</b>. Currently {{ auto_set|length }} brand{{ '' if auto_set|length == 1 else 's' }} on automatic.</p>
<div class="blist" id="blist">
  {% for b in brands %}
  <div class="brow" data-name="{{ b.brand|lower }}" data-n="{{ b.n }}">
    <a href="/competitors?tab=competitor&ff=brand&fo=anyof&fv={{ b.brand|urlencode }}" title="Open {{ b.brand }} in the competitor table">{{ b.brand }}</a>
    {% if b.brand|lower in auto_set %}<span class="auto">AUTO</span>{% endif %}
    <span class="n">{{ b.n }}</span>
    {# ticked = automatic. data-was = saved state, so only real changes are sent #}
    <input type="checkbox" class="apick" data-brand="{{ b.brand }}" data-was="{{ 1 if b.brand|lower in auto_set else 0 }}"
           {% if b.brand|lower in auto_set %}checked{% endif %} title="Automatic price update">
  </div>
  {% else %}
  <div class="empty">No brands found{% if q %} for "{{ q }}"{% endif %}.</div>
  {% endfor %}
</div>
<script>
  // live search: hides brands that don't contain the typed text (no page reload needed)
  (function () {
    var box = document.getElementById("bq"), rows = document.querySelectorAll("#blist .brow");
    var info = document.getElementById("bcount"), timer;
    console.log("[AUTOMATE] " + rows.length + " brands on the page");
    box.addEventListener("input", function () {
      var t = box.value.trim().toLowerCase(), shown = 0, products = 0;
      rows.forEach(function (r) {
        var ok = !t || r.dataset.name.indexOf(t) >= 0;
        r.style.display = ok ? "" : "none";
        if (ok) { shown++; products += +r.dataset.n; }
      });
      info.textContent = shown + " brand" + (shown === 1 ? "" : "s") + (t ? ' matching "' + box.value.trim() + '"' : "") +
                         " · " + products + " products";
      clearTimeout(timer);
      timer = setTimeout(function () { console.log("[AUTOMATE] Search '" + t + "' -> " + shown + " brands"); }, 400);
    });
    rows.forEach(function (r) {
      r.querySelector("a").addEventListener("click", function () {
        console.log("[AUTOMATE] Opening brand: " + r.querySelector("a").textContent);
      });
    });
  })();

  // ---- ticks: automatic / manual + "Update all" bar ----
  function changes() {
    var auto = [], manual = [];
    document.querySelectorAll(".apick").forEach(function (c) {
      var was = c.dataset.was === "1";
      if (c.checked && !was) auto.push(c.dataset.brand);        // newly ticked  -> automatic
      if (!c.checked && was) manual.push(c.dataset.brand);      // newly unticked -> manual
    });
    return {auto: auto, manual: manual};
  }
  function pickChanged(c) {
    console.log("[AUTOMATE] " + c.dataset.brand + (c.checked ? " ticked (automatic)" : " unticked (manual)"));
    var ch = changes(), n = ch.auto.length + ch.manual.length;
    document.getElementById("abar").hidden = n === 0;           // bar shows while there are unsaved changes
    document.getElementById("acount").textContent = n + " brand" + (n === 1 ? "" : "s") + " changed";
    document.getElementById("amsg").textContent = "";
  }
  document.querySelectorAll(".apick").forEach(function (c) { c.addEventListener("change", function () { pickChanged(c); }); });
  function cancelAuto() {
    document.querySelectorAll(".apick").forEach(function (c) { c.checked = c.dataset.was === "1"; });
    document.getElementById("abar").hidden = true;
    console.log("[AUTOMATE] Cancel -> ticks reset");
  }
  async function saveAuto() {
    var ch = changes(), msg = document.getElementById("amsg"), btn = document.getElementById("asave");
    var text = (ch.auto.length ? "AUTOMATIC: " + ch.auto.join(", ") + " (their UAE Price in Airtable will be set to the suggested price now). " : "") +
               (ch.manual.length ? "MANUAL: " + ch.manual.join(", ") : "");
    if (!confirm(text)) return;
    btn.disabled = true; msg.textContent = "Saving...";
    console.log("[AUTOMATE] Update all -> auto:", ch.auto, "manual:", ch.manual);
    try {
      var r = await fetch("/automate/save", {method: "POST", headers: {"Content-Type": "application/json"},
                                             body: JSON.stringify(ch)});
      var d = await r.json();
      if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
      msg.textContent = "Saved." + (d.to_update ? " Updating " + d.to_update + " products in Airtable in the background..." : "");
      console.log("[AUTOMATE] Saved:", d);
      setTimeout(function () { location.reload(); }, 1800);
    } catch (e) { msg.textContent = "Not saved: " + e.message; btn.disabled = false; }
  }
</script>
<div class="bulkbar" id="abar" hidden>
  <span class="bulkcount" id="acount">0 brands changed</span>
  <button type="button" class="bulkcancel" onclick="cancelAuto()">Cancel</button>
  <button type="button" class="bulkupdate" id="asave" onclick="saveAuto()">Update all</button>
  <span class="bulkmsg" id="amsg"></span>
</div>
{% elif option and option[0] == 'users' %}
<a class="back" href="/settings">&larr; All settings</a>
{# User management: admins add users, change password / role, disable or remove them (actions are in auth.py) #}
{% if msg %}<p class="ok">{{ msg }}</p>{% endif %}
{% if error %}<p class="err">{{ error }}</p>{% endif %}
{% if not admin %}
<div class="empty"><b>Only admins can manage users.</b><br>Ask an admin to add or change users.</div>
{% else %}
<form class="dform" method="post" action="/settings/users/add"
      onsubmit="console.log('[USERS] Add user: ' + this.username.value + ' (' + this.role.value + ')')">
  <label>Add a user</label>
  <div class="urow">
    <input name="username" placeholder="Username" required pattern="[A-Za-z0-9._-]{2,30}" autocomplete="off"
           title="2-30 characters: letters, numbers, . _ -">
    <span class="pwwrap"><input name="password" type="password" placeholder="Password" required minlength="{{ min_pw }}"
           autocomplete="new-password"><button type="button" class="eye" onclick="eye(this)" title="Show password">&#128065;</button></span>
    <select name="role"><option value="user">User</option><option value="admin">Admin</option></select>
    <button type="submit">Add user</button>
  </div>
  <p class="hint"><b>Admin</b> = everything + User management. <b>User</b> = everything except User management.</p>
</form>
<div class="ulist">
  {% for u in users %}
  <div class="urow2 {{ 'off' if not u.active else '' }}">
    <div class="uname"><b>{{ u.username }}</b>{% if u.username == me %} <span class="you">you</span>{% endif %}
      {% if u.main %}<span class="role admin" title="Main admin - protected, cannot be changed by other users">Main admin</span>{% else %}<span class="role {{ u.role }}">{{ u.role|capitalize }}</span>{% endif %}{% if not u.active %} <span class="dis">Disabled</span>{% endif %}{% if u.locked %} <span class="dis" title="Too many wrong passwords. Change password or wait to unlock.">Locked {{ u.locked }} min</span>{% endif %}
      <div class="umeta">{% if u.fixed %}Set in Render (USERS) - change it there
        {% else %}Added by {{ u.created_by or '-' }}{% if u.created_at %} on {{ u.created_at.strftime('%d %b %Y') }}{% endif %}
        · Last login {{ u.last_login.strftime('%d %b %Y, %H:%M') if u.last_login else 'never' }}{% endif %}</div>
    </div>
    {# main admin: other admins see no buttons; the main admin only sees Change (own password) #}
    {% if u.main and u.username != me %}<span class="umeta">Protected</span>
    {% elif not u.fixed %}
    <div class="uact">
      {# new password #}
      <form method="post" action="/settings/users/{{ u.username|urlencode }}/password" class="inl"
            onsubmit="console.log('[USERS] New password for {{ u.username }}')">
        <span class="pwwrap"><input name="password" type="password" placeholder="New password" required minlength="{{ min_pw }}"
               autocomplete="new-password"><button type="button" class="eye" onclick="eye(this)" title="Show password">&#128065;</button></span>
        <button type="submit" class="sbtn">Change</button>
      </form>
      {% if u.username != me %}
      {# role: admin <-> user #}
      <form method="post" action="/settings/users/{{ u.username|urlencode }}/role" class="inl"
            onsubmit="console.log('[USERS] Role of {{ u.username }} -> ' + this.role.value)">
        <input type="hidden" name="role" value="{{ 'user' if u.role == 'admin' else 'admin' }}">
        <button type="submit" class="sbtn">Make {{ 'user' if u.role == 'admin' else 'admin' }}</button>
      </form>
      {# disable / enable #}
      <form method="post" action="/settings/users/{{ u.username|urlencode }}/active" class="inl"
            onsubmit="console.log('[USERS] {{ 'Disable' if u.active else 'Enable' }} {{ u.username }}')">
        <input type="hidden" name="active" value="{{ '0' if u.active else '1' }}">
        <button type="submit" class="sbtn">{{ 'Disable' if u.active else 'Enable' }}</button>
      </form>
      {# remove for good #}
      <form method="post" action="/settings/users/{{ u.username|urlencode }}/delete" class="inl"
            onsubmit="if (!confirm('Remove user {{ u.username }}? They cannot log in any more.')) return false; console.log('[USERS] Remove {{ u.username }}')">
        <button type="submit" class="sbtn red">Remove</button>
      </form>
      {% endif %}
    </div>
    {% endif %}
  </div>
  {% endfor %}
</div>
<script>
  // eye button: show / hide the password in the box next to it
  function eye(b) {
    var i = b.previousElementSibling, show = i.type === "password";
    i.type = show ? "text" : "password";
    b.innerHTML = show ? "&#128584;" : "&#128065;";
  }
  console.log("[USERS] Page: {{ users|length }} users");
</script>
{% endif %}
{% elif option and option[0] == 'discount' %}
<a class="back" href="/settings">&larr; All settings</a>
{# Auto discount rate: admin writes the % (e.g. 2 or 5). Saved in table app_settings, key 'discount_rate'. #}
<form class="dform" method="post" action="/settings/discount"
      onsubmit="console.log('[DISCOUNT] Save clicked: ' + this.rate.value + '%')">
  <label for="rate">Discount below the cheapest in-stock competitor</label>
  <div class="drow">
    <input id="rate" name="rate" type="number" min="0" max="50" step="0.1" required
           value="{{ '%g' % rate if rate is not none else '' }}" placeholder="e.g. 5">
    <span class="pct">%</span>
    <button type="submit">Save</button>
  </div>
  {% if rate is not none %}<p class="hint">Example: cheapest competitor AED 100 &rarr; with {{ '%g' % rate }}% the suggested price is
     AED {{ '%g' % (100 - rate) }}.</p>{% endif %}
  {% if saved %}<p class="ok">Saved: {{ '%g' % rate }}%.</p>{% endif %}
  {% if error %}<p class="err">{{ error }}</p>{% endif %}
  {% if rate_info %}<p class="hint">Last changed by <b>{{ rate_info.updated_by or '-' }}</b> on {{ rate_info.updated_at.strftime('%d %b %Y, %H:%M') }}.</p>{% endif %}
  {% if rate is none %}<p class="err">No rate saved yet: suggested prices are not being calculated. Write a rate and Save.</p>{% endif %}
  <script>console.log("[DISCOUNT] Page: saved rate = {{ rate if rate is not none else 'NOT SET' }}{% if saved %}, just saved{% endif %}{% if recalc %}, recalc = {{ recalc }}{% endif %}");</script>
  {% if recalc == '1' %}<p class="ok">All suggested prices are being recalculated with the new rate (about 1 minute).
     Automatic brands are then updated in Airtable.</p>{% elif recalc == 'busy' %}<p class="hint">Another run is in progress -
     it will use the new rate when it reaches the price step.</p>{% endif %}
  <p class="note">This rate is used for every suggested price: on the competitor table, for Update price / Update all,
     and for the automatic brands. Saving recalculates all suggested prices.</p>
</form>
{% elif option %}
<a class="back" href="/settings">&larr; All settings</a>
<div class="empty"><b>{{ option[1] }}</b><br>{{ option[2] }}<br><br>Coming soon.</div>
{% else %}
<div class="opts">
  {% for key, label, about in options %}
  <a class="opt" href="/settings/{{ key }}"><b>{{ label }}</b><span>{{ about }}</span></a>
  {% endfor %}
</div>
{% endif %}
</div></body></html>"""


@competitors_bp.route("/settings")
@competitors_bp.route("/settings/<key>")
def settings_page(key=None):
    """Settings: /settings lists the 3 options, /settings/<key> opens one (empty for now)."""
    option = next((o for o in SETTINGS_OPTIONS if o[0] == key), None)
    if key and not option:
        print(f"[SETTINGS] Unknown option '{key}' -> showing all settings", flush=True)
    print(f"[SETTINGS] Opened: {option[1] if option else 'all settings'}", flush=True)
    counts, brands, auto_set = {}, [], set()
    rate, rate_info = None, None
    users = []
    daily = {}
    q = request.args.get("q", "").strip()
    try:
        conn = db_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            counts = tab_counts(cur)                 # numbers next to each shop in the Competitors menu
            if option and option[0] == "discount":
                rate_info = get_setting(cur, "discount_rate")
                rate = float(rate_info["value"]) if rate_info else None
                print(f"[DISCOUNT] Saved discount rate: {rate if rate is not None else 'NOT SET - suggested prices are not calculated'}", flush=True)
            if option and option[0] == "users":
                print(f"[USERS] User management opened by {g.get('user')} (admin: {is_admin()})", flush=True)
                users = list_users() if is_admin() else []
            if option and option[0] == "automate":
                daily = daily_info(cur)                           # Daily competitor update box
                daily["saved"] = request.args.get("daily_saved") == "1"
                # Automate: every brand of our products with how many products it has (search = brand contains q)
                sql = ("SELECT brand, COUNT(*) AS n FROM competitor_table WHERE brand IS NOT NULL AND brand <> ''"
                       + (" AND brand ILIKE %s" if q else "") + " GROUP BY brand ORDER BY LOWER(brand)")
                cur.execute(sql, [f"%{q}%"] if q else [])
                brands = cur.fetchall()
                cur.execute("SELECT to_regclass('brand_automation') AS t")
                if cur.fetchone()["t"]:
                    cur.execute("SELECT LOWER(brand) AS b FROM brand_automation WHERE auto")
                    auto_set = {r["b"] for r in cur.fetchall()}
                print(f"[AUTOMATE] {len(auto_set)} brands on automatic", flush=True)
                print(f"[AUTOMATE] Search {q!r}: {len(brands)} brands, "
                      f"{sum(b['n'] for b in brands)} products", flush=True)
        finally:
            cur.close()
            conn.close()
    except Exception as ex:
        print(f"[competitors] settings: counts not loaded ({ex})", flush=True)
    print(f"[MENU] Settings page shown to {g.get('user') or 'unknown user'}", flush=True)
    return render_template_string(SETTINGS_HTML, nav_html=render_nav("settings", option[0] if option else None, counts),
                                  nav_css=NAV_CSS, option=option, options=SETTINGS_OPTIONS, brands=brands, q=q, auto_set=auto_set,
                                  rate=rate, rate_info=rate_info, saved=request.args.get("saved") == "1",
                                  error=request.args.get("error"), recalc=request.args.get("recalc"),
                                  daily=daily, users=users, admin=is_admin(), me=g.get("user"), msg=request.args.get("msg"),
                                  min_pw=MIN_PASSWORD, title=option[1] if option else "Settings")


# ---------------------------------------------------------------- app settings (values the admin changes on the page)
SETTINGS_CREATE = """
CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,             -- e.g. 'discount_rate'
    value       TEXT,                         -- e.g. '5'  (percent)
    updated_by  TEXT,                         -- user who saved it
    updated_at  TIMESTAMP DEFAULT NOW()
);
"""


DAILY_DEFAULT = 10      # brands per day when nothing is saved (same as daily_update.py)
WEEKLY_DEFAULT = 4      # full lists day: 0 = Monday ... 4 = Friday (same as daily_update.py; PC is off at weekends)
DAYS_LIST = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]          # working days only


def daily_info(cur):
    """Daily / weekly update box: settings + what daily_update.py saved (last run, results, next brands)."""
    import json
    def number(key, default, lo, hi):
        row = get_setting(cur, key)
        try:
            v = int(float(row["value"])) if row else default
        except ValueError:
            v = default
        return v if lo <= v <= hi else default
    row = get_setting(cur, "daily_update")
    state = json.loads(row["value"]) if row and row["value"] else None
    info = {"per_day": number("daily_brands_per_day", DAILY_DEFAULT, 1, 100),
            "weekly_day": number("daily_weekly_day", WEEKLY_DEFAULT, 0, 4), "days_list": DAYS_LIST, "state": state}
    print(f"[DAILY] Box: {info['per_day']} brands/day, full lists every {DAYS_LIST[info['weekly_day']]}, last daily "
          f"{(state or {}).get('date', 'never')} ({(state or {}).get('daily_status', '-')}), last full lists "
          f"{(state or {}).get('weekly_date', 'never')}", flush=True)
    return info


@competitors_bp.route("/settings/daily", methods=["POST"])
def daily_save():
    """Save brands per day (1 - 100) and the weekly full-list day (0 = Monday ... 4 = Friday)."""
    user = g.get("user") or "unknown"
    try:
        n = int(float(request.form.get("per_day") or ""))
        day = int(request.form.get("weekly_day") or WEEKLY_DEFAULT)
    except ValueError:
        n, day = 0, -1
    if not 1 <= n <= 100 or not 0 <= day <= 4:
        print(f"[DAILY] {user}: per_day={request.form.get('per_day')!r} weekly_day={request.form.get('weekly_day')!r} "
              f"not valid -> not saved", flush=True)
        return redirect("/settings/automate")
    conn = db_conn()
    cur = conn.cursor()
    try:
        cur.execute(SETTINGS_CREATE)
        for key, value in (("daily_brands_per_day", n), ("daily_weekly_day", day)):
            cur.execute("""INSERT INTO app_settings (key, value, updated_by) VALUES (%s, %s, %s)
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                                                           updated_at = NOW()""", (key, str(value), user))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    print(f"[DAILY] {user} saved: {n} brands a day, full lists every {DAYS_LIST[day]}", flush=True)
    return redirect("/settings/automate?daily_saved=1")


def get_setting(cur, key):
    """One saved setting as a dict {value, updated_by, updated_at}, or None when it was never saved."""
    cur.execute(SETTINGS_CREATE)
    cur.execute("SELECT value, updated_by, updated_at FROM app_settings WHERE key = %s", (key,))
    row = cur.fetchone()
    print(f"[SETTINGS] Read '{key}': {dict(row) if row else 'not saved yet'}", flush=True)
    return row


@competitors_bp.route("/settings/discount", methods=["POST"])
def discount_save():
    """Save the Auto discount rate written by the admin (0 - 50 %)."""
    raw = (request.form.get("rate") or "").strip().replace("%", "")
    user = g.get("user") or "unknown"
    print(f"[DISCOUNT] {user} wants to save discount rate {raw!r}", flush=True)
    try:
        rate = round(float(raw), 2)
    except ValueError:
        print("[DISCOUNT] Not a number -> not saved", flush=True)
        return redirect("/settings/discount?error=Please+write+a+number,+e.g.+5")
    if not 0 <= rate <= 50:
        print(f"[DISCOUNT] {rate} is outside 0-50 -> not saved", flush=True)
        return redirect("/settings/discount?error=The+rate+must+be+between+0+and+50")
    conn = db_conn()
    cur = conn.cursor()
    try:
        cur.execute(SETTINGS_CREATE)
        cur.execute("""INSERT INTO app_settings (key, value, updated_by) VALUES ('discount_rate', %s, %s)
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                                                       updated_at = NOW()""", (str(rate), user))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    print(f"[DISCOUNT] Saved discount rate {rate}% by {user}", flush=True)
    if not ON_DISCOUNT_CHANGE:
        print("[DISCOUNT] No recalculation hook set (app.py not loaded?) -> prices update at the next run", flush=True)
    started = ON_DISCOUNT_CHANGE() if ON_DISCOUNT_CHANGE else False    # app.py: recalculate suggested prices now
    print(f"[DISCOUNT] Recalculation {'started now' if started else 'not started now (another run busy)'}", flush=True)
    return redirect("/settings/discount?saved=1&recalc=" + ("1" if started else "busy"))


# Set by app.py: function that recalculates all suggested prices after a new rate is saved
ON_DISCOUNT_CHANGE = None


def get_discount_rate(cur):
    """Auto discount rate in percent (e.g. 2.5), or None when the admin never saved one.
    Works with any cursor (app.py uses plain tuples, this page uses dict rows)."""
    print("[DISCOUNT] get_discount_rate(): reading 'discount_rate' from app_settings", flush=True)
    cur.execute(SETTINGS_CREATE)
    cur.execute("SELECT value FROM app_settings WHERE key = 'discount_rate'")
    row = cur.fetchone()
    value = (row["value"] if isinstance(row, dict) else row[0]) if row else None
    try:
        rate = float(value) if value not in (None, "") else None
    except ValueError:
        print(f"[DISCOUNT] Saved value {value!r} is not a number -> treated as NOT SET", flush=True)
        rate = None
    print(f"[DISCOUNT] Using Auto discount rate: {rate if rate is not None else 'NOT SET'}", flush=True)
    return rate
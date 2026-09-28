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
from flask import Blueprint, request, render_template_string

competitors_bp = Blueprint("competitors", __name__)

PER_PAGE = 50

# ---------------------------------------------------------------- tabs
# Each "cols" entry is the SQL used for that field (None = column not available for this source)
SOURCES = [
    {"key": "competitor", "label": "Competitor table", "table": None},   # coming later
    {"key": "samawa", "label": "Samawa", "table": "samawa_catalog",
     "cols": {"name": "name", "brand": "brand", "gtin": "gtin", "url": "product_url",
              "price": "price", "stock": "stock", "volume": "volume", "updated": "updated_at"}},
    {"key": "ff", "label": "French Fragrance", "table": "french_fragrance_catalog",
     "cols": {"name": "name", "brand": None, "gtin": "gtin", "url": "product_url",
              "price": "COALESCE(price_inc_tax, price)", "stock": "stock", "volume": "volume",
              "updated": "updated_at"}},
    # {"key": "shop3", "label": "Shop 3", "table": "shop3_catalog", "cols": {...}},   # add more here
]
SOURCE_BY_KEY = {s["key"]: s for s in SOURCES}


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
 .tab{flex:0 0 auto;padding:11px 16px;border-radius:12px;color:var(--muted);text-decoration:none;white-space:nowrap;font-size:16px}
 .tab.on{background:var(--bg);color:var(--text);font-weight:600}
 .bar{display:flex;gap:8px;margin:14px 0 6px}
 .bar input,.bar select{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:12px;
       padding:13px 14px;font-size:16px}
 .bar input{flex:1;min-width:0}
 .bar button{background:var(--goldbtn);color:#1c1b19;border:0;border-radius:12px;padding:0 18px;font-size:16px;font-weight:600}
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
 .pbtn{background:var(--goldbtn);color:#1c1b19;text-decoration:none;border-radius:12px;padding:12px 18px;font-weight:600}
 .pbtn.off{visibility:hidden}
 :focus-visible{outline:2px solid var(--gold);outline-offset:2px}
</style></head><body><div class="page">

<h1>Saved data</h1>

<nav class="tabs">
  {% for s in sources %}
  <a class="tab {% if s.key == src.key %}on{% endif %}" href="?tab={{ s.key }}">{{ s.label }} {{ counts[s.key] }}</a>
  {% endfor %}
</nav>

{% if not src.table %}
  <p class="hint"></p>
  <div class="empty">The competitor table isn't set up yet. It will show matched prices from every shop side by side.</div>
{% elif not exists %}
  <p class="hint"></p>
  <div class="empty">No {{ src.label }} products yet. Run the download for this shop first.</div>
{% else %}
  <form class="bar" method="get">
    <input type="hidden" name="tab" value="{{ src.key }}">
    <input type="text" name="q" value="{{ q }}" placeholder="Filter by {% if src.cols.brand %}brand, {% endif %}name, GTIN">
    <select name="stock">
      <option value="" {% if not stock %}selected{% endif %}>All</option>
      <option value="In stock" {% if stock=='In stock' %}selected{% endif %}>In stock</option>
      <option value="Out of stock" {% if stock=='Out of stock' %}selected{% endif %}>Out of stock</option>
    </select>
    <button type="submit">Go</button>
  </form>
  <p class="hint">{{ total }} {{ 'product' if total == 1 else 'products' }}{% if q or stock %} match this filter{% endif %}.</p>

  {% for r in rows %}
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
    <div class="date">Updated {{ r.updated.strftime('%d %b, %H:%M') if r.updated else '' }}</div>
  </div>
  {% else %}
  <div class="empty">No products match this filter. Clear the filter to see everything.</div>
  {% endfor %}

  <div class="pager">
    <a class="pbtn {% if page <= 1 %}off{% endif %}" href="?tab={{ src.key }}&q={{ q|urlencode }}&stock={{ stock|urlencode }}&page={{ page-1 }}">Previous</a>
    <span class="gtin">Page {{ page }} of {{ pages }}</span>
    <a class="pbtn {% if page >= pages %}off{% endif %}" href="?tab={{ src.key }}&q={{ q|urlencode }}&stock={{ stock|urlencode }}&page={{ page+1 }}">Next</a>
  </div>
{% endif %}

</div></body></html>
"""


@competitors_bp.route("/competitors")
def competitors():
    tab = request.args.get("tab", "samawa")
    src = SOURCE_BY_KEY.get(tab, SOURCES[1])
    q = request.args.get("q", "").strip()
    stock = request.args.get("stock", "").strip()
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1
    print(f"[competitors] tab={src['key']} q={q!r} stock={stock!r} page={page}", flush=True)

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
                search_cols = [col for col in (c["name"], c["brand"], c["gtin"]) if col]
                where.append("(" + " OR ".join(f"{col} ILIKE %s" for col in search_cols) + ")")
                params += [f"%{q}%"] * len(search_cols)
            if stock:
                where.append(f"{c['stock']} = %s")
                params.append(stock)
            where_sql = ("WHERE " + " AND ".join(where)) if where else ""

            cur.execute(f"SELECT COUNT(*) AS c FROM {src['table']} {where_sql}", params)
            total = cur.fetchone()["c"]
            pages = max(math.ceil(total / PER_PAGE), 1)
            page = min(page, pages)

            select = ", ".join(f"{expr or 'NULL'} AS {alias}" for alias, expr in c.items())
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
                                  total=total, page=page, pages=pages, q=q, stock=stock, exists=exists)
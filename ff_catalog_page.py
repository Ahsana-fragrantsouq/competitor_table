"""
French Fragrance Catalog page (reads Postgres french_fragrance_db).
Plugged into app.py as a Blueprint -> open /ff-catalog

Env var:
  FF_DATABASE_URL = Postgres URL ending in /french_fragrance_db
"""

import os
import math
import psycopg2
import psycopg2.extras
from flask import Blueprint, request, render_template_string

ff_catalog_bp = Blueprint("ff_catalog", __name__)

FF_PER_PAGE = 100


def ff_db_conn():
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    print("[ff_catalog] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


FF_HTML = """
<!doctype html><html><head><title>French Fragrance Catalog</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font-family:Arial,sans-serif;margin:30px 38px;color:#111}
 h1{font-size:40px;margin:10px 0}
 h2{font-size:30px;margin:24px 0 16px}
 .bar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
 .bar input,.bar select{padding:9px;font-size:15px;border:1px solid #ccc;border-radius:4px}
 .bar input[type=text]{width:320px}
  .btn{background:#f2d675;color:#1c1b19;border:0;padding:10px 18px;border-radius:4px;cursor:pointer;font-size:15px;text-decoration:none;font-weight:600}
 .wrap{overflow-x:auto}
 table{border-collapse:collapse;width:100%;font-size:15px}
 th,td{border:1px solid #ddd;padding:8px;text-align:left;vertical-align:middle}
 th{background:#f4f4f4;white-space:nowrap}
 td.name{min-width:260px}
 .in{color:#070} .out{color:#b00}
 .pager{margin:18px 0;display:flex;gap:10px;align-items:center}
</style></head><body>
<h1>Saved Data</h1>
<h2>French Fragrance Catalog ({{ total }})</h2>

<form class="bar" method="get">
  <input type="text" name="q" value="{{ q }}" placeholder="Search name or GTIN">
  <select name="stock">
    <option value="" {% if not stock %}selected{% endif %}>All stock</option>
    <option value="In stock" {% if stock=='In stock' %}selected{% endif %}>In stock</option>
    <option value="Out of stock" {% if stock=='Out of stock' %}selected{% endif %}>Out of stock</option>
  </select>
  <button class="btn" type="submit">Search</button>
  <a class="btn" href="{{ request.path }}">Clear</a>
</form>

<div class="wrap">
<table>
  <tr>
    <th>id</th><th>product_url</th><th>gtin</th><th>name</th>
    <th>price_inc_tax</th><th>price</th><th>stock</th><th>volume</th><th>updated_at</th>
  </tr>
  {% for r in rows %}
  <tr>
    <td>{{ r.id }}</td>
    <td>{% if r.product_url %}<a href="{{ r.product_url }}" target="_blank">View product</a>{% endif %}</td>
    <td>{{ r.gtin or '' }}</td>
    <td class="name">{{ r.name or '' }}</td>
    <td>{% if r.price_inc_tax is not none %}AED {{ '%.2f' % r.price_inc_tax }}{% endif %}</td>
    <td>{% if r.price is not none %}AED {{ '%.2f' % r.price }}{% endif %}</td>
    <td class="{{ 'in' if r.stock=='In stock' else 'out' }}">{{ r.stock or '' }}</td>
    <td>{{ r.volume or '' }}</td>
    <td>{{ r.updated_at }}</td>
  </tr>
  {% else %}
  <tr><td colspan="9">No products match this search. Clear the search to see all products.</td></tr>
  {% endfor %}
</table>
</div>

<div class="pager">
  {% if page > 1 %}<a class="btn" href="?q={{ q }}&stock={{ stock }}&page={{ page-1 }}">Previous</a>{% endif %}
  <span>Page {{ page }} of {{ pages }}</span>
  {% if page < pages %}<a class="btn" href="?q={{ q }}&stock={{ stock }}&page={{ page+1 }}">Next</a>{% endif %}
</div>
</body></html>
"""


@ff_catalog_bp.route("/ff-catalog")
def ff_catalog():
    q = request.args.get("q", "").strip()
    stock = request.args.get("stock", "").strip()
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1
    print(f"[ff_catalog] Search q={q!r} stock={stock!r} page={page}")

    where, params = [], []
    if q:
        where.append("(name ILIKE %s OR gtin ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    if stock:
        where.append("stock = %s")
        params.append(stock)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = ff_db_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(f"SELECT COUNT(*) AS c FROM french_fragrance_catalog {where_sql}", params)
        total = cur.fetchone()["c"]
        pages = max(math.ceil(total / FF_PER_PAGE), 1)
        page = min(page, pages)
        print(f"[ff_catalog] {total} matching rows, {pages} pages")

        cur.execute(
            f"""SELECT id, product_url, gtin, name, price_inc_tax, price, stock, volume, updated_at
                FROM french_fragrance_catalog {where_sql}
                ORDER BY id LIMIT %s OFFSET %s""",
            params + [FF_PER_PAGE, (page - 1) * FF_PER_PAGE],
        )
        rows = cur.fetchall()
        print(f"[ff_catalog] Loaded {len(rows)} rows for page {page}")
    finally:
        cur.close()
        conn.close()

    return render_template_string(FF_HTML, rows=rows, total=total, page=page,
                                  pages=pages, q=q, stock=stock)
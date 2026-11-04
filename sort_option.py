"""
Sort option  (like Airtable's "Sort by")  - Competitor table + Item Master
=========================================================================
Two drop-downs next to the search box: Sort by  [SKU | Brand | Perfume name | Product name]  and  [A -> Z | Z -> A].
Picking one reloads the page (search / filters kept); Previous / Next keep the sort (sort_qs).
URL: ?sort=sku|brand|perfume|name&dir=asc|desc   (no sort = the page's normal order)

Where things are:
  SECTION 1  settings -> SORT_FIELDS (key, label)
  SECTION 2  helpers  -> read_sort(), order_sql(), sort_qs()
  SECTION 3  page part -> SORT_HTML (Jinja: needs sort_fields, sort, sort_dir), SORT_CSS
"""

from urllib.parse import urlencode


def log(*a):
    """Print to the Render log (lines start with [SORT])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
SORT_FIELDS = [("sku", "SKU"), ("brand", "Brand"), ("perfume", "Perfume name"), ("name", "Product name")]
SORT_KEYS = {k for k, _ in SORT_FIELDS}


# ============================================================ SECTION 2: helpers
def read_sort(args):
    """URL -> (sort key or '', 'asc' / 'desc'). Unknown values are ignored."""
    key = (args.get("sort") or "").strip()
    key = key if key in SORT_KEYS else ""
    direction = "desc" if (args.get("dir") or "").lower() == "desc" else "asc"
    return key, direction


def order_sql(key, direction, exprs, default):
    """ORDER BY text. exprs = {sort key: SQL expression of that page}; empty values sort LAST either way.
    No / unknown sort -> default (the page's normal order)."""
    if not key or key not in exprs:
        return default
    d = "DESC" if direction == "desc" else "ASC"
    log(f"[SORT] by {key} {d}")
    return f"LOWER(TRIM(COALESCE({exprs[key]}, ''))) = '' ASC, LOWER({exprs[key]}) {d}, {default}"


def sort_qs(key, direction):
    """'&sort=..&dir=..' for page links ('' when no sort)."""
    return ("&" + urlencode({"sort": key, "dir": direction})) if key else ""


# ============================================================ SECTION 3: page part
SORT_CSS = """ /* Sort by (sort_option.py) */
 .sortbox{display:inline-flex;align-items:center;gap:6px;font-size:14px;color:var(--muted)}
 .sortbox select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:9px 10px;font-size:14px}
"""

# inside the page's search <form method="get">: a change submits the form (search / filters stay)
SORT_HTML = """<span class="sortbox">Sort by
      <select name="sort" onchange="console.log('[SORT] ' + this.value); this.form.submit()">
        <option value="" {% if not sort %}selected{% endif %}>-</option>
        {% for k, label in sort_fields %}<option value="{{ k }}" {% if sort == k %}selected{% endif %}>{{ label }}</option>{% endfor %}
      </select>
      <select name="dir" onchange="this.form.submit()">
        <option value="asc" {% if sort_dir != 'desc' %}selected{% endif %}>A &rarr; Z</option>
        <option value="desc" {% if sort_dir == 'desc' %}selected{% endif %}>Z &rarr; A</option>
      </select></span>"""

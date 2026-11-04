"""
Sort option  (like Airtable's "Sort" box)  - Competitor table + Item Master
==========================================================================
A "Sort" button next to the search box opens a small box like Airtable:
    Sort by
    [SKU          v]  [A -> Z v]   x        <- one row per sort level (first row = first sort)
    + Add another sort
Every change is applied at once (like Airtable "Automatically sort records"): the page reloads, search / filters kept.
x removes that level. The button then says "Sorted by 1 field" / "Sorted by 2 fields".
URL: ?sort=sku&dir=asc&sort=brand&dir=desc ...  (sort = sku|brand|perfume|name, max 4 levels; none = normal order)
Previous / Next keep the sort (sort_qs).

Where things are:
  SECTION 1  settings  -> SORT_FIELDS (key, label), MAX_LEVELS
  SECTION 2  helpers   -> read_sort(), order_sql(), sort_qs()
  SECTION 3  page part -> SORT_HTML (Jinja: needs sorts, sort_fields), SORT_CSS, SORT_JS
"""

from urllib.parse import urlencode


def log(*a):
    """Print to the Render log (lines start with [SORT])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
SORT_FIELDS = [("sku", "SKU"), ("brand", "Brand"), ("perfume", "Perfume name"), ("name", "Product name")]
SORT_KEYS = {k for k, _ in SORT_FIELDS}
MAX_LEVELS = 4


# ============================================================ SECTION 2: helpers
def read_sort(args):
    """URL -> [(key, 'asc'/'desc'), ...] in order; unknown / repeated fields are dropped."""
    keys, dirs = args.getlist("sort"), args.getlist("dir")
    out, seen = [], set()
    for i, k in enumerate(keys):
        k = (k or "").strip()
        if k in SORT_KEYS and k not in seen:
            d = "desc" if (dirs[i] if i < len(dirs) else "").lower() == "desc" else "asc"
            out.append((k, d))
            seen.add(k)
    return out[:MAX_LEVELS]


def order_sql(sorts, exprs, default):
    """ORDER BY text for all levels. exprs = {key: SQL expression}; per level empty values go LAST; capitals ignored.
    No sort -> default (the page's normal order)."""
    parts = []
    for k, d in sorts:
        if k in exprs:
            e = exprs[k]
            parts += [f"(TRIM(COALESCE({e}, '')) = '') ASC", f"LOWER({e}) {'DESC' if d == 'desc' else 'ASC'}"]
    if not parts:
        return default
    log(f"[SORT] by " + ", ".join(f"{k} {d}" for k, d in sorts))
    return ", ".join(parts + [default])


def sort_qs(sorts):
    """'&sort=..&dir=..' (all levels) for page links; '' when no sort."""
    return ("&" + urlencode([p for k, d in sorts for p in (("sort", k), ("dir", d))])) if sorts else ""


# ============================================================ SECTION 3: page part
SORT_CSS = """ /* Sort box like Airtable (sort_option.py) */
 .sortwrap{position:relative;display:inline-block}
 .sortbtn{background:none;color:var(--text);border:1px solid var(--line);border-radius:10px;padding:10px 14px;font-size:14px;cursor:pointer}
 .sortbtn.on{background:#f6d6c6;color:#1c1b19;border-color:#f6d6c6}
 .sortbox{position:absolute;z-index:30;top:calc(100% + 6px);left:0;min-width:360px;background:var(--card,#262421);
          border:1px solid var(--line);border-radius:12px;padding:14px;box-shadow:0 8px 24px rgba(0,0,0,.35)}
 .sortbox[hidden]{display:none}
 .sortbox .stitle{color:var(--muted);font-size:13px;margin-bottom:10px}
 .sortrow{display:flex;gap:8px;align-items:center;margin-bottom:8px}
 .sortrow select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:14px}
 .sortrow select.sfield{flex:1}
 /* !important: pages style every form button (Item Master = big yellow) -> keep these plain like Airtable */
 .sortbox .sortadd{background:none !important;border:0 !important;color:var(--muted) !important;font-size:14px !important;
                   font-weight:400 !important;cursor:pointer;padding:6px 0 !important;box-shadow:none !important}
 .sortbox .sdel{background:none !important;border:0 !important;color:var(--muted) !important;padding:0 4px !important;
                font-size:18px !important;font-weight:400 !important}
 @media (max-width:640px){ .sortbox{min-width:0;width:calc(100vw - 40px)} }
"""

# inside the page's search <form method="get"> (the selects are sent with the form)
SORT_HTML = """<span class="sortwrap">
      <button type="button" class="sortbtn {% if sorts %}on{% endif %}" onclick="sortToggle(this)">&#8645;
        {% if sorts %}Sorted by {{ sorts|length }} field{% if sorts|length > 1 %}s{% endif %}{% else %}Sort{% endif %}</button>
      <div class="sortbox" id="sortbox" hidden>
        <div class="stitle">Sort by</div>
        <div id="sortrows">
        {% for k, d in (sorts or [('', 'asc')]) %}
          <div class="sortrow">
            <select class="sfield" name="sort" onchange="sortApply(this.form)">
              {% if not k %}<option value="" selected>Pick a field</option>{% endif %}
              {% for fk, label in sort_fields %}<option value="{{ fk }}" {% if fk == k %}selected{% endif %}>{{ label }}</option>{% endfor %}
            </select>
            <select name="dir" onchange="sortApply(this.form)">
              <option value="asc" {% if d != 'desc' %}selected{% endif %}>A &rarr; Z</option>
              <option value="desc" {% if d == 'desc' %}selected{% endif %}>Z &rarr; A</option>
            </select>
            <button type="button" class="sdel" title="Remove this sort" onclick="sortRemove(this)">&times;</button>
          </div>
        {% endfor %}
        </div>
        {% if sorts and sorts|length < 4 %}
        <button type="button" class="sortadd" onclick="sortAdd()">+ Add another sort</button>
        {% endif %}
      </div>
    </span>"""

SORT_JS = """  // ---- Sort box like Airtable (sort_option.py): every change re-sorts at once ----
  function sortToggle(btn) {
    var box = document.getElementById("sortbox"); box.hidden = !box.hidden;
  }
  function sortApply(form) {
    // rows without a field are not sent
    form.querySelectorAll(".sortrow").forEach(function (r) {
      var f = r.querySelector("select.sfield");
      if (!f.value) { f.disabled = true; r.querySelector("select[name=dir]").disabled = true; }
    });
    var p = form.querySelector("input[name=page]"); if (p) p.remove();          // back to page 1
    console.log("[SORT] apply");
    form.submit();
  }
  function sortRemove(btn) {
    var row = btn.closest(".sortrow"), form = row.closest("form");
    row.remove();
    console.log("[SORT] removed a sort");
    sortApply(form);
  }
  function sortAdd() {
    // new row = first field not used yet, A -> Z
    var rows = document.getElementById("sortrows"), used = [];
    rows.querySelectorAll("select.sfield").forEach(function (s) { used.push(s.value); });
    var row = rows.querySelector(".sortrow").cloneNode(true), f = row.querySelector("select.sfield");
    var free = Array.prototype.find.call(f.options, function (o) { return o.value && used.indexOf(o.value) < 0; });
    if (!free) return;
    f.value = free.value; row.querySelector("select[name=dir]").value = "asc";
    rows.appendChild(row);
    console.log("[SORT] added " + free.value);
    sortApply(rows.closest("form"));
  }
  document.addEventListener("click", function (e) {           // click outside closes the box
    var box = document.getElementById("sortbox");
    if (box && !box.hidden && !e.target.closest(".sortwrap")) box.hidden = true;
  });
"""

"""
Add new link  (ADMIN ONLY - "Add new link" at the bottom right of every product card on the Competitor table)
===========================================================================================================
For a competitor product the matching did not find. Paste its product link -> Save:
  1. the shop is found from the link (NL_HOSTS) - or picked in the box
  2. link already in that shop's saved product list -> used as it is
     else the product is READ from the link (SECTION 3) and SAVED INTO THAT SHOP'S LIST (e.g. amberiya_catalog):
       Shopify shops   -> <link>.js  (name, brand, AED price, stock of the chosen size / ?variant=)
       Al Jazeera      -> its El-Dokan API (aljazeera.py)
       other shops     -> the product page's schema.org data (brandsperfume.py br_parse)
       site blocks us  -> the box asks for name / price / stock and saves what the admin types
  3. the link is put on OUR product, CONFIRMED (link_edit.set_link: later match runs keep it) and least + suggested
     price are recalculated at once (French Fragrance -> Branded Perfume / Essenzi follow).
Branded Perfume / Essenzi have no own list (they follow French Fragrance) -> not offered.
A shop's next full download may refresh (or mark 'Not on site') a row added here, like any other row of its list.

Where things are (change them here):
  SECTION 1  settings      -> NL_URL, NL_HOSTS (web address -> shop), NL_SHOPS (shops offered)
  SECTION 2  card parts    -> NL_CSS, NL_HTML (button + box), NL_JS (Save / Cancel / manual fields)
  SECTION 3  read product  -> nl_read(): Shopify .js / Al Jazeera API / page data
  SECTION 4  save          -> nl_save_catalog() (into the shop's list) + POST /competitor/new-link
"""

import re
import json
import zlib
from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode

import requests
from flask import Blueprint, request, jsonify, g

import link_edit                                    # SHOP_LISTS, HOOKS (database, recalculation), set_link
import brandsperfume as br                          # br_get, br_parse, br_num, br_volume

new_link_bp = Blueprint("new_link", __name__)


def log(*a):
    """Print to the Render log (lines start with [NEW-LINK])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
NL_URL = "/competitor/new-link"
# part of the web address -> shop prefix (first match wins)
NL_HOSTS = [("samawa.ae", "samawa"), ("frenchfragrance.com", "ff"), ("vperfumes.com", "vp"),
            ("alinjazperfumes.com", "aj"), ("ounass.ae", "on"), ("amberiya.ae", "am"), ("nichegallerie.com", "ng"),
            ("perfumedubai.com", "pd"), ("mhgboutique.com", "mh"), ("emiratespride.com", "ep"), ("anfasic.ae", "an"),
            ("khaltat.com", "kh"), ("oudelite.com", "oe"), ("brandsperfume.com", "br"),
            ("abdulsamadalqurashi.com", "aq"), ("arabianoud.com", "ao"), ("aljazeeraperfumes.com", "jz"),
            ("ajmal.com", "jm"),
            ("alharamainperfumes.com", "ah")]
NL_SHOPS = [k for k in link_edit.SHOP_LISTS if k not in ("bp", "es")]     # BP / ES follow French Fragrance
NL_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124 Safari/537.36", "Accept": "application/json"}


# tracking parts that Google / ads add to a pasted link ("?srsltid=..."): cut off, so the link is found in the
# shop's saved list and saved clean (live case 2026-11-03: Al Injaz link from Google with ?srsltid=AYCX9...)
NL_TRACKING = ("srsltid", "gclid", "gbraid", "wbraid", "fbclid", "msclkid", "_pos", "_sid", "_ss", "_psq", "_v",
               "ref", "mc_cid", "mc_eid")


def nl_clean_url(url):
    """Link without tracking parts (utm_*, srsltid ...) and #...; other parts (?variant=) kept."""
    parts = urlsplit(url.strip())
    keep = [(k, v) for k, vs in parse_qs(parts.query, keep_blank_values=True).items() for v in vs
            if not k.lower().startswith("utm_") and k.lower() not in NL_TRACKING]
    return urlunsplit(parts._replace(query=urlencode(keep), fragment=""))


def nl_name_from_url(url):
    """Product name guessed from the link ('.../maison-crivelli-hibiscus-mahajad-extrait-50-ml/' ->
    'Maison Crivelli Hibiscus Mahajad Extrait 50 ml') - put in the box when the site cannot be read."""
    slug = [x for x in urlsplit(url).path.split("/") if x][-1:] or [""]
    words = re.sub(r"\.html?$", "", slug[0]).replace("-", " ").replace("_", " ").split()
    words = [w for w in words if not re.fullmatch(r"p?\d{5,}", w)]          # ids like p123456 / 0201020036
    text = " ".join(w if re.fullmatch(r"\d+(ml)?", w, re.I) or w.lower() == "ml" else w.capitalize() for w in words)
    return re.sub(r"(\d+)\s*ml\b", r"\1 ml", text, flags=re.I)


def nl_shop_of(url):
    """Shop prefix from the link's web address, None when unknown."""
    host = urlsplit(url).netloc.lower()
    return next((k for part, k in NL_HOSTS if part in host), None)


# ============================================================ SECTION 2: card parts (competitors_page.py puts them in)
NL_CSS = """ /* Add new link (admins only, new_link.py): bottom right of a Competitor table card */
 .nl-wrap{display:flex;flex-direction:column;align-items:flex-end;margin-top:8px}
 .nl-btn{background:none;border:0;color:var(--muted);font-size:13px;cursor:pointer;text-decoration:underline;padding:0}
 .nl-box{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;margin-top:6px;width:100%}
 .nl-box[hidden],.nl-man[hidden]{display:none}
 .nl-box input,.nl-box select{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;
                              padding:7px 9px;font-size:13px}
 .nl-box input.nl-url{flex:1 1 320px;min-width:0}
 .nl-man{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;width:100%}
 .nl-man input.nl-name{flex:1 1 260px;min-width:0} .nl-man input.nl-price{width:110px}
 .nl-man label{font-size:13px;color:var(--muted);display:flex;align-items:center;gap:4px}
 .nl-box button{border-radius:8px;padding:7px 12px;font-weight:600;cursor:pointer;font-size:13px}
 .nl-save{background:#f2d675;color:#1c1b19;border:0}
 .nl-cancel{background:none;color:var(--muted);border:1px solid var(--line)}
 .nl-msg{font-size:12px;margin-top:4px;text-align:right} .nl-msg.err{color:#e0806c} .nl-msg.ok{color:#8fd19e}
"""

# r = product row (competitors_page.py card); nl_shops = [(prefix, label)]
NL_HTML = """    {# Add new link: admins only (new_link.py) - competitor product the matching did not find #}
    {% if admin %}
    <div class="nl-wrap">
      <button type="button" class="nl-btn" onclick="nlOpen('{{ r.fi }}')">Add new link</button>
      <div class="nl-box" id="nl-{{ r.fi }}" hidden>
        <input type="url" class="nl-url" id="nlurl-{{ r.fi }}" placeholder="Paste the competitor's product link"
               oninput="nlGuess('{{ r.fi }}')">
        <select id="nlshop-{{ r.fi }}" title="Shop (found from the link)">
          <option value="">Shop ...</option>
          {% for k, label in nl_shops %}<option value="{{ k }}">{{ label }}</option>{% endfor %}
        </select>
        {# only shown when the shop's site cannot be read: type the product's name, price and stock #}
        <div class="nl-man" id="nlman-{{ r.fi }}" hidden>
          <input class="nl-name" id="nlname-{{ r.fi }}" placeholder="Product name on that shop">
          <input class="nl-price" id="nlprice-{{ r.fi }}" type="number" min="0" step="0.01" placeholder="AED price">
          <label><input type="checkbox" id="nlstock-{{ r.fi }}" checked> In stock</label>
        </div>
        <button type="button" class="nl-save" onclick="nlSave('{{ r.fi }}', this)">Save</button>
        <button type="button" class="nl-cancel" onclick="nlClose('{{ r.fi }}')">Cancel</button>
      </div>
      <div class="nl-msg" id="nlmsg-{{ r.fi }}"></div>
    </div>
    {% endif %}
"""

NL_JS = """  // ---- Add new link (admins only, new_link.py): read the product from the link, save it in the shop's list + link it ----
  var NL_HOSTS = @@NL_HOSTS@@;
  function nlOpen(fi) {
    var b = document.getElementById("nl-" + fi); b.hidden = !b.hidden;
    if (!b.hidden) document.getElementById("nlurl-" + fi).focus();
  }
  function nlClose(fi) {
    document.getElementById("nl-" + fi).hidden = true;
    document.getElementById("nlman-" + fi).hidden = true;
    document.getElementById("nlurl-" + fi).value = "";
    document.getElementById("nlmsg-" + fi).textContent = "";
  }
  function nlGuess(fi) {                               // shop from the pasted link
    var u = document.getElementById("nlurl-" + fi).value.toLowerCase();
    for (var i = 0; i < NL_HOSTS.length; i++) {
      if (u.indexOf(NL_HOSTS[i][0]) >= 0) { document.getElementById("nlshop-" + fi).value = NL_HOSTS[i][1]; return; }
    }
  }
  async function nlSave(fi, btn) {
    var msg = document.getElementById("nlmsg-" + fi), man = document.getElementById("nlman-" + fi);
    var body = {fi_record_id: fi, url: document.getElementById("nlurl-" + fi).value.trim(),
                shop: document.getElementById("nlshop-" + fi).value};
    if (!body.url) { msg.className = "nl-msg err"; msg.textContent = "Paste a link first"; return; }
    if (!man.hidden) {                                 // typed by hand (site could not be read)
      body.manual = {name: document.getElementById("nlname-" + fi).value.trim(),
                     price: document.getElementById("nlprice-" + fi).value,
                     in_stock: document.getElementById("nlstock-" + fi).checked};
    }
    btn.disabled = true; msg.className = "nl-msg"; msg.textContent = "Reading the product ...";
    console.log("[NEW-LINK] " + fi + " " + body.shop + " " + body.url);
    try {
      var r = await fetch("@@NL_URL@@", {method: "POST", headers: {"Content-Type": "application/json"},
                                         body: JSON.stringify(body)});
      var d = await r.json().catch(function () { return {ok: false, error: "HTTP " + r.status}; });
      if (d.need_manual) {                             // site blocked -> ask for name / price / stock
        man.hidden = false; msg.className = "nl-msg err";
        msg.textContent = d.error + " Type the name, price and stock, then Save again.";
        if (d.name) document.getElementById("nlname-" + fi).value = d.name;
        return;
      }
      if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
      msg.className = "nl-msg ok";
      msg.textContent = (d.added ? "Added to " + d.label + " list + linked: " : "Linked: ") + d.name + " | AED " +
                        (d.price == null ? "-" : d.price) + " | " + d.stock + " - reloading...";
      setTimeout(function () { location.reload(); }, 1200);
    } catch (e) {
      msg.className = "nl-msg err"; msg.textContent = "Not saved: " + e.message;
    } finally { btn.disabled = false; }
  }
"""


def nl_js():
    return NL_JS.replace("@@NL_URL@@", NL_URL).replace("@@NL_HOSTS@@", json.dumps([list(x) for x in NL_HOSTS]))


# ============================================================ SECTION 3: read the product from its link
def nl_variant_id(url, sku=None):
    """Stable number for shops whose list needs a variant_id but the link has none (same idea as brandsperfume)."""
    return zlib.crc32(f"{url.split('#')[0]}|{sku or ''}".encode()) + 10 ** 12


def nl_shopify(url):
    """Shopify product link (/products/<handle>) -> row dict, None when not Shopify."""
    parts = urlsplit(url)
    if "/products/" not in parts.path:
        return None
    try:
        r = requests.get(f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}.js", headers=NL_HEADERS, timeout=40)
        p = r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(p, dict) or not p.get("variants"):
        return None
    want = (parse_qs(parts.query).get("variant") or [None])[0]
    v = next((x for x in p["variants"] if str(x.get("id")) == str(want)), None) or p["variants"][0]
    vt = (v.get("title") or "").strip()
    name = p.get("title") if vt.lower() in ("", "default title") else f"{p.get('title')} - {vt}"
    price = round(v["price"] / 100, 2) if isinstance(v.get("price"), (int, float)) else br.br_num(v.get("price"))
    bc = str(v.get("barcode") or "").strip()
    return {"variant_id": int(v["id"]), "product_id": p.get("id"), "url": url, "name": name,
            "brand": (p.get("vendor") or "").strip() or None, "product_type": p.get("type") or None,
            "sku": (v.get("sku") or "").strip() or None, "gtin": bc if bc.isdigit() and 12 <= len(bc) <= 14 else None,
            "price_inc_tax": price, "stock": "In stock" if v.get("available") else "Out of stock",
            "volume": br.br_volume(vt, p.get("title"), *(p.get("tags") or []))}


def nl_woo(url, shop):
    """WooCommerce shop (Al Injaz, Niche Gallerie): /product/<slug>/ -> its public Store API
    /wp-json/wc/store/v1/products?slug=<slug> (same data alinjaz.py / nichegallerie.py download) -> row dict or None."""
    parts = urlsplit(url)
    m = re.search(r"/product/([^/?#]+)", parts.path)
    if not m:
        return None
    try:
        r = requests.get(f"{parts.scheme}://{parts.netloc}/wp-json/wc/store/v1/products",
                         params={"slug": m.group(1)}, headers=NL_HEADERS, timeout=40)
        items = r.json() if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(items, list) or not items:
        log(f"[NEW-LINK] Store API {parts.netloc} slug {m.group(1)}: HTTP {r.status_code}, nothing")
        return None
    if shop == "ng":
        import nichegallerie as mod
        row = mod.ng_item(items[0])
    else:
        import alinjaz as mod
        row = mod.aj_item(items[0])
    return {**row, "url": url}


def nl_aljazeera(url):
    """Al Jazeera link /en/product-details/<NAME>/<id>?variant=<vid> -> row dict from its API (aljazeera.py)."""
    import aljazeera as jz
    m = re.search(r"/product-details/[^/]+/(\d+)", url)
    if not m:
        return None
    st, txt = jz.jz_api_get(f"/api/customer/products/{m.group(1)}")
    try:
        prod = ((json.loads(txt) or {}).get("data") or {}).get("product")
    except ValueError:
        prod = None
    if st != 200 or not isinstance(prod, dict):
        return None
    rows, _ = jz.jz_api_rows(prod, keep_all=True)
    want = (parse_qs(urlsplit(url).query).get("variant") or [None])[0]
    return next((r for r in rows if str(r["variant_id"]) == str(want)), rows[0] if rows else None)


def nl_page(url):
    """Any other shop: the product page's schema.org data -> (row dict or None, HTTP status)."""
    st, html = br.br_get(url)
    if st != 200:
        return None, st
    rows = br.br_parse(url, html)
    clean = lambda u: (u or "").split("#")[0].rstrip("/")
    row = next((r for r in rows if clean(r["url"]) == clean(url)), rows[0] if rows else None)
    return row, st


def nl_read(shop, url):
    """-> (row dict or None, reason when None). Row keys: variant_id, url, name, brand, product_type, sku, gtin,
    price_inc_tax, stock, volume."""
    try:
        row = nl_aljazeera(url) if shop == "jz" else nl_shopify(url)
        st = 200
        if row is None and shop in ("aj", "ng"):             # WooCommerce: Store API first (page may be a bot check)
            row = nl_woo(url, shop)
        if row is None and shop != "jz":
            row, st = nl_page(url)
    except Exception as ex:                                 # never a 500 because of a strange page
        log(f"[NEW-LINK] read {url}: ERROR {ex}")
        return None, f"could not read the page ({str(ex)[:80]})."
    if row is None:
        return None, ("the site blocks our server (HTTP %s, bot check)." % st if st in (202, 403, 429, 503, 0)
                      else "no product data found on that page." if st == 200 else f"the page answered HTTP {st}.")
    if not row.get("price_inc_tax"):
        return None, "no price found on that page."
    row["url"] = url
    return row, None


# ============================================================ SECTION 4: save into the shop's list + link
def nl_columns(cur, table):
    """Column names of a shop's list table."""
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
    return {c for (c,) in cur.fetchall()}


def nl_save_catalog(cur, shop, row):
    """Insert the product into the shop's list (only the columns that table has). Returns the link stored there
    (an existing row of the same variant keeps its own link)."""
    label, table, link_col, *_ = link_edit.SHOP_LISTS[shop]
    cols = nl_columns(cur, table)
    if not cols:
        raise RuntimeError(f"{table} does not exist yet - run that shop's download once first")
    price = row.get("price_inc_tax")
    name = row.get("name") or ""
    if "brand" not in cols and row.get("brand") and row["brand"].lower() not in name.lower():
        name = f"{row['brand']} {name}"                    # list without brand column: brand in the name
    vid = row.get("variant_id") or nl_variant_id(row["url"], row.get("sku"))
    values = {link_col: row["url"], "name": name, "brand": row.get("brand"), "gtin": row.get("gtin"),
              "sku": row.get("sku"), "product_type": row.get("product_type"), "volume": row.get("volume"),
              "stock": row.get("stock"), "variant_id": vid, "product_id": row.get("product_id"),
              "airtable_record_id": f"new:{row['url']}"}      # French Fragrance list key
    if "price_inc_tax" in cols:
        values.update(price_inc_tax=price, price=round(price / 1.05, 2) if price else None)
    else:
        values["price"] = price                              # Samawa: one price column = price shown
    use = {k: v for k, v in values.items() if k in cols}
    keys = list(use)
    cur.execute(f"INSERT INTO {table} ({', '.join(keys)}, updated_at) VALUES ({', '.join(['%s'] * len(keys))}, NOW()) "
                f"ON CONFLICT DO NOTHING RETURNING {link_col}", [use[k] for k in keys])
    got = cur.fetchone()
    if got:
        return got[0], True
    if "variant_id" in cols:                                 # same size already in the list under another link
        cur.execute(f"SELECT {link_col} FROM {table} WHERE variant_id = %s", (vid,))
        old = cur.fetchone()
        if old:
            return old[0], False
    return row["url"], False


@new_link_bp.post(NL_URL)
def new_link_save():
    """Body (JSON): {"fi_record_id": "rec...", "url": "https://...", "shop": "am" or "",
                     "manual": {"name": "...", "price": "123", "in_stock": true}  (only when the site cannot be read)}"""
    user = g.get("user") or "unknown"
    if g.get("role") != "admin":
        log(f"[NEW-LINK] {user} is not an admin -> refused")
        return jsonify({"ok": False, "error": "Only admins can add links"}), 403
    body = request.get_json(silent=True) or {}
    fid, url = str(body.get("fi_record_id") or ""), str(body.get("url") or "").strip()
    if url.lower().startswith("http"):
        clean = nl_clean_url(url)
        if clean != url:
            log(f"[NEW-LINK] tracking part cut off: {url} -> {clean}")
        url = clean
    shop = body.get("shop") or nl_shop_of(url)
    if not fid.startswith(("rec", "new")) or not url.lower().startswith("http"):
        return jsonify({"ok": False, "error": "Paste the full product link (https://...)"}), 400
    if shop not in NL_SHOPS:
        return jsonify({"ok": False, "error": "Shop not found from the link - pick the shop in the list"}), 400
    if not link_edit.HOOKS:
        return jsonify({"ok": False, "error": "not ready (app.py did not call link_edit.init)"}), 500
    label, table, link_col, name_sql, price_sql, stock_col = link_edit.SHOP_LISTS[shop]
    log(f"[NEW-LINK] {user} -> {fid}: {label} {url}")

    conn = link_edit.HOOKS["connect"]()
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM competitor_table WHERE fi_record_id = %s", (fid,))
        if not cur.fetchone():
            return jsonify({"ok": False, "error": "Product not found"}), 404
        # 1. already in the shop's list?
        item = None
        if nl_columns(cur, table):
            cur.execute(f"SELECT {link_col}, {name_sql}, {price_sql}, {stock_col} FROM {table} "
                        f"WHERE rtrim({link_col}, '/') = rtrim(%s, '/') LIMIT 1", (url,))
            item = cur.fetchone()
        conn.commit()                                       # no open transaction while the shop's site answers
        added = False
        if item:
            link, name, price, stock = item
            log(f"[NEW-LINK] already in {table}: {name} | AED {price} | {stock}")
        else:
            # 2. read it from the link (or take what the admin typed) and save it in the shop's list
            man = body.get("manual") or None
            if man:
                price = br.br_num(man.get("price"))
                if not (man.get("name") or "").strip() or not price or price <= 0:
                    return jsonify({"ok": False, "need_manual": True, "error": "Name and price are needed."}), 400
                row = {"url": url, "name": man["name"].strip(), "brand": None, "price_inc_tax": price,
                       "stock": "In stock" if man.get("in_stock") else "Out of stock",
                       "volume": br.br_volume(man["name"])}
                log(f"[NEW-LINK] typed by {user}: {row['name']} | AED {price} | {row['stock']}")
            else:
                row, why = nl_read(shop, url)
                if row is None:
                    log(f"[NEW-LINK] {url}: not read - {why}")
                    return jsonify({"ok": False, "need_manual": True, "error": f"{label}: {why}",
                                    "name": nl_name_from_url(url)})      # name guessed from the link -> box
                log(f"[NEW-LINK] read: {row.get('brand') or ''} {row['name']} | AED {row['price_inc_tax']} | "
                    f"{row['stock']} | {row.get('volume') or '-'}")
            link, added = nl_save_catalog(cur, shop, row)
            cur.execute(f"SELECT {link_col}, {name_sql}, {price_sql}, {stock_col} FROM {table} "
                        f"WHERE {link_col} = %s LIMIT 1", (link,))
            link, name, price, stock = cur.fetchone()
            log(f"[NEW-LINK] {'ADDED to' if added else 'already in'} {table}: {link}")
        # 3. put it on OUR product, confirmed + prices recalculated
        link_edit.set_link(cur, fid, shop, link, name, price, stock, user)
        conn.commit()
    except Exception as ex:
        conn.rollback()
        log(f"[NEW-LINK] {fid}: ERROR {ex}")
        return jsonify({"ok": False, "error": str(ex)[:200]}), 500
    finally:
        cur.close()
        conn.close()
    log(f"[NEW-LINK] Done: {fid} {label} -> {link} ({name} | AED {price} | {stock})")
    return jsonify({"ok": True, "added": added, "label": label, "link": link, "name": name, "stock": stock,
                    "price": float(price) if price is not None else None})

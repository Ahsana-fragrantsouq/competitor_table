"""
Samawa Competitor Matcher - Render service
------------------------------------------
Flow:
  1. Download the full Samawa catalog from https://samawa.ae/products.json (Shopify public endpoint)
  2. Read French Inventories records for the requested brands (Airtable)
  3. Match each product to Samawa: existing link -> barcode -> fuzzy (brand + name + size + type + gender)
  4. Create / update rows in the Competitor table with Samawa link, price and stock

Trigger:  POST /samawa/run   header X-Run-Secret: <RUN_SECRET>   body {"brands": ["Afnan", "Armaf"]}
Status :  GET  /samawa/status
"""

import os
import re
import time
import difflib
import threading
import traceback
import unicodedata
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ---------------------------------------------------------------- config
AIRTABLE_TOKEN = os.environ["AIRTABLE_TOKEN"]
RUN_SECRET = os.environ.get("RUN_SECRET", "")
BASE_ID = "app5gOqDt9aZrW5bV"
FI_TABLE = "tblL03CEHdYy1kUdQ"      # French Inventories
COMP_TABLE = "tblN9zlKmfvOXZ9Ov"    # Competitor table
SAMAWA_BASE = "https://samawa.ae"
MATCH_THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", "0.85"))
DEFAULT_BRANDS = [b.strip() for b in os.environ.get("DEFAULT_BRANDS", "").split(",") if b.strip()]

# French Inventories field names
F_BRAND = "Brand"
F_BARCODE = "Barcode"
F_PERFUME = "Perfume Name"
F_PRODUCT = "Product Name"
F_SIZE = "Size"
F_TYPE = "Type"
F_CATEGORY = "Category"

# Competitor table field names
C_NAME = "Name"
C_LINK_FI = "French Inventories"
C_URL = "Samawa link"
C_PRICE = "Samawa Price"
C_STOCK = "Samawa Stock?"

AT_URL = f"https://api.airtable.com/v0/{BASE_ID}"
AT_HEADERS = {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}
SAMAWA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

run_lock = threading.Lock()
state = {"last_summary": None, "catalog_incomplete": False}


def log(*args):
    # flush=True so logs show up immediately in Render
    print(*args, flush=True)


# ---------------------------------------------------------------- text helpers
PHRASES = [
    (" eau de toilette ", " edt "),
    (" eau de parfum ", " edp "),
    (" essence de parfum ", " edp "),
    (" pure parfum ", " parfum "),
    (" extrait de parfum ", " extrait "),
    (" eau de cologne ", " edc "),
    (" men and women ", " unisex "),
    (" women and men ", " unisex "),
    (" pour homme ", " men "),
    (" pour femme ", " women "),
    (" for him ", " men "),
    (" for her ", " women "),
    (" aqva ", " aqua "),   # Bvlgari spelling
    (" aoud ", " oud "),
]

STOP = {
    "perfume", "perfumes", "fragrance", "fragrances", "for", "and", "men", "women", "man", "woman",
    "unisex", "him", "her", "homme", "femme", "pour", "edt", "edp", "edc", "extrait", "parfum",
    "eau", "de", "ml", "oz", "spray", "the", "by", "new", "with",
    "edition", "limited", "collection", "special", "signature",
    "woody", "floral", "oriental", "fruity", "spicy", "aromatic", "aquatic", "chypre", "fougere", "gourmand", "citrus",
}


def as_text(v):
    # Airtable lookups can come back as lists
    if v is None:
        return ""
    if isinstance(v, list):
        return " ".join(str(x) for x in v)
    return str(v)


def norm_text(s):
    s = re.sub(r"[\u2018\u2019\u201a\u201b`\u00b4]", " ", as_text(s))  # curly apostrophes -> space (L’Interdit = L'Interdit)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()  # remove accents
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\b\d+(?:\s\d+)?\s*(?:ml|oz)\b", " ", s)      # drop sizes: "100ml", "100 ml", "1 7 oz"
    s = re.sub(r"\b(\d+)\s+(am|pm)\b", r"\1\2", s)            # "9 am" -> "9am"
    s = f" {s.strip()} "
    for a, b in PHRASES:
        s = s.replace(a, b)
    return s


def key(s):
    s = unicodedata.normalize("NFKD", as_text(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def tokens(normed):
    out = set()
    for t in normed.split():
        if t in STOP or re.fullmatch(r"\d+(ml|oz)", t):
            continue
        out.add(t)
    return out


def detect_type(normed):
    for code in ("extrait", "edp", "edt", "edc"):
        if f" {code} " in normed:
            return code
    if " cologne " in normed:
        return "edc"
    if " parfum " in normed:
        return "parfum"
    return None


def detect_gender(normed):
    if " unisex " in normed:
        return "unisex"
    men = any(w in normed for w in (" men ", " man ", " homme ", " him "))
    women = any(w in normed for w in (" women ", " woman ", " femme ", " her "))
    if men and women:
        return "unisex"
    if women:
        return "women"
    if men:
        return "men"
    return None


def parse_size(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", as_text(text).lower())
    return float(m.group(1)) if m else None


def norm_barcode(v):
    digits = re.sub(r"\D", "", as_text(v)).lstrip("0")
    return digits if len(digits) >= 8 else None


def token_hit(t, pool):
    if t in pool:
        return True
    for p in pool:
        if abs(len(p) - len(t)) > 2:
            continue
        need = 0.8 if len(p) == len(t) and len(t) >= 5 else 0.85
        if difflib.SequenceMatcher(None, t, p).ratio() >= need:
            return True
    return False


def name_score(fi_tokens, sm_tokens):
    if not fi_tokens or not sm_tokens:
        return 0.0
    coverage = sum(token_hit(t, sm_tokens) for t in fi_tokens) / len(fi_tokens)
    precision = sum(token_hit(t, fi_tokens) for t in sm_tokens) / len(sm_tokens)
    return round(0.6 * coverage + 0.4 * precision, 3)


# ---------------------------------------------------------------- Samawa
MAX_PAGES = 100  # Shopify storefront hard limit: page 101+ returns 400 (max 25,000 items per list)


def slim(p):
    # keep only what matching needs (drops body_html/images) -> much less memory on 512 MB instance
    return {
        "id": p.get("id"),
        "handle": p.get("handle", ""),
        "title": p.get("title", ""),
        "vendor": p.get("vendor", ""),
        "tags": p.get("tags", []),
        "variants": [{"title": v.get("title"), "sku": v.get("sku"), "price": v.get("price"),
                      "available": v.get("available")} for v in p.get("variants", [])],
    }


RETRY_WAITS = [10, 30, 60, 120, 180]  # seconds to wait between retries when Samawa is busy (429 / 5xx)


def samawa_get(path, page, label):
    """GET one page from Samawa. Retries on 429 / 5xx / network errors. Returns Response or None."""
    for attempt, wait in enumerate([0] + RETRY_WAITS):
        if wait:
            log(f"[SAMAWA] {label} page {page}: retry {attempt}/{len(RETRY_WAITS)} in {wait}s")
            time.sleep(wait)
        try:
            r = requests.get(f"{SAMAWA_BASE}{path}", params={"limit": 250, "page": page},
                             headers=SAMAWA_HEADERS, timeout=60)
        except requests.RequestException as ex:
            log(f"[SAMAWA] {label} page {page}: network error {ex}")
            continue
        if r.status_code == 429 or r.status_code >= 500:
            log(f"[SAMAWA] {label} page {page}: HTTP {r.status_code} (Samawa busy)")
            continue
        return r
    log(f"[SAMAWA] {label} page {page}: giving up after {len(RETRY_WAITS)} retries")
    return None


def fetch_paged(path, label, list_key):
    items, page = [], 1
    while page <= MAX_PAGES:
        r = samawa_get(path, page, label)
        if r is None:
            state["catalog_incomplete"] = True
            log(f"[SAMAWA] {label}: continuing with {len(items)} items downloaded so far")
            break
        if r.status_code in (400, 404):
            log(f"[SAMAWA] {label}: stopped at page {page} (HTTP {r.status_code})")
            break
        r.raise_for_status()
        batch = r.json().get(list_key, [])
        if not batch:
            break
        if list_key == "products":
            items.extend(slim(p) for p in batch)
        else:
            items.extend(batch)
        log(f"[SAMAWA] {label} page {page}: {len(batch)} (total {len(items)})")
        if len(batch) < 250:
            break
        page += 1
        time.sleep(2)
    return items


def fetch_samawa_catalog(brands):
    by_id = {}

    # 1) Brand collections first: complete per brand, not affected by the 25,000 cap
    log("[SAMAWA] Looking for brand collections ...")
    bkeys = [key(b) for b in brands]
    collections = fetch_paged("/collections.json", "collections", "collections")
    for c in collections:
        ckey, hkey = key(c.get("title")), key(c.get("handle"))
        if any(bk and (bk in ckey or bk in hkey or (len(ckey) >= 4 and ckey in bk)) for bk in bkeys):
            log(f"[SAMAWA] Brand collection found: '{c.get('title')}' ({c.get('handle')})")
            for p in fetch_paged(f"/collections/{c['handle']}/products.json", c["handle"], "products"):
                by_id[p["id"]] = p
    log(f"[SAMAWA] From brand collections: {len(by_id)} products")

    # 2) Full catalog (first 25,000 products) - also catches brands without a collection + barcodes
    log("[SAMAWA] Downloading full catalog from products.json ...")
    for p in fetch_paged("/products.json", "catalog", "products"):
        by_id[p["id"]] = p

    log(f"[SAMAWA] Catalog ready: {len(by_id)} unique products")
    return list(by_id.values())


def build_index(catalog):
    """One entry per variant. Returns (entries, by_barcode, by_handle)."""
    entries, by_barcode, by_handle = [], {}, {}
    for p in catalog:
        handle = p.get("handle", "")
        title = p.get("title", "")
        tags = [t.lower() for t in p.get("tags", [])]
        full_n = norm_text(title)
        name_n = norm_text(re.split(r"\s-\s|,", title)[0])  # drop " - Aromatic Aquatic..." / ", Oriental Woody..."
        p_type = detect_type(full_n)
        p_size = parse_size(title) or next((parse_size(t) for t in tags if parse_size(t)), None)
        p_gender = detect_gender(full_n)

        for v in p.get("variants", []):
            e = {
                "handle": handle,
                "title": title,
                "vendor_key": key(p.get("vendor")),
                "title_key": key(title),
                "title_norm": full_n,
                "size": parse_size(v.get("title")) or p_size,
                "type": p_type,
                "gender": p_gender,
                "tokens": tokens(name_n),
                "price": float(v.get("price") or 0),
                "available": bool(v.get("available")),
            }
            entries.append(e)
            by_handle.setdefault(handle, []).append(e)
            bc = norm_barcode(v.get("sku"))
            if bc:
                by_barcode[bc] = e
    log(f"[INDEX] {len(entries)} variants, {len(by_barcode)} barcodes, {len(by_handle)} handles")
    return entries, by_barcode, by_handle


def samawa_fields(e):
    return {C_URL: f"{SAMAWA_BASE}/products/{e['handle']}", C_PRICE: e["price"], C_STOCK: e["available"]}


# ---------------------------------------------------------------- Airtable
def at_list(table, params):
    records, offset = [], None
    while True:
        p = dict(params)
        if offset:
            p["offset"] = offset
        r = requests.get(f"{AT_URL}/{table}", headers=AT_HEADERS, params=p, timeout=60)
        if r.status_code == 429:
            log("[AIRTABLE] Rate limited, waiting 30s")
            time.sleep(30)
            continue
        if not r.ok:
            log(f"[AIRTABLE] List error {r.status_code}: {r.text}")
            r.raise_for_status()
        data = r.json()
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
        time.sleep(0.25)
    return records


def at_batch(table, method, records):
    for i in range(0, len(records), 10):
        chunk = records[i:i + 10]
        while True:
            r = requests.request(method, f"{AT_URL}/{table}", headers=AT_HEADERS,
                                 json={"records": chunk, "typecast": True}, timeout=60)
            if r.status_code == 429:
                log("[AIRTABLE] Rate limited, waiting 30s")
                time.sleep(30)
                continue
            if not r.ok:
                log(f"[AIRTABLE] {method} error {r.status_code}: {r.text}")
                r.raise_for_status()
            break
        log(f"[AIRTABLE] {method} {i + len(chunk)}/{len(records)} rows done")
        time.sleep(0.25)


def fetch_fi_for_brand(brand):
    b = brand.strip().lower().replace('"', '\\"')
    formula = f'TRIM(LOWER(ARRAYJOIN({{{F_BRAND}}})))="{b}"'
    recs = at_list(FI_TABLE, {
        "filterByFormula": formula,
        "fields[]": [F_BARCODE, F_PERFUME, F_PRODUCT, F_SIZE, F_TYPE, F_CATEGORY],
    })
    log(f"[FI] Brand '{brand}': {len(recs)} French Inventories records")
    if not recs:
        log(f"[FI] WARNING: 0 records for '{brand}' - check spelling matches the brands table")
    return recs


# ---------------------------------------------------------------- matching
def pick_by_size(entries, size):
    if size:
        for e in entries:
            if e["size"] and abs(e["size"] - size) <= 0.5:
                return e
    return entries[0]


def match_one(fields, brand, brand_entries, by_barcode):
    product_name = as_text(fields.get(F_PRODUCT))
    fi_size = parse_size(fields.get(F_SIZE)) or parse_size(product_name)
    fi_type = detect_type(norm_text(fields.get(F_TYPE))) or detect_type(norm_text(product_name))
    fi_gender = detect_gender(norm_text(fields.get(F_CATEGORY)))
    brand_tokens = tokens(norm_text(brand))
    fi_tokens = tokens(norm_text(fields.get(F_PERFUME) or product_name)) - brand_tokens

    def score(e):
        # returns None when the candidate is impossible (wrong size / type / gender)
        if fi_size and (not e["size"] or abs(e["size"] - fi_size) > 0.5):
            return None
        if fi_gender and e["gender"] and "unisex" not in (fi_gender, e["gender"]) and fi_gender != e["gender"]:
            return None
        s = name_score(fi_tokens, e["tokens"] - brand_tokens)
        if fi_type and e["type"] and fi_type != e["type"]:
            s -= 0.12   # EDT vs EDP: allowed only if the name is a near-perfect match
        return round(s, 3)

    def method(base, e):
        return f"{base}(type {fi_type}->{e['type']})" if fi_type and e["type"] and fi_type != e["type"] else base

    scored = [(s, e) for e in brand_entries if (s := score(e)) is not None]
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[0][0] if scored else 0.0

    # 1) barcode - trusted only if it passes size/type/gender AND no other product has a better name
    bc = norm_barcode(fields.get(F_BARCODE))
    if bc and bc in by_barcode:
        e = by_barcode[bc]
        s = score(e)
        if s is not None and ((not fi_tokens) or (s >= 0.6 and s >= top - 0.001)):
            return e, method("barcode", e), s
        log(f"[BARCODE-REJECT] {product_name} -> barcode points to '{e['title']}' (score={s}), using name match")

    # 2) fuzzy
    if not fi_tokens:
        return None, "no name tokens", 0.0
    if not scored:
        return None, "no candidate with same size/type/gender", 0.0
    best_score, best = scored[0]
    if best_score < MATCH_THRESHOLD:
        return None, f"low score (best: {best['title']})", best_score
    tied = [e for s, e in scored if s >= best_score - 0.02]
    if len({e["handle"] for e in tied}) > 1:
        if all(name_score(fi_tokens, e["tokens"] - brand_tokens) >= 0.95 for e in tied):
            # Samawa has duplicate listings of the same perfume -> prefer in stock, then cheapest
            best = sorted(tied, key=lambda e: (not e["available"], e["price"]))[0]
            log(f"[DUPLICATE] {product_name}: {len(tied)} Samawa listings, picked '{best['title']}'")
        else:
            return None, f"ambiguous ({tied[0]['title']} | {tied[1]['title']})", best_score
    return best, method("fuzzy", best), best_score


def run_match(brands, rematch=False):
    started = time.time()
    log(f"[RUN] rematch={rematch}")
    summary = {"brands": brands, "rematch": rematch, "matched_barcode": 0, "matched_fuzzy": 0, "existing_link_refreshed": 0,
               "link_gone": 0, "unmatched": 0, "created": 0, "updated": 0, "unmatched_list": []}

    state["catalog_incomplete"] = False
    entries, by_barcode, by_handle = build_index(fetch_samawa_catalog(brands))
    safe_mode = state["catalog_incomplete"]
    summary["catalog_incomplete"] = safe_mode
    if safe_mode:
        log("[SAFE-MODE] Samawa download was incomplete -> will NOT clear links or untick stock this run")

    # existing Competitor rows, keyed by French Inventories record id
    comp_rows = at_list(COMP_TABLE, {"fields[]": [C_LINK_FI, C_URL]})
    comp_by_fi = {}
    for row in comp_rows:
        for fid in row.get("fields", {}).get(C_LINK_FI, []):
            comp_by_fi[fid] = row
    log(f"[COMP] {len(comp_rows)} existing Competitor rows")

    creates, updates = [], []

    for brand in brands:
        bkey, bnorm = key(brand), norm_text(brand)
        # vendor "Dior" also counts for brand "Christian Dior" (vendor name inside brand name)
        brand_entries = [e for e in entries if e["vendor_key"].startswith(bkey) or bnorm in e["title_norm"]
                         or (len(e["vendor_key"]) >= 4 and e["vendor_key"] in bkey)]
        log(f"\n[BRAND] ===== {brand}: {len(brand_entries)} Samawa variants =====")

        for rec in fetch_fi_for_brand(brand):
            fid, f = rec["id"], rec.get("fields", {})
            pname = as_text(f.get(F_PRODUCT)) or as_text(f.get(F_PERFUME))
            row = comp_by_fi.get(fid)
            existing_url = None if rematch else (row or {}).get("fields", {}).get(C_URL)

            # A) row already has a Samawa link (manual or earlier run) -> keep link, refresh price/stock
            if existing_url:
                handle = urlparse(existing_url).path.rstrip("/").split("/products/")[-1]
                found = by_handle.get(handle)
                if found:
                    e = pick_by_size(found, parse_size(f.get(F_SIZE)) or parse_size(pname))
                    updates.append({"id": row["id"], "fields": samawa_fields(e)})
                    summary["existing_link_refreshed"] += 1
                    log(f"[REFRESH] {pname} -> AED {e['price']} | stock={e['available']}")
                elif safe_mode:
                    log(f"[SAFE-MODE] {pname} -> handle '{handle}' not downloaded, left unchanged")
                else:
                    updates.append({"id": row["id"], "fields": {C_STOCK: False}})
                    summary["link_gone"] += 1
                    log(f"[GONE] {pname} -> handle '{handle}' not in Samawa catalog, stock unticked")
                continue

            # B) find a match
            e, method, score = match_one(f, brand, brand_entries, by_barcode)
            if e:
                summary["matched_barcode" if method.startswith("barcode") else "matched_fuzzy"] += 1
                log(f"[MATCH:{method} {score}] {pname} -> {e['title']} | AED {e['price']} | stock={e['available']}")
                sf = samawa_fields(e)
            else:
                summary["unmatched"] += 1
                summary["unmatched_list"].append(f"{pname} | {method} | {score}")
                log(f"[NO MATCH] {pname} | {method} | score={score}")
                sf = {}

            if row:
                if sf:
                    updates.append({"id": row["id"], "fields": sf})
                elif rematch and not safe_mode:
                    # rematch: clear the old (possibly wrong) link
                    updates.append({"id": row["id"], "fields": {C_URL: None, C_PRICE: None, C_STOCK: False}})
            else:
                creates.append({"fields": {C_NAME: pname, C_LINK_FI: [fid], **sf}})

    log(f"\n[WRITE] Creating {len(creates)} rows, updating {len(updates)} rows")
    at_batch(COMP_TABLE, "POST", creates)
    at_batch(COMP_TABLE, "PATCH", updates)
    summary["created"], summary["updated"] = len(creates), len(updates)
    summary["seconds"] = round(time.time() - started, 1)
    log(f"[DONE] {summary}")
    return summary


# ---------------------------------------------------------------- routes
def _worker(brands, rematch=False):
    try:
        state["last_summary"] = run_match(brands, rematch)
    except Exception as ex:
        log(f"[ERROR] {ex}\n{traceback.format_exc()}")
        state["last_summary"] = {"error": str(ex)}
    finally:
        run_lock.release()


@app.get("/")
def health():
    return jsonify({"ok": True, "service": "samawa-matcher"})


@app.post("/samawa/run")
def trigger():
    if RUN_SECRET and request.headers.get("X-Run-Secret") != RUN_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    brands = body.get("brands") or DEFAULT_BRANDS
    rematch = bool(body.get("rematch"))
    if not brands:
        return jsonify({"error": "no brands given"}), 400
    if not run_lock.acquire(blocking=False):
        return jsonify({"error": "a run is already in progress"}), 409
    log(f"[RUN] Started for brands: {brands}")
    threading.Thread(target=_worker, args=(brands, rematch), daemon=True).start()
    return jsonify({"started": True, "brands": brands, "rematch": rematch}), 202


@app.get("/samawa/run")
def trigger_from_url():
    # Browser-friendly trigger: /samawa/run?secret=XXX&brands=Afnan,Armaf
    if RUN_SECRET and request.args.get("secret") != RUN_SECRET:
        log("[RUN-URL] Unauthorized attempt")
        return jsonify({"error": "unauthorized"}), 401
    brands = [b.strip() for b in request.args.get("brands", "").split(",") if b.strip()] or DEFAULT_BRANDS
    rematch = request.args.get("rematch") == "1"
    if not brands:
        return jsonify({"error": "no brands given"}), 400
    if not run_lock.acquire(blocking=False):
        log("[RUN-URL] Rejected: a run is already in progress")
        return jsonify({"error": "a run is already in progress"}), 409
    log(f"[RUN-URL] Started from browser for brands: {brands}")
    threading.Thread(target=_worker, args=(brands, rematch), daemon=True).start()
    return jsonify({"started": True, "brands": brands, "rematch": rematch, "check_status": "/samawa/status"}), 202


@app.get("/samawa/status")
def status():
    return jsonify({"running": run_lock.locked(), "last_summary": state["last_summary"]})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
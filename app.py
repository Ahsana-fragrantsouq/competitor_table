"""
Competitor Table service (Render: https://competitor-table.onrender.com)
=======================================================================
What this app does
  Compares Fragrant Souq products (Airtable "French Inventories") with competitor websites
  (Samawa now, French Fragrance / V Perfumes later) and saves the result in Postgres.

Files in this project
  app.py              -> this file: matching logic + all "run" URLs
  samawa_catalog.py   -> downloads the whole Samawa website (products.json) into Postgres table samawa_catalog
  ff_catalog_page.py  -> simple table page for French Fragrance products (Postgres french_fragrance_catalog)
  competitors_page.py -> the dark tabbed page /competitors (Competitor table | Samawa | French Fragrance | Branded Perfume)
  branded_perfume.py  -> copies French Fragrance products and reads the same pages on brandedperfume.com
  essenzi.py          -> same for essenzi.com (table essenzi_catalog); page reading = es_scraper.py on the PC
  vperfumes.py        -> downloads the vperfumes.com UAE Perfumes category into vperfumes_catalog

Normal order to run things (open these URLs in the browser)
  1. /samawa-catalog/run?secret=XXX        download Samawa website into Postgres (samawa_catalog)
  2. /competitor/load-fi?secret=XXX        copy ALL French Inventories products into Postgres (competitor_table)
  3. /competitor/match-samawa?secret=XXX   find each product on Samawa -> link, price, stock, suggestion
  4. /competitor/match-ff?secret=XXX       same for French Fragrance (uses french_fragrance_catalog)
  5. /competitor/update-least?secret=XXX   Branded Perfume + Essenzi prices (via FF match) + least price
  6. /competitor/match-vp?secret=XXX       find each product on V Perfumes (vperfumes_catalog, barcode first)
     (least price is also recalculated automatically at the end of steps 2, 3 and 4)
  Progress: step 1 -> /samawa-catalog/status | steps 2, 3, 4 -> /samawa/status | Result -> /competitors

Old URL still here (reads Airtable, matches, but saves NOTHING - only writes logs)
  /samawa/run?secret=XXX

Environment variables (Render -> Environment)
  AIRTABLE_TOKEN     Airtable token (only READS Airtable now)
  FF_DATABASE_URL    Postgres URL ending in /french_fragrance_db
  RUN_SECRET         password for all /run URLs (?secret=...)
  BRANDS_NAME_FIELD  brand name field in the Airtable brands table ("Brand Name")
  MATCH_THRESHOLD    minimum name score to count as a match (default 0.85)
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

# Pages / jobs that live in their own files ("blueprints") and are plugged into this app
from ff_catalog_page import ff_catalog_bp          # /ff-catalog
from samawa_catalog import samawa_catalog_bp       # /samawa-catalog, /samawa-catalog/run, /samawa-catalog/status
from competitors_page import competitors_bp        # /competitors
from branded_perfume import branded_perfume_bp     # /branded-perfume/test, /load, /run, /status
from essenzi import essenzi_bp                     # /essenzi/load, /essenzi/status
from vperfumes import vperfumes_bp                 # /vperfumes/test, /vperfumes/run, /vperfumes/status

app = Flask(__name__)
app.register_blueprint(ff_catalog_bp)
app.register_blueprint(samawa_catalog_bp)
app.register_blueprint(competitors_bp)
app.register_blueprint(branded_perfume_bp)
app.register_blueprint(essenzi_bp)
app.register_blueprint(vperfumes_bp)


# ---------------------------------------------------------------- config
# Settings. Values in os.environ come from Render -> Environment; the rest are fixed IDs / names.
AIRTABLE_TOKEN = os.environ["AIRTABLE_TOKEN"]
RUN_SECRET = os.environ.get("RUN_SECRET", "")
BASE_ID = "app5gOqDt9aZrW5bV"
FI_TABLE = "tblL03CEHdYy1kUdQ"      # French Inventories
COMP_TABLE = "tblN9zlKmfvOXZ9Ov"    # Competitor table
SAMAWA_BASE = "https://samawa.ae"
MATCH_THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", "0.85"))
# Brands come from the Airtable "brands" table: every record with the checkbox ticked is processed
BRANDS_TABLE = os.environ.get("BRANDS_TABLE", "brands")
BRANDS_NAME_FIELD = os.environ.get("BRANDS_NAME_FIELD", "Name")          # brand name field in brands table
BRANDS_TRACK_FIELD = os.environ.get("BRANDS_TRACK_FIELD", "")  # optional checkbox filter; empty = ALL brands

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
C_SUGGEST = "Samawa Suggestion"     # URL field: best guess for unmatched rows (review by hand)
SUGGESTIONS = os.environ.get("SUGGESTIONS", "") == "1"   # turn on only after creating the field above

# Airtable API address + login header, and a normal browser "User-Agent" so Samawa answers like to a browser
AT_URL = f"https://api.airtable.com/v0/{BASE_ID}"
AT_HEADERS = {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}
SAMAWA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

# run_lock: only ONE long job (samawa run / load-fi / match-samawa) can run at a time
# state:    progress of the current / last job, shown at /samawa/status
run_lock = threading.Lock()
state = {"last_summary": None, "catalog_incomplete": False}


def log(*args):
    # flush=True so logs show up immediately in Render
    print(*args, flush=True)


# ---------------------------------------------------------------- text helpers
# Product names are written differently on every site ("Eau de Parfum" vs "EDP", "Pour Homme" vs "Men").
# These helpers turn names into the same simple form so they can be compared.

# Long wording -> short code (applied after lower-casing)
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

# Words ignored when comparing names: they appear in almost every product, so they say nothing
# about WHICH perfume it is (type, gender and size are checked separately)
STOP = {
    "perfume", "perfumes", "fragrance", "fragrances", "for", "and", "men", "women", "man", "woman",
    "unisex", "him", "her", "homme", "femme", "pour", "edt", "edp", "edc", "extrait", "parfum",
    "eau", "de", "ml", "oz", "spray", "the", "by", "new", "with",
    "edition", "limited", "collection", "special", "signature",
    "woody", "floral", "oriental", "fruity", "spicy", "aromatic", "aquatic", "chypre", "fougere", "gourmand", "citrus",
    "oil", "attar", "concentrated", "cpo",   # perfume-oil wording (Ajmal, Al Haramain ...)
}


def as_text(v):
    """Any Airtable value (text / number / list) -> plain text."""
    # Airtable lookups can come back as lists
    if v is None:
        return ""
    if isinstance(v, list):
        return " ".join(str(x) for x in v)
    return str(v)


def norm_text(s):
    """Clean a name for comparing: lowercase, no accents/symbols, no sizes, EDP/EDT codes. Example:
    'Dior Sauvage Eau de Parfum 100ml' -> ' dior sauvage edp '"""
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
    """Letters + digits only, lowercase: 'Christian Dior' -> 'christiandior'. Used to compare brand names."""
    s = unicodedata.normalize("NFKD", as_text(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def tokens(normed):
    """Cleaned name -> set of important words (STOP words and sizes removed): {'dior', 'sauvage'}."""
    out = set()
    for t in normed.split():
        if t in STOP or re.fullmatch(r"\d+(ml|oz)", t):
            continue
        out.add(t)
    return out


def detect_type(normed):
    """Find the concentration in a cleaned name: extrait / edp / edt / edc / parfum, or None."""
    for code in ("extrait", "edp", "edt", "edc"):
        if f" {code} " in normed:
            return code
    if " cologne " in normed:
        return "edc"
    if " parfum " in normed:
        return "parfum"
    return None


def detect_gender(normed):
    """Find the gender in a cleaned name: men / women / unisex, or None if not mentioned."""
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
    """'100 ml' / 'Sauvage 100ml' -> 100.0 (None if no ml size in the text)."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", as_text(text).lower())
    return float(m.group(1)) if m else None


def norm_barcode(v):
    """Keep digits only, drop leading zeros. Less than 8 digits is not a real barcode -> None."""
    digits = re.sub(r"\D", "", as_text(v)).lstrip("0")
    return digits if len(digits) >= 8 else None


def token_hit(t, pool):
    """Is word t in the other name? Allows small spelling differences ('musc' ~ 'musk', 'magestic' ~ 'majestic')."""
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
    """How similar two names are, 0.0 - 1.0.
    coverage  = how many of OUR words are in their name (60%)
    precision = how many of THEIR words are in our name (40%)
    1.0 = same words. A match needs MATCH_THRESHOLD (0.85)."""
    if not fi_tokens or not sm_tokens:
        return 0.0
    coverage = sum(token_hit(t, sm_tokens) for t in fi_tokens) / len(fi_tokens)
    precision = sum(token_hit(t, fi_tokens) for t in sm_tokens) / len(sm_tokens)
    return round(0.6 * coverage + 0.4 * precision, 3)


# ---------------------------------------------------------------- Samawa (used by the OLD /samawa/run only)
# The new flow reads Samawa from Postgres (samawa_catalog.py fills it), not from these functions.
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


def fetch_paged(path, label, list_key, max_pages=MAX_PAGES):
    """Download all pages of a Samawa list (products or collections), 250 per page. Used by the OLD /samawa/run."""
    items, page = [], 1
    while page <= max_pages:
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


BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "10"))   # brands per batch


def collection_matches(c, bkeys):
    """Is this Samawa collection a brand page for one of these brands? (used by the OLD /samawa/run)"""
    ckey, hkey = key(c.get("title")), key(c.get("handle"))
    # exact name, or brand inside collection name (brand >= 5 letters), or "Dior" inside "Christian Dior"
    return any(bk and (bk in (ckey, hkey) or (len(bk) >= 5 and (bk in ckey or bk in hkey))
                       or (len(ckey) >= 4 and ckey in bk)) for bk in bkeys)


def fetch_brand_collections(brands, collections):
    """Products from Samawa brand collections for these brands only (complete, not affected by 25,000 cap)."""
    bkeys = [key(b) for b in brands]
    by_id = {}
    for c in collections:
        if collection_matches(c, bkeys):
            log(f"[SAMAWA] Brand collection found: '{c.get('title')}' ({c.get('handle')})")
            for p in fetch_paged(f"/collections/{c['handle']}/products.json", c["handle"], "products", max_pages=20):
                by_id[p["id"]] = p
    log(f"[SAMAWA] From brand collections: {len(by_id)} products")
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
    """Samawa entry -> Airtable Competitor fields (OLD /samawa/run only)."""
    return {C_URL: f"{SAMAWA_BASE}/products/{e['handle']}", C_PRICE: e["price"], C_STOCK: e["available"]}


# ---------------------------------------------------------------- Airtable
# Airtable is only READ now (French Inventories + brands). Nothing is written back.
def at_list(table, params):
    """READ all records of an Airtable table (100 per page, follows 'offset' until the end).
    Waits 30s and retries when Airtable says too many requests (429)."""
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
    """WRITE to Airtable, 10 records per request. NOT USED anymore (Airtable is read-only now), kept for reference."""
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
    """French Inventories products of ONE brand (OLD /samawa/run with ?brands=...)."""
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


def fetch_all_brand_products():
    """Returns {brand name: [French Inventories records]} for all brands (or only ticked ones)."""
    params = {"fields[]": [BRANDS_NAME_FIELD]}
    if BRANDS_TRACK_FIELD:
        params["filterByFormula"] = f"{{{BRANDS_TRACK_FIELD}}}"
    id_to_name = {}
    for r in at_list(BRANDS_TABLE, params):
        name = as_text(r.get("fields", {}).get(BRANDS_NAME_FIELD)).strip()
        if name and not name.isdigit():            # skip empty / barcode-like junk brand rows
            id_to_name[r["id"]] = name
    log(f"[BRANDS] {len(id_to_name)} brands read from '{BRANDS_TABLE}'")

    log("[FI] Reading all French Inventories records ...")
    groups = {}
    for rec in at_list(FI_TABLE, {"fields[]": [F_BRAND, F_BARCODE, F_PERFUME, F_PRODUCT, F_SIZE, F_TYPE, F_CATEGORY]}):
        for bid in rec.get("fields", {}).get(F_BRAND, []):      # Brand = linked record ids
            if bid in id_to_name:
                groups.setdefault(id_to_name[bid], []).append(rec)
                break
    log(f"[FI] {sum(len(v) for v in groups.values())} products in {len(groups)} brands (brands without products skipped)")
    return groups


# ---------------------------------------------------------------- matching
# match_one() is shared: the old /samawa/run and the new /competitor/match-samawa both use it.
def pick_by_size(entries, size):
    """From several variants of one Samawa product, pick the one with the same ml size (else the first)."""
    if size:
        for e in entries:
            if e["size"] and abs(e["size"] - size) <= 0.5:
                return e
    return entries[0]


def match_one(fields, brand, brand_entries, by_barcode, trust_barcode=False):
    """THE MATCHING RULES - find ONE French Inventories product on the competitor site.

    fields        : our product (Product Name, Perfume Name, Size, Type, Category, Barcode)
    brand         : our brand name
    brand_entries : competitor products of the same brand (from build_index / sm_entries_from_db)
    by_barcode    : competitor products by barcode

    Steps:
      1. Remove impossible candidates: different ml size, or men vs women.
      2. Barcode: same barcode AND name looks right -> match.
      3. Name: best name_score >= MATCH_THRESHOLD (0.85) -> match.
         EDP vs EDT costs -0.12, so it only matches if the name is almost perfect.
         Two different products with the same score -> "ambiguous" (no match, sent as suggestion).

    Returns (match, method, score, guess)
      match  = competitor product or None
      method = "barcode" / "fuzzy" / reason for no match (e.g. "low score (...)")
      guess  = best candidate when there is no match (becomes the SUGGESTION link)
    """
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
        if s is not None and (trust_barcode or (not fi_tokens) or (s >= 0.6 and s >= top - 0.001)):
            return e, method("barcode", e), s, None
        log(f"[BARCODE-REJECT] {product_name} -> barcode points to '{e['title']}' (score={s}), using name match")

    # 2) fuzzy
    if not fi_tokens:
        return None, "no name tokens", 0.0, None
    if not scored:
        return None, "no candidate with same size/type/gender", 0.0, None
    best_score, best = scored[0]
    if best_score < MATCH_THRESHOLD:
        return None, f"low score (best: {best['title']})", best_score, best
    tied = [e for s, e in scored if s >= best_score - 0.02]
    if len({e["handle"] for e in tied}) > 1:
        if all(name_score(fi_tokens, e["tokens"] - brand_tokens) >= 0.95 for e in tied):
            # Samawa has duplicate listings of the same perfume -> prefer in stock, then cheapest
            best = sorted(tied, key=lambda e: (not e["available"], e["price"]))[0]
            log(f"[DUPLICATE] {product_name}: {len(tied)} Samawa listings, picked '{best['title']}'")
        else:
            return None, f"ambiguous ({tied[0]['title']} | {tied[1]['title']})", best_score, tied[0]
    return best, method("fuzzy", best), best_score, None


def run_match(brands, rematch=False, fi_groups=None):
    """OLD /samawa/run: downloads Samawa + reads Airtable, matches, but only LOGS the result (saves nothing).
    Replaced by /competitor/load-fi + /competitor/match-samawa. Kept for reference."""
    started = time.time()
    batches = [brands[i:i + BATCH_SIZE] for i in range(0, len(brands), BATCH_SIZE)]
    log(f"[RUN] rematch={rematch} | {len(brands)} brands in {len(batches)} batches of {BATCH_SIZE}")
    summary = {"brands": len(brands), "rematch": rematch, "batches_done": 0, "batches_total": len(batches),
               "matched_barcode": 0, "matched_fuzzy": 0, "existing_link_refreshed": 0, "link_gone": 0,
               "unmatched": 0, "created": 0, "updated": 0, "unmatched_list": []}
    state["last_summary"] = summary          # live progress in /samawa/status

    # 1) things downloaded ONCE for the whole run
    state["catalog_incomplete"] = False
    log("[SAMAWA] Downloading full catalog from products.json ...")
    base_entries, base_barcode, base_handle = build_index(fetch_paged("/products.json", "catalog", "products"))
    collections = fetch_paged("/collections.json", "collections", "collections")
    base_incomplete = state["catalog_incomplete"]

    comp_rows = at_list(COMP_TABLE, {"fields[]": [C_LINK_FI, C_URL]})
    comp_by_fi = {}
    for row in comp_rows:
        for fid in row.get("fields", {}).get(C_LINK_FI, []):
            comp_by_fi[fid] = row
    log(f"[COMP] {len(comp_rows)} existing Competitor rows")

    # 2) brands 10 by 10
    for bi, batch in enumerate(batches, 1):
        log(f"\n########## BATCH {bi}/{len(batches)}: {batch} ##########")
        state["catalog_incomplete"] = base_incomplete
        c_entries, c_barcode, c_handle = build_index(fetch_brand_collections(batch, collections))
        entries = base_entries + c_entries
        by_barcode = {**base_barcode, **c_barcode}
        by_handle = {**base_handle, **c_handle}
        safe_mode = state["catalog_incomplete"]
        if safe_mode:
            log("[SAFE-MODE] Samawa download incomplete -> will NOT clear links or untick stock in this batch")

        creates, updates = [], []
        for brand in batch:
            bkey, bnorm = key(brand), norm_text(brand)
            # vendor "Dior" also counts for brand "Christian Dior" (vendor name inside brand name)
            brand_entries = [e for e in entries if e["vendor_key"].startswith(bkey) or bnorm in e["title_norm"]
                             or (len(e["vendor_key"]) >= 4 and e["vendor_key"] in bkey)]
            recs = fi_groups[brand] if fi_groups is not None else fetch_fi_for_brand(brand)
            log(f"\n[BRAND] ===== {brand}: {len(recs)} products | {len(brand_entries)} Samawa variants =====")

            for rec in recs:
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
                e, method, score, guess = match_one(f, brand, brand_entries, by_barcode)
                note = {}
                if e:
                    summary["matched_barcode" if method.startswith("barcode") else "matched_fuzzy"] += 1
                    log(f"[MATCH:{method} {score}] {pname} -> {e['title']} | AED {e['price']} | stock={e['available']}")
                    sf = samawa_fields(e)
                    if SUGGESTIONS:
                        sf[C_SUGGEST] = None                             # matched -> clear old suggestion
                else:
                    summary["unmatched"] += 1
                    if len(summary["unmatched_list"]) < 300:
                        summary["unmatched_list"].append(f"{pname} | {method} | {score}")
                    log(f"[NO MATCH] {pname} | {method} | score={score}")
                    sf = {}
                    if SUGGESTIONS:
                        # best guess (score >= 0.5) so the team only needs to verify, not search
                        good = guess is not None and score >= 0.5
                        note = {C_SUGGEST: f"{SAMAWA_BASE}/products/{guess['handle']}" if good else None}

                if row:
                    if sf:
                        updates.append({"id": row["id"], "fields": sf})
                    elif rematch and not safe_mode:
                        # rematch: clear the old (possibly wrong) link
                        updates.append({"id": row["id"], "fields": {C_URL: None, C_PRICE: None, C_STOCK: False, **note}})
                    elif note:
                        updates.append({"id": row["id"], "fields": note})
                else:
                    creates.append({"fields": {C_NAME: pname, C_LINK_FI: [fid], **sf, **note}})

        # Airtable upload removed - results only in logs and /samawa/status
        log(f"\n[NO-UPLOAD] Batch {bi}/{len(batches)}: {len(creates)} new + {len(updates)} updated rows NOT sent to Airtable")
        summary["created"] += len(creates)
        summary["updated"] += len(updates)
        summary["batches_done"] = bi
        summary["seconds"] = round(time.time() - started, 1)
        log(f"[BATCH DONE] {bi}/{len(batches)} | matched {summary['matched_barcode'] + summary['matched_fuzzy']} "
            f"| unmatched {summary['unmatched']} | {summary['seconds']}s")
        time.sleep(5)   # small pause between batches (gentle on Samawa)

    summary["catalog_incomplete"] = base_incomplete
    summary["seconds"] = round(time.time() - started, 1)
    log(f"[DONE] {summary}")
    return summary



# ================================================================ COMPETITOR TABLE (Postgres)  <- NEW FLOW
# Step 1: /competitor/load-fi       -> all French Inventories products -> competitor_table
# Step 2: /competitor/match-samawa  -> match every product to samawa_catalog -> link, price, stock, suggestion
# Step 3: /competitor/match-ff      -> same for French Fragrance (french_fragrance_catalog) -> ff_* columns
# Step 4: /competitor/match-vp      -> same for V Perfumes (vperfumes_catalog) -> vp_* columns
#
# competitor_table = ONE row per French Inventories product:
#   fi_record_id           Airtable record id of the product (never changes -> used to update the right row)
#   french_inventory_code  Item ID, e.g. "ADP1018/ Acqua Di Parma Ambra 180 ml EDP Perfume"
#   sku, product_name, uae_price
#   brand, barcode, perfume_name   (not shown on the page, only used for matching)
#   samawa_link / samawa_price / samawa_stock   filled when a match is found
#   samawa_suggestion      best guess link when NO match (team checks it by hand)
#   samawa_method / samawa_score   why it matched or not (e.g. "fuzzy" 0.92, "low score" 0.61)
#   ff_link / ff_price / ff_stock / ff_suggestion / ff_method / ff_score   same, for French Fragrance
#   bp_link / bp_price / bp_stock   Branded Perfume  - taken from the French Fragrance match (same products)
#   es_link / es_price / es_stock   Essenzi          - taken from the French Fragrance match (same products)
#   vp_link / vp_price / vp_stock / vp_suggestion / vp_method / vp_score   V Perfumes (own matching, barcode first)
#   <shop>_title           the competitor's own product name (shown as the link text on the page)
#   <shop>_confirmed       TRUE = suggestion accepted with "OK" -> later match runs keep this link
#   <shop>_rejected        link refused with "Not OK" -> never suggested again for this product
#   least_price            lowest price: ours (always) + every shop that has it IN STOCK, VAT included
#   least_priced_website   who has that price; ties are all listed, e.g. "Fragrant Souq, Samawa"
#   suggested_price        lowest IN-STOCK COMPETITOR price (ours not included) minus 5%, e.g. 83 -> 78.85
import psycopg2
import psycopg2.extras
from psycopg2.extras import execute_values

F_ITEM_ID = "Item ID"          # French Inventories fields (in addition to F_BRAND, F_BARCODE ... above)
F_SKU = "SKU"
F_UAE_PRICE = "UAE Price"

# Creates the table the first time; "ADD COLUMN IF NOT EXISTS" adds new columns later without losing data
CT_CREATE = """
CREATE TABLE IF NOT EXISTS competitor_table (
    id SERIAL PRIMARY KEY,
    fi_record_id TEXT UNIQUE NOT NULL,
    updated_at TIMESTAMP DEFAULT NOW()
);
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS french_inventory_code TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS sku TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS product_name TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS uae_price NUMERIC(10,2);
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS brand TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS barcode TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS perfume_name TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS samawa_link TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS samawa_price NUMERIC(10,2);
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS samawa_stock BOOLEAN DEFAULT FALSE;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS samawa_suggestion TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS samawa_method TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS samawa_score NUMERIC(5,3);
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS ff_link TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS ff_price NUMERIC(10,2);
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS ff_stock BOOLEAN DEFAULT FALSE;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS ff_suggestion TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS ff_method TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS ff_score NUMERIC(5,3);
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS bp_link TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS bp_price NUMERIC(10,2);
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS bp_stock BOOLEAN DEFAULT FALSE;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS es_link TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS es_price NUMERIC(10,2);
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS es_stock BOOLEAN DEFAULT FALSE;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS vp_link TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS vp_price NUMERIC(10,2);
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS vp_stock BOOLEAN DEFAULT FALSE;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS vp_suggestion TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS vp_method TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS vp_score NUMERIC(5,3);
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS samawa_title TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS ff_title TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS bp_title TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS es_title TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS vp_title TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS samawa_confirmed BOOLEAN DEFAULT FALSE;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS ff_confirmed BOOLEAN DEFAULT FALSE;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS vp_confirmed BOOLEAN DEFAULT FALSE;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS samawa_rejected TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS ff_rejected TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS vp_rejected TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS least_price NUMERIC(10,2);
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS least_priced_website TEXT;
ALTER TABLE competitor_table ADD COLUMN IF NOT EXISTS suggested_price NUMERIC(10,2);
CREATE INDEX IF NOT EXISTS idx_ct_sku ON competitor_table (sku);
CREATE INDEX IF NOT EXISTS idx_ct_brand ON competitor_table (brand);
"""

# Step 1 save: new product -> insert, existing product (same fi_record_id) -> update its details.
# Samawa columns are NOT touched here, so re-running step 1 keeps the match results.
CT_UPSERT_FI = """
INSERT INTO competitor_table
    (fi_record_id, french_inventory_code, sku, product_name, uae_price,
     brand, barcode, perfume_name)
VALUES %s
ON CONFLICT (fi_record_id) DO UPDATE SET
    french_inventory_code = EXCLUDED.french_inventory_code,
    sku          = EXCLUDED.sku,
    product_name = EXCLUDED.product_name,
    uae_price    = EXCLUDED.uae_price,
    brand        = EXCLUDED.brand,
    barcode      = EXCLUDED.barcode,
    perfume_name = EXCLUDED.perfume_name,
    updated_at   = NOW();
"""

def ct_update_sql(prefix):
    """Save query for one shop: updates only that shop's columns (samawa_* or ff_*) of many rows at once."""
    return f"""
UPDATE competitor_table AS c SET
    {prefix}_link       = v.link,
    {prefix}_price      = v.price::numeric,
    {prefix}_stock      = v.stock::boolean,
    {prefix}_suggestion = v.suggestion,
    {prefix}_method     = v.method,
    {prefix}_score      = v.score::numeric,
    {prefix}_title      = v.title,
    updated_at          = NOW()
FROM (VALUES %s) AS v(fi_record_id, link, price, stock, suggestion, method, score, title)
WHERE c.fi_record_id = v.fi_record_id;
"""


def pg_conn():
    """Open a connection to the Postgres database french_fragrance_db."""
    url = os.environ.get("FF_DATABASE_URL")
    if not url:
        raise RuntimeError("FF_DATABASE_URL is not set")
    log("[PG] Opening connection to french_fragrance_db")
    return psycopg2.connect(url, sslmode="require")


def first_text(v):
    """Airtable lookups / multi-selects come back as lists -> first value as text."""
    if isinstance(v, list):
        v = v[0] if v else None
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def first_number(v):
    """Airtable number / currency / lookup -> number with 2 decimals (None if empty)."""
    if isinstance(v, list):
        v = v[0] if v else None
    try:
        return round(float(v), 2) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- step 1: French Inventories -> Postgres
def ct_load_fi():
    """STEP 1 - copy ALL French Inventories products from Airtable into competitor_table.

    1. Read the brands table (Brand in French Inventories is a link, so we need id -> brand name).
    2. Read every French Inventories product (Item ID, SKU, Product Name, UAE Price, Brand, Barcode, Perfume Name).
    3. Save them in Postgres, 500 at a time (insert new / update existing).
    """
    started = time.time()
    summary = {"step": "load-fi", "products": 0, "without_brand": 0, "bad_price": []}
    state["last_summary"] = summary

    # brand record id -> brand name (Brand in French Inventories is a linked field)
    brand_names = {}
    for r in at_list(BRANDS_TABLE, {"fields[]": [BRANDS_NAME_FIELD]}):
        name = as_text(r.get("fields", {}).get(BRANDS_NAME_FIELD)).strip()
        if name:
            brand_names[r["id"]] = name
    log(f"[CT-LOAD] {len(brand_names)} brands read from '{BRANDS_TABLE}'")

    log("[CT-LOAD] Reading all French Inventories products from Airtable ...")
    recs = at_list(FI_TABLE, {"fields[]": [F_ITEM_ID, F_SKU, F_PRODUCT, F_UAE_PRICE, F_BRAND, F_BARCODE,
                                           F_PERFUME]})
    log(f"[CT-LOAD] {len(recs)} French Inventories records downloaded")

    rows = []
    for rec in recs:
        f = rec.get("fields", {})
        brand = next((brand_names[b] for b in (f.get(F_BRAND) or []) if b in brand_names), None)
        if not brand:
            summary["without_brand"] += 1

        # A price of 1,000,000+ is a typing mistake in Airtable (usually a barcode in the price field).
        # Save it as empty instead of crashing, and list it so it can be fixed in French Inventories.
        uae_price = first_number(f.get(F_UAE_PRICE))
        if uae_price is not None and abs(uae_price) >= 1_000_000:
            item_id = first_text(f.get(F_ITEM_ID))
            log(f"[CT-LOAD] WRONG UAE PRICE {uae_price} for '{item_id}' -> saved as empty, please fix in Airtable")
            summary["bad_price"].append(f"{item_id} | {uae_price}")
            uae_price = None

        rows.append((
            rec["id"], first_text(f.get(F_ITEM_ID)), first_text(f.get(F_SKU)), first_text(f.get(F_PRODUCT)),
            uae_price, brand, first_text(f.get(F_BARCODE)), first_text(f.get(F_PERFUME)),
        ))

    conn = pg_conn()
    cur = conn.cursor()
    try:
        log("[CT-LOAD] Creating / updating competitor_table columns")
        cur.execute(CT_CREATE)
        conn.commit()
        for i in range(0, len(rows), 500):
            execute_values(cur, CT_UPSERT_FI, rows[i:i + 500])
            conn.commit()
            summary["products"] = min(i + 500, len(rows))
            log(f"[CT-LOAD] Saved {summary['products']}/{len(rows)} products")
    finally:
        cur.close()
        conn.close()

    summary["seconds"] = round(time.time() - started, 1)
    log(f"[CT-LOAD DONE] {summary}")
    summary["least"] = ct_update_least()          # our UAE prices may have changed -> recalculate least price
    return summary


# ---------------------------------------------------------------- step 2 / 3: match to a competitor shop
def sm_entries_from_db(cur):
    """samawa_catalog rows -> same entry format build_index() makes, so match_one() works unchanged."""
    cur.execute("SELECT product_url, gtin, brand, name, price, stock, volume FROM samawa_catalog "
                "WHERE stock <> 'Not on site'")
    entries, by_barcode = [], {}
    for url, gtin, brand, name, price, stock, volume in cur.fetchall():
        name = name or ""
        full_n = norm_text(name)
        e = {
            "handle": urlparse(url or "").path.rstrip("/").split("/products/")[-1],
            "url": url,
            "title": name,
            "vendor_key": key(brand),
            "title_key": key(name),
            "title_norm": full_n,
            "size": parse_size(volume) or parse_size(name),
            "type": detect_type(full_n),
            "gender": detect_gender(full_n),
            "tokens": tokens(norm_text(re.split(r"\s-\s|,", name)[0])),   # drop " - 100ml" variant part
            "price": float(price or 0),
            "available": stock == "In stock",
        }
        entries.append(e)
        bc = norm_barcode(gtin)
        if bc:
            by_barcode[bc] = e
    log(f"[CT-MATCH] {len(entries)} Samawa variants loaded from samawa_catalog | {len(by_barcode)} with barcode")
    return entries, by_barcode


def ff_entries_from_db(cur):
    """french_fragrance_catalog rows -> same entry format as Samawa, so match_one() works unchanged.
    French Fragrance has no brand column: the brand is found inside the product name instead."""
    cur.execute("SELECT product_url, gtin, name, COALESCE(price_inc_tax, price), stock, volume "
                "FROM french_fragrance_catalog")
    entries, by_barcode = [], {}
    for url, gtin, name, price, stock, volume in cur.fetchall():
        name = name or ""
        full_n = norm_text(name)
        e = {
            "handle": (url or "").rstrip("/"),
            "url": url,
            "title": name,
            "vendor_key": "",                     # no brand column -> brand is matched in title_norm
            "title_key": key(name),
            "title_norm": full_n,
            "size": parse_size(volume) or parse_size(name),
            "type": detect_type(full_n),
            "gender": detect_gender(full_n),
            "tokens": tokens(norm_text(re.split(r"\s-\s|,", name)[0])),
            "price": float(price or 0),           # price including 5% VAT when available
            "available": stock == "In stock",
        }
        entries.append(e)
        bc = norm_barcode(gtin)
        if bc:
            by_barcode[bc] = e
    log(f"[CT-MATCH] {len(entries)} French Fragrance products loaded from french_fragrance_catalog "
        f"| {len(by_barcode)} with GTIN")
    return entries, by_barcode


def vp_entries_from_db(cur):
    """vperfumes_catalog rows -> same entry format, so match_one() works unchanged.
    V Perfumes has no brand column (brand is at the start of the name) and its sku = barcode (gtin)."""
    cur.execute("SELECT to_regclass('vperfumes_catalog')")
    if cur.fetchone()[0] is None:
        log("[CT-MATCH] vperfumes_catalog does not exist yet")
        return [], {}
    cur.execute("SELECT product_url, gtin, name, COALESCE(price_inc_tax, price), stock, volume "
                "FROM vperfumes_catalog WHERE COALESCE(stock, '') <> 'Not on site'")
    entries, by_barcode = [], {}
    for url, gtin, name, price, stock, volume in cur.fetchall():
        name = name or ""
        full_n = norm_text(name)
        e = {
            "handle": (url or "").rstrip("/"),
            "url": url,
            "title": name,
            "vendor_key": "",                     # no brand column -> brand is matched in title_norm
            "title_key": key(name),
            "title_norm": full_n,
            "size": parse_size(volume) or parse_size(name),
            "type": detect_type(full_n),
            "gender": detect_gender(full_n),
            "tokens": tokens(norm_text(re.split(r"\s-\s|,", name)[0])),
            "price": float(price or 0),           # price shown on V Perfumes (VAT included)
            "available": stock == "In stock",
        }
        entries.append(e)
        bc = norm_barcode(gtin)
        if bc:
            by_barcode[bc] = e
    log(f"[CT-MATCH] {len(entries)} V Perfumes products loaded from vperfumes_catalog | {len(by_barcode)} with barcode")
    return entries, by_barcode


# One entry per competitor shop. To add a new shop later (e.g. V Perfumes):
#   1. load its products into a Postgres table, 2. write a loader like ff_entries_from_db,
#   3. add its columns to CT_CREATE, 4. add one entry here + one route below.
SHOPS = {
    "samawa": {"label": "Samawa", "prefix": "samawa", "loader": sm_entries_from_db,
               "empty_hint": "samawa_catalog is empty - run /samawa-catalog/run first",
               "trust_barcode": False},   # Samawa SKUs are sometimes wrong -> barcode must also look right by name
    "ff": {"label": "French Fragrance", "prefix": "ff", "loader": ff_entries_from_db,
           "empty_hint": "french_fragrance_catalog is empty - load it first",
           "trust_barcode": True},       # French Fragrance GTINs are reliable -> barcode match is trusted
    "vp": {"label": "V Perfumes", "prefix": "vp", "loader": vp_entries_from_db,
           "empty_hint": "vperfumes_catalog is empty - run /vperfumes/run first",
           "trust_barcode": True},       # V Perfumes sku = real barcode -> barcode match is trusted
}


def ct_match_shop(shop_key):
    """STEP 2 / 3 - find every competitor_table product on ONE shop and save link / price / stock / suggestion.

    1. Load all products of the shop from Postgres (samawa_catalog or french_fragrance_catalog).
    2. Load all our products from competitor_table and group them by brand.
    3. For each brand: take only that shop's products of the same brand, run match_one() for each of ours.
         matched      -> <shop>_link, <shop>_price, <shop>_stock, suggestion cleared
         not matched  -> link/price cleared, suggestion = best guess if its score >= 0.5
    4. Save every ~500 products, so results appear on /competitors while it runs.
    Size, EDP/EDT and gender are read from the product name.
    """
    shop = SHOPS[shop_key]
    tag = f"CT-{shop_key.upper()}"                # log prefix: [CT-SAMAWA ...] / [CT-FF ...]
    started = time.time()
    summary = {"step": f"match-{shop_key}", "products": 0, "matched_barcode": 0, "matched_fuzzy": 0,
               "unmatched": 0, "with_suggestion": 0, "skipped_no_brand": 0, "brands_done": 0}
    state["last_summary"] = summary

    conn = pg_conn()
    cur = conn.cursor()
    try:
        cur.execute(CT_CREATE)                    # adds the shop's columns if they don't exist yet
        conn.commit()
        entries, by_barcode = shop["loader"](cur)
        if not entries:
            summary["error"] = shop["empty_hint"]
            log(f"[{tag}] {summary['error']}")
            return summary

        p = shop["prefix"]
        # links confirmed with "OK" (kept) and links refused with "Not OK" (never suggested again)
        cur.execute(f"SELECT fi_record_id, {p}_link, {p}_rejected, {p}_confirmed FROM competitor_table")
        confirmed, rejected = {}, {}
        for fid, link, rej, conf in cur.fetchall():
            if conf and link:
                confirmed[fid] = link
            if rej:
                rejected[fid] = rej
        by_url = {e["url"]: e for e in entries}
        log(f"[{tag}] {len(confirmed)} confirmed links kept, {len(rejected)} refused suggestions remembered")

        cur.execute("SELECT fi_record_id, brand, product_name, perfume_name, barcode FROM competitor_table")
        groups = {}
        for fid, brand, pname, perfume, barcode in cur.fetchall():
            summary["products"] += 1
            if not brand:
                summary["skipped_no_brand"] += 1
                continue
            # size, type (EDP/EDT) and gender are read from the product name, e.g. "... 100 ml EDP Men Perfume"
            fields = {F_PRODUCT: pname, F_PERFUME: perfume, F_SIZE: None, F_TYPE: None,
                      F_CATEGORY: pname, F_BARCODE: barcode}
            groups.setdefault(brand, []).append((fid, fields))
        brands = sorted(groups)
        summary["brands_total"] = len(brands)
        log(f"[{tag}] {summary['products']} products in {len(brands)} brands "
            f"({summary['skipped_no_brand']} without brand skipped)")

        save_sql = ct_update_sql(shop["prefix"])
        updates = []
        for bi, brand in enumerate(brands, 1):
            bkey, bnorm = key(brand), norm_text(brand)
            # same brand = shop's brand starts with ours, or our brand name is inside the product title,
            # or shop brand "Dior" inside our "Christian Dior"
            brand_entries = [e for e in entries if (e["vendor_key"] and e["vendor_key"].startswith(bkey))
                             or bnorm in e["title_norm"]
                             or (len(e["vendor_key"]) >= 4 and e["vendor_key"] in bkey)]
            if shop_key in ("ff", "vp"):
                # French Fragrance / V Perfumes titles start with the FULL brand ("Christian Dior Sauvage ...").
                # Our brand is "Dior" -> the word "christian" would lower the name score.
                # So for FF, ignore the words written BEFORE our brand name in the title.
                trimmed = []
                for e in brand_entries:
                    pos = e["title_norm"].find(bnorm)
                    if pos > 0:
                        e = {**e, "tokens": e["tokens"] - tokens(e["title_norm"][:pos])}
                    trimmed.append(e)
                brand_entries = trimmed
            log(f"\n[{tag}-BRAND] {bi}/{len(brands)} {brand}: {len(groups[brand])} products | "
                f"{len(brand_entries)} {shop['label']} products")

            for fid, fields in groups[brand]:
                pname = fields[F_PRODUCT] or fields[F_PERFUME] or fid
                if fid in confirmed:
                    # confirmed by the team -> keep the link, only refresh price / stock / name
                    ce = by_url.get(confirmed[fid])
                    if ce:
                        updates.append((fid, ce["url"], ce["price"], ce["available"], None, "confirmed", None, ce["title"]))
                    else:
                        log(f"[{tag}-CONFIRMED] {pname} -> link no longer in catalog, stock set to out")
                        updates.append((fid, confirmed[fid], None, False, None, "confirmed", None, None))
                    summary["kept_confirmed"] = summary.get("kept_confirmed", 0) + 1
                    continue
                e, method, score, guess = match_one(fields, brand, brand_entries, by_barcode,
                                                    trust_barcode=shop["trust_barcode"])
                if e:
                    summary["matched_barcode" if method.startswith("barcode") else "matched_fuzzy"] += 1
                    log(f"[{tag}-MATCH:{method} {score}] {pname} -> {e['title']} | AED {e['price']} | stock={e['available']}")
                    updates.append((fid, e["url"], e["price"], e["available"], None, method, score, e["title"]))
                else:
                    summary["unmatched"] += 1
                    suggestion = guess["url"] if guess is not None and score >= 0.5 else None
                    if suggestion and suggestion == rejected.get(fid):
                        suggestion = None                 # team said "Not OK" to this link before
                    if suggestion:
                        summary["with_suggestion"] += 1
                    log(f"[{tag}-NO MATCH] {pname} | {method} | score={score}"
                        + (f" | suggestion {suggestion}" if suggestion else ""))
                    updates.append((fid, None, None, False, suggestion, method[:200], score, None))

            # save every 500 products so results appear while it runs
            if len(updates) >= 500 or bi == len(brands):
                execute_values(cur, save_sql, updates)
                conn.commit()
                log(f"[{tag}-SAVE] {len(updates)} rows written to competitor_table")
                updates = []
            summary["brands_done"] = bi
            summary["seconds"] = round(time.time() - started, 1)
    finally:
        cur.close()
        conn.close()

    summary["seconds"] = round(time.time() - started, 1)
    log(f"[{tag} DONE] {summary}")
    summary["least"] = ct_update_least()          # new shop prices -> recalculate least price
    return summary


def ct_match_samawa():
    """STEP 2 - match all products to Samawa."""
    return ct_match_shop("samawa")


def ct_match_ff():
    """STEP 3 - match all products to French Fragrance."""
    return ct_match_shop("ff")


def ct_match_vp():
    """STEP 4 - match all products to V Perfumes."""
    return ct_match_shop("vp")


# ---------------------------------------------------------------- Branded Perfume / Essenzi + least price
# Branded Perfume and Essenzi sell the SAME products as French Fragrance (same company, same links).
# So a product matched to French Fragrance (ff_link) is also matched to them:
#   competitor_table.ff_link = french_fragrance_catalog.product_url
#   french_fragrance_catalog.id = branded_perfume_catalog.ff_id / essenzi_catalog.ff_id
SISTER_LINKS = [
    # (label, prefix in competitor_table, catalog table, catalog column prefix)
    ("Branded Perfume", "bp", "branded_perfume_catalog", "bp"),
    ("Essenzi", "es", "essenzi_catalog", "es"),
]

# All offers: our UAE price ALWAYS + every competitor only when IN STOCK with a real price (all include VAT).
# Order number = order in which tied websites are listed.
LEAST_SQL = """
UPDATE competitor_table SET least_price = NULL, least_priced_website = NULL;

WITH offers AS (
    -- OUR price: always compared (our own stock is not checked)
    SELECT fi_record_id, 'Fragrant Souq' AS site, uae_price AS price, 1 AS ord
      FROM competitor_table WHERE uae_price > 0
    UNION ALL
    SELECT fi_record_id, 'Samawa', samawa_price, 2
      FROM competitor_table WHERE samawa_stock AND samawa_link IS NOT NULL AND samawa_price > 0
    UNION ALL
    SELECT fi_record_id, 'French Fragrance', ff_price, 3
      FROM competitor_table WHERE ff_stock AND ff_link IS NOT NULL AND ff_price > 0
    UNION ALL
    SELECT fi_record_id, 'Branded Perfume', bp_price, 4
      FROM competitor_table WHERE bp_stock AND bp_link IS NOT NULL AND bp_price > 0
    UNION ALL
    SELECT fi_record_id, 'Essenzi', es_price, 5
      FROM competitor_table WHERE es_stock AND es_link IS NOT NULL AND es_price > 0
    UNION ALL
    SELECT fi_record_id, 'V Perfumes', vp_price, 6
      FROM competitor_table WHERE vp_stock AND vp_link IS NOT NULL AND vp_price > 0
),
best AS (
    SELECT fi_record_id, MIN(price) AS min_price FROM offers GROUP BY fi_record_id
),
winners AS (   -- every website that has the lowest price (ties -> all of them)
    SELECT o.fi_record_id, b.min_price, string_agg(o.site, ', ' ORDER BY o.ord) AS sites
    FROM offers o JOIN best b ON b.fi_record_id = o.fi_record_id AND o.price = b.min_price
    GROUP BY o.fi_record_id, b.min_price
)
UPDATE competitor_table c
SET least_price = w.min_price, least_priced_website = w.sites
FROM winners w
WHERE c.fi_record_id = w.fi_record_id;
"""


# Suggested price settings - change here later (e.g. 0.03 = 3% below, 0 decimals = whole AED)
SUGGEST_BELOW = 0.05        # 5% below the cheapest in-stock competitor
SUGGEST_DECIMALS = 0        # 78.85 -> 79 (nearest whole AED; 2 = keep 78.85)

# suggested_price = cheapest in-stock COMPETITOR (Samawa / FF / Branded Perfume / Essenzi / V Perfumes) minus 5%
SUGGEST_SQL = f"""
UPDATE competitor_table SET suggested_price = NULL;

WITH competitor_offers AS (
    SELECT fi_record_id, samawa_price AS price FROM competitor_table
      WHERE samawa_stock AND samawa_link IS NOT NULL AND samawa_price > 0
    UNION ALL
    SELECT fi_record_id, ff_price FROM competitor_table WHERE ff_stock AND ff_link IS NOT NULL AND ff_price > 0
    UNION ALL
    SELECT fi_record_id, bp_price FROM competitor_table WHERE bp_stock AND bp_link IS NOT NULL AND bp_price > 0
    UNION ALL
    SELECT fi_record_id, es_price FROM competitor_table WHERE es_stock AND es_link IS NOT NULL AND es_price > 0
    UNION ALL
    SELECT fi_record_id, vp_price FROM competitor_table WHERE vp_stock AND vp_link IS NOT NULL AND vp_price > 0
)
UPDATE competitor_table c
SET suggested_price = ROUND(m.min_price * (1 - {SUGGEST_BELOW}), {SUGGEST_DECIMALS})
FROM (SELECT fi_record_id, MIN(price) AS min_price FROM competitor_offers GROUP BY fi_record_id) m
WHERE c.fi_record_id = m.fi_record_id;
"""


def ct_update_least():
    """1. Copy Branded Perfume + Essenzi price/stock into competitor_table (via the French Fragrance match).
    2. Recalculate least_price + least_priced_website + suggested_price for every product.
    Runs automatically after load-fi / match-samawa / match-ff, and by /competitor/update-least."""
    started = time.time()
    result = {}
    conn = pg_conn()
    cur = conn.cursor()
    try:
        cur.execute(CT_CREATE)
        for label, p, table, cp in SISTER_LINKS:
            cur.execute("SELECT to_regclass(%s)", (table,))
            if cur.fetchone()[0] is None:
                log(f"[CT-LEAST] {table} does not exist yet -> {label} skipped")
                continue
            # clear old values, then take them from the sister catalog through the French Fragrance match
            cur.execute(f"UPDATE competitor_table SET {p}_link = NULL, {p}_price = NULL, {p}_stock = FALSE, "
                        f"{p}_title = NULL")
            # product name on that site if the catalog has it (filled by the PC scraper), else French Fragrance name
            cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
                        (table, f"{cp}_title"))
            title_sql = f"COALESCE(s.{cp}_title, f.name)" if cur.fetchone() else "f.name"
            cur.execute(f"""
                UPDATE competitor_table c SET
                    {p}_link  = s.{cp}_url,
                    {p}_title = {title_sql},
                    {p}_price = COALESCE(s.{cp}_price_inc_tax, s.{cp}_price),
                    {p}_stock = (s.{cp}_stock = 'In stock')
                FROM french_fragrance_catalog f
                JOIN {table} s ON s.ff_id = f.id
                WHERE c.ff_link = f.product_url
                  AND COALESCE(s.{cp}_stock, '') <> 'Not on site'
            """)
            result[p] = cur.rowcount
            log(f"[CT-LEAST] {label}: {cur.rowcount} products linked through the French Fragrance match")

        cur.execute(LEAST_SQL)
        cur.execute(SUGGEST_SQL)
        cur.execute("SELECT COUNT(*) FROM competitor_table WHERE suggested_price IS NOT NULL")
        result["with_suggested_price"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM competitor_table WHERE least_price IS NOT NULL")
        result["with_least_price"] = cur.fetchone()[0]
        cur.execute("SELECT least_priced_website, COUNT(*) FROM competitor_table "
                    "WHERE least_priced_website IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 10")
        result["cheapest_counts"] = {site: n for site, n in cur.fetchall()}
        conn.commit()
    finally:
        cur.close()
        conn.close()
    result["seconds"] = round(time.time() - started, 1)
    log(f"[CT-LEAST DONE] {result}")
    return result


def ct_update_least_job():
    """For /competitor/update-least (e.g. after the Branded Perfume / Essenzi scrapers finished)."""
    summary = {"step": "update-least"}
    state["last_summary"] = summary
    summary.update(ct_update_least())
    return summary


def _ct_worker(fn):
    """Background thread for step 1 / step 2. Always releases run_lock at the end, even after an error."""
    try:
        state["last_summary"] = fn()
    except Exception as ex:
        log(f"[CT-ERROR] {ex}\n{traceback.format_exc()}")
        state["last_summary"] = {"error": str(ex)}
    finally:
        run_lock.release()


# ---------------------------------------------------------------- routes
def _worker(brands, rematch=False):
    """Background thread for the OLD /samawa/run (the browser gets an answer immediately, work continues here)."""
    try:
        fi_groups = None
        if not brands:                                  # no brands in URL -> all brands from brands table
            fi_groups = fetch_all_brand_products()
            brands = sorted(fi_groups)
        if not brands:
            log(f"[RUN] No brands found in '{BRANDS_TABLE}' -> nothing to do")
            state["last_summary"] = {"error": f"no brands found in {BRANDS_TABLE}"}
            return
        state["last_summary"] = run_match(brands, rematch, fi_groups)
    except Exception as ex:
        log(f"[ERROR] {ex}\n{traceback.format_exc()}")
        state["last_summary"] = {"error": str(ex)}
    finally:
        run_lock.release()


# ================================================================ URLs (routes)
# Every long job starts in a background thread and answers the browser immediately with {"started": true}.
# Watch progress at /samawa/status or in Render -> Logs.

@app.get("/")
def health():
    """Health check - Render / you can open / to see the service is alive."""
    return jsonify({"ok": True, "service": "samawa-matcher"})


@app.post("/samawa/run")
def trigger():
    """OLD run started by a script (POST, secret in header X-Run-Secret). Logs only."""
    if RUN_SECRET and request.headers.get("X-Run-Secret") != RUN_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    brands = body.get("brands") or []          # empty -> brands table
    rematch = bool(body.get("rematch"))
    if not run_lock.acquire(blocking=False):
        return jsonify({"error": "a run is already in progress"}), 409
    log(f"[RUN] Started for brands: {brands or 'from brands table'}")
    threading.Thread(target=_worker, args=(brands, rematch), daemon=True).start()
    return jsonify({"started": True, "brands": brands or "from brands table", "rematch": rematch}), 202


@app.get("/samawa/run")
def trigger_from_url():
    # Browser-friendly trigger: /samawa/run?secret=XXX&brands=Afnan,Armaf
    if RUN_SECRET and request.args.get("secret") != RUN_SECRET:
        log("[RUN-URL] Unauthorized attempt")
        return jsonify({"error": "unauthorized"}), 401
    brands = [b.strip() for b in request.args.get("brands", "").split(",") if b.strip()]  # empty -> brands table
    rematch = request.args.get("rematch") == "1"
    if not run_lock.acquire(blocking=False):
        log("[RUN-URL] Rejected: a run is already in progress")
        return jsonify({"error": "a run is already in progress"}), 409
    log(f"[RUN-URL] Started from browser for brands: {brands or 'from brands table'}")
    threading.Thread(target=_worker, args=(brands, rematch), daemon=True).start()
    return jsonify({"started": True, "brands": brands or "from brands table", "rematch": rematch,
                    "check_status": "/samawa/status"}), 202


@app.get("/samawa/status")
def status():
    """Progress / result of the current or last job: /samawa/run, /competitor/load-fi, /competitor/match-samawa."""
    return jsonify({"running": run_lock.locked(), "last_summary": state["last_summary"]})


def _ct_start(fn, label):
    """Shared start logic for step 1 / 2: check ?secret=, make sure nothing else is running, start in background."""
    if RUN_SECRET and request.args.get("secret") != RUN_SECRET:
        log(f"[{label}] Unauthorized attempt")
        return jsonify({"error": "unauthorized"}), 401
    if not run_lock.acquire(blocking=False):
        return jsonify({"error": "a run is already in progress"}), 409
    log(f"[{label}] Started")
    threading.Thread(target=_ct_worker, args=(fn,), daemon=True).start()
    return jsonify({"started": True, "step": label, "check_status": "/samawa/status"}), 202


@app.get("/competitor/load-fi")
def ct_load_fi_route():
    # Step 1: /competitor/load-fi?secret=XXX -> all French Inventories products into competitor_table
    return _ct_start(ct_load_fi, "CT-LOAD")


@app.get("/competitor/match-samawa")
def ct_match_samawa_route():
    # Step 2: /competitor/match-samawa?secret=XXX -> Samawa link / price / stock / suggestion for every product
    return _ct_start(ct_match_samawa, "CT-MATCH")


@app.get("/competitor/match-ff")
def ct_match_ff_route():
    # Step 3: /competitor/match-ff?secret=XXX -> French Fragrance link / price / stock / suggestion for every product
    return _ct_start(ct_match_ff, "CT-FF")


# ---------------------------------------------------------------- suggestion OK / Not OK buttons
# Where each shop's products are, so an accepted suggestion gets its name / price / stock
SUGGESTION_SHOPS = {
    "samawa": ("Samawa", "SELECT name, price, stock FROM samawa_catalog WHERE product_url = %s LIMIT 1"),
    "ff": ("French Fragrance", "SELECT name, COALESCE(price_inc_tax, price), stock FROM french_fragrance_catalog "
                               "WHERE product_url = %s LIMIT 1"),
    "vp": ("V Perfumes", "SELECT name, COALESCE(price_inc_tax, price), stock FROM vperfumes_catalog "
                         "WHERE product_url = %s LIMIT 1"),
}
LEAST_ORDER = [("Samawa", "samawa"), ("French Fragrance", "ff"), ("Branded Perfume", "bp"),
               ("Essenzi", "es"), ("V Perfumes", "vp")]


def link_sisters_for(cur, fid):
    """After a French Fragrance link changes: refresh Branded Perfume + Essenzi for ONE product."""
    for label, p, table, cp in SISTER_LINKS:
        cur.execute("SELECT to_regclass(%s)", (table,))
        if cur.fetchone()[0] is None:
            continue
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name = %s AND column_name = %s",
                    (table, f"{cp}_title"))
        title_sql = f"COALESCE(s.{cp}_title, f.name)" if cur.fetchone() else "f.name"
        cur.execute(f"""
            UPDATE competitor_table c SET
                {p}_link = s.{cp}_url, {p}_title = {title_sql},
                {p}_price = COALESCE(s.{cp}_price_inc_tax, s.{cp}_price), {p}_stock = (s.{cp}_stock = 'In stock')
            FROM french_fragrance_catalog f JOIN {table} s ON s.ff_id = f.id
            WHERE c.fi_record_id = %s AND c.ff_link = f.product_url AND COALESCE(s.{cp}_stock, '') <> 'Not on site'
        """, (fid,))
        log(f"[SUGGEST] {label} linked for {fid}: {cur.rowcount}")


def least_for(cur, fid):
    """Recalculate least price / least priced website / suggested price for ONE product (same rules as LEAST_SQL)."""
    cols = ", ".join(f"{p}_price, {p}_stock, {p}_link" for _, p in LEAST_ORDER)
    cur.execute(f"SELECT uae_price, {cols} FROM competitor_table WHERE fi_record_id = %s", (fid,))
    row = cur.fetchone()
    if not row:
        return
    offers = [("Fragrant Souq", float(row[0]))] if row[0] and row[0] > 0 else []
    comp = []
    for i, (label, _) in enumerate(LEAST_ORDER):
        price, stock, link = row[1 + i * 3: 4 + i * 3]
        if stock and link and price and price > 0:                 # only in-stock competitors count
            comp.append((label, float(price)))
    offers += comp
    least = min((pr for _, pr in offers), default=None)
    sites = ", ".join(s for s, pr in offers if least is not None and abs(pr - least) < 0.005) or None
    best_comp = min((pr for _, pr in comp), default=None)
    suggested = round(best_comp * (1 - SUGGEST_BELOW), SUGGEST_DECIMALS) if best_comp else None
    cur.execute("UPDATE competitor_table SET least_price = %s, least_priced_website = %s, suggested_price = %s "
                "WHERE fi_record_id = %s", (least, sites, suggested, fid))
    log(f"[SUGGEST] Least for {fid}: {least} ({sites}) | suggested {suggested}")


@app.post("/competitor/suggestion")
def ct_suggestion_route():
    """OK / Not OK buttons next to a suggestion link on /competitors.
    Body (JSON): {"fi_record_id": "recXXX", "shop": "samawa" | "ff" | "vp", "action": "ok" | "notok"}"""
    body = request.get_json(silent=True) or {}
    fid, shop, action = str(body.get("fi_record_id") or ""), body.get("shop"), body.get("action")
    if shop not in SUGGESTION_SHOPS or action not in ("ok", "notok") or not fid.startswith("rec"):
        return jsonify({"ok": False, "error": "wrong request"}), 400
    label, catalog_sql = SUGGESTION_SHOPS[shop]
    conn = pg_conn()
    cur = conn.cursor()
    try:
        cur.execute(CT_CREATE)
        cur.execute(f"SELECT {shop}_suggestion FROM competitor_table WHERE fi_record_id = %s", (fid,))
        row = cur.fetchone()
        url = row[0] if row else None
        if not url:
            return jsonify({"ok": False, "error": "No suggestion for this product any more"}), 404

        if action == "notok":
            # remove the suggestion and remember it, so the next match run does not suggest it again
            cur.execute(f"UPDATE competitor_table SET {shop}_suggestion = NULL, {shop}_rejected = %s, "
                        f"updated_at = NOW() WHERE fi_record_id = %s", (url, fid))
            conn.commit()
            log(f"[SUGGEST] NOT OK: {fid} {label} {url}")
            return jsonify({"ok": True, "action": "notok"})

        # OK -> it becomes the real match, with name / price / stock from that shop's catalog
        cur.execute(catalog_sql, (url,))
        item = cur.fetchone()
        name, price, stock = item if item else (None, None, None)
        cur.execute(f"""UPDATE competitor_table SET
                {shop}_link = %s, {shop}_title = %s, {shop}_price = %s, {shop}_stock = %s,
                {shop}_suggestion = NULL, {shop}_confirmed = TRUE, {shop}_method = 'confirmed', {shop}_score = NULL,
                updated_at = NOW()
            WHERE fi_record_id = %s""", (url, name, price, stock == "In stock", fid))
        if shop == "ff":
            link_sisters_for(cur, fid)            # Branded Perfume / Essenzi follow the French Fragrance link
        least_for(cur, fid)
        conn.commit()
        log(f"[SUGGEST] OK: {fid} {label} -> {name} | AED {price} | {stock}")
        return jsonify({"ok": True, "action": "ok", "name": name, "price": float(price) if price else None,
                        "stock": stock})
    finally:
        cur.close()
        conn.close()


@app.post("/competitor/update-prices")
def ct_update_prices_route():
    """'Update all' bar on /competitors: set UAE Price in Airtable for SEVERAL products at once.
    Body (JSON): {"items": [{"fi_record_id": "recXXX", "price": 79}, ...]}  (max 50 = one page)"""
    items = (request.get_json(silent=True) or {}).get("items") or []
    good, skipped = {}, []
    for it in items[:50]:
        fid = str(it.get("fi_record_id") or "").strip()
        try:
            price = round(float(it.get("price")), 2)
        except (TypeError, ValueError):
            skipped.append(fid); continue
        if fid.startswith("rec") and 0 < price < 100000:
            good[fid] = price
        else:
            skipped.append(fid)
    if not good:
        return jsonify({"ok": False, "error": "No valid prices to update"}), 400
    log(f"[PRICES] Bulk update requested for {len(good)} products ({len(skipped)} skipped)")

    # 1) Airtable: 10 records per request (Airtable limit), wait a little between requests
    fids = list(good)
    failed = {}
    for i in range(0, len(fids), 10):
        chunk = fids[i:i + 10]
        r = requests.patch(f"{AT_URL}/{FI_TABLE}", headers=AT_HEADERS, timeout=30,
                           json={"records": [{"id": f, "fields": {F_UAE_PRICE: good[f]}} for f in chunk],
                                 "typecast": True})
        if r.status_code == 429:                       # too many requests -> wait and try once more
            time.sleep(30)
            r = requests.patch(f"{AT_URL}/{FI_TABLE}", headers=AT_HEADERS, timeout=30,
                               json={"records": [{"id": f, "fields": {F_UAE_PRICE: good[f]}} for f in chunk],
                                     "typecast": True})
        if not r.ok:
            log(f"[PRICES] Airtable error {r.status_code}: {r.text[:300]}")
            for f in chunk:
                failed[f] = f"Airtable {r.status_code}"
        else:
            log(f"[PRICES] Airtable updated {i + len(chunk)}/{len(fids)}")
        time.sleep(0.25)

    # 2) Postgres: our price + least price for every product that Airtable accepted
    done = [f for f in fids if f not in failed]
    conn = pg_conn()
    cur = conn.cursor()
    try:
        for f in done:
            cur.execute("UPDATE competitor_table SET uae_price = %s, updated_at = NOW() WHERE fi_record_id = %s",
                        (good[f], f))
            least_for(cur, f)
        conn.commit()
    finally:
        cur.close()
        conn.close()
    log(f"[PRICES] Done: {len(done)} updated, {len(failed)} failed, {len(skipped)} skipped")
    return jsonify({"ok": not failed, "updated": len(done), "failed": failed, "skipped": skipped,
                    "error": (f"{len(failed)} not updated in Airtable" if failed else None)})


@app.get("/competitor/match-vp")
def ct_match_vp_route():
    # Step 4: /competitor/match-vp?secret=XXX -> V Perfumes link / price / stock / suggestion for every product
    return _ct_start(ct_match_vp, "CT-VP")


@app.post("/competitor/update-price")
def ct_update_price_route():
    """'Update price' button on /competitors: write a new UAE Price to Airtable (French Inventories)
    for ONE product, then update competitor_table + least price so the page shows it immediately.
    Body (JSON): {"fi_record_id": "recXXXX", "price": 79}"""
    body = request.get_json(silent=True) or {}
    fid = str(body.get("fi_record_id") or "").strip()
    try:
        price = round(float(body.get("price")), 2)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Price must be a number"}), 400
    if not fid.startswith("rec") or not (0 < price < 100000):
        return jsonify({"ok": False, "error": "Wrong product or price (must be between 0 and 100,000)"}), 400
    log(f"[PRICE] Update requested: {fid} -> AED {price}")

    # 1) Airtable: French Inventories -> UAE Price (needs data.records:write on the token)
    r = requests.patch(f"{AT_URL}/{FI_TABLE}/{fid}", headers=AT_HEADERS,
                       json={"fields": {F_UAE_PRICE: price}, "typecast": True}, timeout=30)
    if not r.ok:
        log(f"[PRICE] Airtable error {r.status_code}: {r.text[:300]}")
        return jsonify({"ok": False, "error": f"Airtable {r.status_code}: {r.text[:200]}"}), 502
    log(f"[PRICE] Airtable updated: {fid} UAE Price = {price}")

    # 2) Postgres: our price + least price / least priced website for the page
    conn = pg_conn()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE competitor_table SET uae_price = %s, updated_at = NOW() WHERE fi_record_id = %s",
                    (price, fid))
        cur.execute(LEAST_SQL)                       # recalculates least price with our new price
        cur.execute("SELECT least_price, least_priced_website FROM competitor_table WHERE fi_record_id = %s", (fid,))
        row = cur.fetchone()
        conn.commit()
    finally:
        cur.close()
        conn.close()
    least, site = (row or (None, None))
    log(f"[PRICE] Done: {fid} -> AED {price} | least now {least} ({site})")
    return jsonify({"ok": True, "price": price,
                    "least_price": float(least) if least is not None else None, "least_site": site})


@app.get("/competitor/update-least")
def ct_update_least_route():
    # /competitor/update-least?secret=XXX -> take latest Branded Perfume / Essenzi prices + recalculate least price
    return _ct_start(ct_update_least_job, "CT-LEAST")


# Local testing only (python app.py). On Render the app is started by gunicorn.
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
"""
MCP endpoint (Model Context Protocol) - READ ONLY  (2026-11-20)
================================================================
User: "required MCP endpoint for our competitor table system (live)". Choices: READ ONLY for now (changes later if
needed), client = claude.ai / Claude Desktop (custom connector), login = SECRET KEY, straight to live.

Connector address (claude.ai -> Settings -> Connectors -> Add custom connector):
    https://competitor-table.onrender.com/mcp/<MCP_TOKEN>
claude.ai custom connectors cannot send a header, so the secret key is the LAST PART OF THE ADDRESS.
Other apps may instead call /mcp with the header  Authorization: Bearer <MCP_TOKEN>.
Render -> Environment: MCP_TOKEN = a long random text (not set = endpoint OFF). New value = old address stops at once.

Safety: every question runs in a READ-ONLY database session (the database itself refuses any change), results are
capped (MAX_ROWS), the token is compared in constant time and never written to our log (log tag [MCP]).
Transport: MCP "Streamable HTTP", plain JSON answers (no SSE stream): POST JSON-RPC -> initialize, tools/list,
tools/call, ping; notifications -> 202. GET -> 405 (no server-sent events).

Where things are (change them here):
  SECTION 1  settings   -> MCP_TOKEN, MAX_ROWS, PROTOCOLS, SERVER_INFO
  SECTION 2  database   -> ro_cursor() (read-only session), shop list from competitors_page.SOURCES
  SECTION 3  tools      -> search_products, get_product, price_gaps, list_shops, search_shop, list_bundles,
                           run_status  (TOOLS = name -> description, input schema, function)
  SECTION 4  JSON-RPC   -> POST /mcp and /mcp/<token>
  SECTION 5  hooks      -> init(connect)
"""

import hmac
import json
import os
import re
from datetime import date, datetime
from decimal import Decimal

import psycopg2.extras
from flask import Blueprint, request, jsonify, Response

mcp_bp = Blueprint("mcp_server", __name__)


def log(*a):
    """Print to the Render log (lines start with [MCP]). The token is never printed."""
    print(*a, flush=True)


# ============================================================ SECTION 1: settings
MCP_TOKEN = os.environ.get("MCP_TOKEN", "")
MAX_ROWS = 50                                    # most rows one tool answer gives back
PROTOCOLS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]   # MCP versions we answer (first = newest)
SERVER_INFO = {"name": "fragrant-souq-competitor-table", "title": "Fragrant Souq competitor table", "version": "1.0"}
INSTRUCTIONS = ("Read-only access to Fragrant Souq's competitor price tracker (perfumes, UAE, prices in AED). "
                "Our products: search_products / get_product (our UAE price, least competitor price, suggested "
                "price, LOP/MOP, stock, competitor prices per shop). price_gaps = where we are more expensive or "
                "cheapest. list_shops / search_shop = competitor and brand-store product lists. list_bundles, "
                "run_status. Nothing can be changed through this connector.")
_hooks = {}


# ============================================================ SECTION 2: database (read only)
def ro_cursor():
    """(connection, dict cursor) in a READ-ONLY session: Postgres itself refuses any INSERT / UPDATE / DELETE."""
    conn = _hooks["connect"]()
    conn.set_session(readonly=True, autocommit=False)
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _shops():
    """[(key, label, table, cols, menu)] from competitors_page.SOURCES (the shop tabs of the website)."""
    import competitors_page
    return [(s["key"], s["label"], s["table"], s.get("cols") or {}, s.get("menu") or "competitors")
            for s in competitors_page.SOURCES if s.get("key") != "competitor"]


def _card_shops():
    """[(label, prefix)] of the shops shown on the Competitor table cards (linked to our products)."""
    import competitors_page
    return list(competitors_page.SOURCES[0]["shops"])


def _clean(v):
    """Database value -> JSON value."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


def _limit(args, default=20):
    try:
        return max(1, min(MAX_ROWS, int(args.get("limit") or default)))
    except (TypeError, ValueError):
        return default


def _words(q):
    return [w for w in re.split(r"\s+", str(q or "").strip()) if w][:6]


# ============================================================ SECTION 3: tools
OUR_COLS = """ct.fi_record_id, ct.sku, ct.product_name, ct.brand, ct.barcode, ct.uae_price AS our_uae_price,
              ct.least_price, ct.least_priced_website, ct.suggested_price, ct.lop, ct.mop,
              ct.our_stock_qty AS our_stock_qty, ct.no_auto AS skip_automatic_price, ct.our_link"""


def _with_auto(cur, rows):
    """Adds brand_on_auto (Item Master -> Brands 'Auto' tick) to every row; table missing -> False."""
    cur.execute("SELECT to_regclass('brand_automation') IS NOT NULL AS ok")
    auto = set()
    if cur.fetchone()["ok"]:
        cur.execute("SELECT LOWER(brand) AS b FROM brand_automation WHERE auto")
        auto = {r["b"] for r in cur.fetchall()}
    for r in rows:
        r["brand_on_auto"] = (r.get("brand") or "").lower() in auto
    return rows


def t_search_products(a):
    words, brand = _words(a.get("query")), (a.get("brand") or "").strip()
    where, params = [], []
    for w in words:                                   # every word somewhere in SKU / name / brand / barcode
        where.append("(COALESCE(ct.sku,'') || ' ' || COALESCE(ct.product_name,'') || ' ' || COALESCE(ct.brand,'') "
                     "|| ' ' || COALESCE(ct.barcode,'')) ILIKE %s")
        params.append(f"%{w}%")
    if brand:
        where.append("LOWER(TRIM(ct.brand)) = LOWER(TRIM(%s))")
        params.append(brand)
    if not where:
        return {"error": "give a query (words of the name / SKU / barcode) or a brand"}
    conn, cur = ro_cursor()
    try:
        cur.execute(f"SELECT {OUR_COLS} FROM competitor_table ct WHERE {' AND '.join(where)} "
                    f"ORDER BY ct.brand, ct.product_name LIMIT %s", params + [_limit(a)])
        rows = _with_auto(cur, cur.fetchall())
    finally:
        conn.close()
    return {"count": len(rows), "products": _clean(rows)}


def t_get_product(a):
    key = (a.get("sku") or a.get("barcode") or a.get("fi_record_id") or "").strip()
    if not key:
        return {"error": "give sku, barcode or fi_record_id"}
    conn, cur = ro_cursor()
    try:
        cur.execute(f"SELECT {OUR_COLS}, ct.* FROM competitor_table ct WHERE UPPER(ct.sku) = UPPER(%s) "
                    f"OR ct.barcode = %s OR ct.fi_record_id = %s LIMIT 1", (key, key, key))
        r = cur.fetchone()
        if r:
            _with_auto(cur, [r])
    finally:
        conn.close()
    if not r:
        return {"error": f"no product with SKU / barcode / id '{key}'"}
    comps = []
    for label, p in _card_shops():                    # only shops linked (or suggested) to this product
        link, sug = r.get(f"{p}_link"), r.get(f"{p}_suggestion")
        if not (link or sug):
            continue
        comps.append({"shop": label, "linked": bool(link), "link": link or None, "suggestion": None if link else sug,
                      "title": r.get(f"{p}_title"), "price": r.get(f"{p}_price"),
                      "in_stock": r.get(f"{p}_stock"), "confirmed_by_admin": r.get(f"{p}_confirmed")})
    keep = [c.strip().split(" AS ")[-1].split(".")[-1] for c in OUR_COLS.replace("\n", " ").split(",")]
    out = {k: r.get(k) for k in keep + ["brand_on_auto"] if k in r}
    out["competitors"] = comps
    return _clean(out)


def t_price_gaps(a):
    mode = a.get("mode") or "more_expensive"
    brand = (a.get("brand") or "").strip()
    where = ["ct.uae_price IS NOT NULL", "ct.least_price IS NOT NULL"]
    params = []
    if mode == "cheapest":
        where.append("ct.uae_price <= ct.least_price")
        order = "ct.least_price - ct.uae_price DESC"
    else:                                             # we are MORE expensive than the cheapest in-stock competitor
        where.append("ct.uae_price > ct.least_price")
        order = "ct.uae_price - ct.least_price DESC"
        try:
            gap = float(a.get("min_gap_aed") or 0)
        except (TypeError, ValueError):
            gap = 0
        if gap > 0:
            where.append("ct.uae_price - ct.least_price >= %s")
            params.append(gap)
    if brand:
        where.append("LOWER(TRIM(ct.brand)) = LOWER(TRIM(%s))")
        params.append(brand)
    conn, cur = ro_cursor()
    try:
        cur.execute(f"SELECT {OUR_COLS}, ct.uae_price - ct.least_price AS gap_aed FROM competitor_table ct "
                    f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT %s", params + [_limit(a)])
        rows = _with_auto(cur, cur.fetchall())
        cur.execute(f"SELECT COUNT(*) AS n FROM competitor_table ct WHERE {' AND '.join(where)}", params)
        total = cur.fetchone()["n"]
    finally:
        conn.close()
    return {"mode": mode, "total_matching": total, "shown": len(rows), "products": _clean(rows)}


def t_list_shops(a):
    conn, cur = ro_cursor()
    out = []
    try:
        for key, label, table, cols, menu in _shops():
            cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (table,))
            if not cur.fetchone()["ok"]:
                continue
            upd = cols.get("updated")
            cur.execute(f"SELECT COUNT(*) AS n{', MAX(' + upd + ') AS last' if upd else ''} FROM {table}")
            row = cur.fetchone()
            out.append({"key": key, "shop": label, "menu": "Brands Store" if menu == "brands" else "Competitors",
                        "products": row["n"], "last_updated": row.get("last")})
    finally:
        conn.close()
    return {"shops": _clean(out)}


def t_search_shop(a):
    want = (a.get("shop") or "").strip().lower()
    shop = next((s for s in _shops() if want in (s[0].lower(), s[1].lower())), None)
    if not shop:
        return {"error": f"unknown shop '{a.get('shop')}' - use list_shops for the keys / names"}
    key, label, table, cols, _ = shop
    fields = [(n, cols.get(n)) for n in ("name", "brand", "price", "stock", "volume", "gtin", "url", "updated")]
    sel = ", ".join(f"{sql} AS {n}" for n, sql in fields if sql)
    words = _words(a.get("query"))
    text = " || ' ' || ".join(f"COALESCE(({sql})::text,'')" for n, sql in fields if sql and n in ("name", "brand", "gtin"))
    where = " AND ".join(f"({text}) ILIKE %s" for _ in words) or "TRUE"
    conn, cur = ro_cursor()
    try:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (table,))
        if not cur.fetchone()["ok"]:
            return {"error": f"{label}: its product list is not downloaded yet"}
        cur.execute(f"SELECT {sel} FROM {table} WHERE {where} LIMIT %s", [f"%{w}%" for w in words] + [_limit(a)])
        rows = cur.fetchall()
    finally:
        conn.close()
    return {"shop": label, "count": len(rows), "products": _clean(rows)}


def t_list_bundles(a):
    words = _words(a.get("query"))
    where = " AND ".join("(title || ' ' || sku) ILIKE %s" for _ in words) or "TRUE"
    conn, cur = ro_cursor()
    try:
        cur.execute("SELECT to_regclass('bundles') IS NOT NULL AS ok")
        if not cur.fetchone()["ok"]:
            return {"count": 0, "bundles": []}
        cur.execute(f"SELECT id, title, sku, items, total_price, discount_type, discount_value, discount_aed, "
                    f"bundle_price, active_until, created_by, created_at FROM bundles WHERE {where} "
                    f"ORDER BY id DESC LIMIT %s", [f"%{w}%" for w in words] + [_limit(a)])
        rows = cur.fetchall()
    finally:
        conn.close()
    for b in rows:                                   # keep items short: qty x sku name @ price
        b["items"] = [{k: it.get(k) for k in ("qty", "sku", "name", "price")} for it in (b.get("items") or [])]
    return {"count": len(rows), "bundles": _clean(rows)}


def t_run_status(a):
    conn, cur = ro_cursor()
    try:
        cur.execute("SELECT key, value, updated_at FROM app_settings WHERE key IN ('daily_update', 'full_lists')")
        rows = cur.fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        try:
            v = json.loads(r["value"] or "{}")
        except ValueError:
            v = r["value"]
        out["run1_pc" if r["key"] == "daily_update" else "run2_render"] = {"saved_at": r["updated_at"], "state": v}
    text = json.dumps(_clean(out), default=str)
    return json.loads(text[:20000]) if len(text) <= 20000 else {"note": "cut to 20000 characters", "raw": text[:20000]}


def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_ROWS, "description": f"max rows (1-{MAX_ROWS}, default 20)"}
TOOLS = {
    "search_products": ("Search OUR products (Competitor table). Every word must appear in SKU / name / brand / "
                        "barcode. Gives our UAE price, least competitor price + shop, suggested price, LOP/MOP, stock.",
                        _schema({"query": {"type": "string", "description": "words, e.g. 'armaf club 105'"},
                                 "brand": {"type": "string", "description": "exact brand name (optional)"},
                                 "limit": LIMIT}), t_search_products),
    "get_product": ("One of OUR products with every competitor linked to it (shop, price, stock, link).",
                    _schema({"sku": {"type": "string"}, "barcode": {"type": "string"},
                             "fi_record_id": {"type": "string"}}), t_get_product),
    "price_gaps": ("Our products where we are MORE EXPENSIVE than the cheapest in-stock competitor (mode "
                   "more_expensive, biggest gap first) or where we are the CHEAPEST (mode cheapest).",
                   _schema({"mode": {"type": "string", "enum": ["more_expensive", "cheapest"]},
                            "brand": {"type": "string"},
                            "min_gap_aed": {"type": "number", "description": "only gaps of at least this many AED"},
                            "limit": LIMIT}), t_price_gaps),
    "list_shops": ("All competitor shops and brand stores we track, with product counts and last update.",
                   _schema({}), t_list_shops),
    "search_shop": ("Search ONE competitor shop's / brand store's own product list (name, brand, price, stock, "
                    "size, barcode, link). shop = key or name from list_shops.",
                    _schema({"shop": {"type": "string"}, "query": {"type": "string"}, "limit": LIMIT}, ["shop"]),
                    t_search_shop),
    "list_bundles": ("Our bundles (Bundle menu): products, total, discount, bundle price, active until.",
                     _schema({"query": {"type": "string"}, "limit": LIMIT}), t_list_bundles),
    "run_status": ("Status of the automatic downloads: Run 1 (PC) and Run 2 (Render, per shop).",
                   _schema({}), t_run_status),
}


# ============================================================ SECTION 4: JSON-RPC over HTTP (Streamable HTTP, JSON only)
def _authorized(path_token):
    """Token from the address (/mcp/<token>) or the Authorization: Bearer header. Endpoint OFF when not set."""
    if not MCP_TOKEN:
        return False
    given = path_token or ""
    h = request.headers.get("Authorization") or ""
    if not given and h.lower().startswith("bearer "):
        given = h[7:].strip()
    return bool(given) and hmac.compare_digest(given.encode(), MCP_TOKEN.encode())


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, msg):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}}


def _handle(msg):
    """One JSON-RPC message -> answer dict, or None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if "id" not in msg:                               # notification (e.g. notifications/initialized)
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        log(f"[MCP] initialize from {(params.get('clientInfo') or {}).get('name', '?')} (protocol {asked})")
        return _result(mid, {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                             "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS})
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": [{"name": n, "description": d, "inputSchema": s,
                                        "annotations": {"readOnlyHint": True, "openWorldHint": False}}
                                       for n, (d, s, _) in TOOLS.items()]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in TOOLS:
            return _error(mid, -32602, f"Unknown tool: {name}")
        try:
            out = TOOLS[name][2](args)
            err = isinstance(out, dict) and "error" in out and len(out) == 1
            log(f"[MCP] {name} {json.dumps(args)[:150]} -> {'ERROR ' + out['error'] if err else 'ok'}")
        except Exception as ex:                       # tool failure = MCP tool error (not a protocol error)
            log(f"[MCP] {name} FAILED: {ex}")
            out, err = {"error": f"{type(ex).__name__}: {str(ex).splitlines()[0][:200]}"}, True
        return _result(mid, {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, default=str)}],
                             "isError": bool(err)})
    return _error(mid, -32601, f"Method not found: {method}")


@mcp_bp.route("/mcp", methods=["GET", "POST", "DELETE"])
@mcp_bp.route("/mcp/<token>", methods=["GET", "POST", "DELETE"])
def mcp_endpoint(token=None):
    if not _authorized(token):
        log(f"[MCP] refused {request.method}: {'MCP_TOKEN not set in Render' if not MCP_TOKEN else 'wrong / no key'}")
        return jsonify({"error": "unauthorized"}), 401
    if request.method != "POST":                      # no server-sent event stream / sessions
        return Response(status=405, headers={"Allow": "POST"})
    body = request.get_json(silent=True)
    if body is None:
        return jsonify(_error(None, -32700, "Parse error")), 400
    if isinstance(body, list):                        # batch
        answers = [x for x in (_handle(m) for m in body) if x is not None]
        return (jsonify(answers), 200) if answers else Response(status=202)
    ans = _handle(body)
    return (jsonify(ans), 200) if ans is not None else Response(status=202)


# ============================================================ SECTION 5: hooks
def init(connect):
    """connect() -> new Postgres connection (app.pg_conn)."""
    _hooks["connect"] = connect
    log(f"[MCP] endpoint /mcp/<key> {'ON (read only)' if MCP_TOKEN else 'OFF - set MCP_TOKEN in Render -> Environment'}"
        f", {len(TOOLS)} tools")

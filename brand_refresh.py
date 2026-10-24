"""
Refresh buttons on Item Master -> Brands  (ADMIN ONLY)
======================================================
"Refresh all" (top) or the round arrow on one brand's row updates that brand's products (or all products):
  1. our product details from Airtable -> competitor_table (load-fi: name, SKU, barcode, UAE price)
  2. our product details from Airtable -> item_master (all fields; website edits / descriptions are kept)
  3. our fragrantsouq.com link + stock (load-our-links)
  4. match to EVERY competitor / brand shop (link, price, stock, suggestion) - from the shop lists already in the
     database (Run 1 / Run 2 download the shops; a refresh does NOT download the shops' websites again)
  5. least price + suggested price (+ automatic brands -> Airtable), once at the end
One brand: only its products are changed, nothing else is touched (no products deleted).
One job at a time: it waits for nothing - when another job runs (a match, Run 2 ...) it says so and does not start.

Where things are (change them here):
  SECTION 1  state           -> rf_state (progress shown on the page)
  SECTION 2  the job         -> run_refresh(): the 5 steps above
  SECTION 3  page parts      -> RF_CSS, RF_TOP (Refresh all + progress line), RF_ROW (arrow per brand), RF_JS
  SECTION 4  routes          -> POST /brands/refresh, GET /brands/refresh/status
  SECTION 5  hooks           -> init(): the jobs from app.py / item_master.py
"""

import time
import threading
import traceback
from datetime import datetime

from flask import Blueprint, request, jsonify, g

from auth import is_admin

refresh_bp = Blueprint("brand_refresh", __name__)


def log(*a):
    """Print to the Render log (lines start with [REFRESH])."""
    print(*a, flush=True)


# ============================================================ SECTION 1: state (what the page shows)
rf_state = {"running": False, "brand": None, "step": None, "done": 0, "total": 0,
            "started": None, "finished": None, "by": None, "error": None, "results": {}}
_hooks = {}


# ============================================================ SECTION 2: the job
def _step(name, fn):
    """Run one step, remember its result (or error) - one failing step never stops the others."""
    rf_state["step"] = name
    log(f"[REFRESH] {rf_state['brand'] or 'ALL brands'}: {name} ...")
    t = time.time()
    try:
        res = fn()
        short = {k: v for k, v in (res or {}).items() if not isinstance(v, (dict, list))} if isinstance(res, dict) else res
        rf_state["results"][name] = {"ok": not (isinstance(res, dict) and res.get("error")), "seconds": round(time.time() - t),
                                     "result": short}
    except Exception as ex:
        log(f"[REFRESH] {name}: ERROR {ex}\n{traceback.format_exc()}")
        rf_state["results"][name] = {"ok": False, "error": str(ex)[:200]}
    rf_state["done"] += 1


def run_refresh(brand):
    """brand = one brand name, or None = ALL brands. Runs in a background thread while holding the app's run_lock."""
    h, only = _hooks, ([brand] if brand else None)
    steps = [("Our products from Airtable (competitor table)", lambda: h["load_fi"](only_brands=only, least=False)),
             ("Our products from Airtable (Item Master)", lambda: h["load_item_master"]("refresh " + (brand or "all"),
                                                                                        only_brands=only)),
             ("Our fragrantsouq.com links + stock", lambda: h["load_our_links"](only_brands=only))]
    steps += [(f"Match {label}", (lambda k=k: h["match_shop"](k, only_brands=only, least=False)))
              for k, label in h["shops"]]
    steps.append(("Least price + suggested price", h["update_least"]))
    rf_state.update(total=len(steps), done=0, results={})
    try:
        for name, fn in steps:
            _step(name, fn)
    except Exception as ex:
        rf_state["error"] = str(ex)[:200]
    finally:
        rf_state.update(running=False, step=None, finished=datetime.utcnow().isoformat(timespec="seconds") + "Z")
        h["run_lock"].release()
        bad = [n for n, r in rf_state["results"].items() if not r.get("ok")]
        log(f"[REFRESH DONE] {brand or 'ALL brands'}: {rf_state['done']}/{rf_state['total']} steps"
            + (f" | problems: {bad}" if bad else " | all OK"))


# ============================================================ SECTION 3: page parts (competitors_page.py SETTINGS_HTML 8d)
RF_CSS = """ /* Refresh buttons (brand_refresh.py) */
 .rf-all{background:transparent;color:#f2d675;border:1px solid #f2d675;border-radius:10px;padding:9px 16px;font-size:15px;
         font-weight:700;cursor:pointer;margin-right:8px}
 .rf-one{background:none;border:1px solid var(--line);color:var(--muted);border-radius:8px;width:30px;height:30px;
         font-size:16px;line-height:1;cursor:pointer;flex:0 0 auto;padding:0}
 .rf-one:hover{color:#f2d675;border-color:#f2d675}
 .rf-all:disabled,.rf-one:disabled{opacity:.4;cursor:wait}
 .rfmsg{font-size:14px;color:var(--muted);margin:4px 2px 10px} .rfmsg b{color:var(--text)}
 .rfmsg.ok{color:#8cc58c} .rfmsg.err{color:#e0806c}
"""

# goes in the bar with "Create new" (admins)
RF_TOP = """<button type="button" class="rf-all" onclick="rfStart(null, this)"
          title="Update ALL products of ALL brands: Airtable, Item Master, our links, every competitor + brand shop, prices">&#x21bb; Refresh all</button>"""
RF_MSG = """<div class="rfmsg" id="rfmsg" hidden></div>"""

# one per brand row (b = brand row), before the product count
RF_ROW = """{% if admin %}<button type="button" class="rf-one" data-brand="{{ b.brand }}" onclick="rfStart(this.dataset.brand, this)"
          title="Refresh {{ b.brand }}: its products in Item Master + competitor table (links, prices, stock)">&#x21bb;</button>{% endif %}"""

RF_JS = """<script>
  // ---- Refresh all / one brand (brand_refresh.py) ----
  var rfTimer = null;
  function rfButtons(off) { document.querySelectorAll(".rf-all,.rf-one").forEach(function (b) { b.disabled = off; }); }
  function rfShow(st) {
    var m = document.getElementById("rfmsg"); if (!m) return;
    var who = st.brand ? st.brand : "all brands";
    if (st.running) {
      m.hidden = false; m.className = "rfmsg";
      m.innerHTML = "Refreshing <b></b>: step " + (st.done + 1) + " of " + st.total + " - " + (st.step || "") + " ...";
      m.querySelector("b").textContent = who; rfButtons(true);
    } else if (st.finished) {
      var bad = Object.keys(st.results || {}).filter(function (k) { return !st.results[k].ok; });
      m.hidden = false; m.className = "rfmsg " + (bad.length ? "err" : "ok");
      m.textContent = "Refresh of " + who + " finished " + (bad.length ? "with problems in: " + bad.join(", ") : "- all steps OK") +
                      " (" + st.finished.replace("T", " ").replace("Z", " UTC") + ")";
      rfButtons(false);
    }
  }
  function rfPoll() {
    fetch("/brands/refresh/status").then(function (r) { return r.json(); }).then(function (st) {
      rfShow(st);
      clearTimeout(rfTimer);
      if (st.running) rfTimer = setTimeout(rfPoll, 4000);
    }).catch(function () { rfTimer = setTimeout(rfPoll, 8000); });
  }
  async function rfStart(brand, btn) {
    var what = brand ? "the products of " + brand : "ALL products of ALL brands (takes 15-30 minutes)";
    if (!confirm("Refresh " + what + "?\\n\\nOur details from Airtable, Item Master, our fragrantsouq.com links and every " +
                 "competitor / brand shop match (links, prices, stock), then least + suggested prices.")) return;
    console.log("[REFRESH] start " + (brand || "ALL brands"));
    rfButtons(true);
    try {
      var r = await fetch("/brands/refresh", {method: "POST", headers: {"Content-Type": "application/json"},
                                               body: JSON.stringify({brand: brand})});
      var d = await r.json().catch(function () { return {ok: false, error: "HTTP " + r.status}; });
      if (!d.ok) throw new Error(d.error || ("HTTP " + r.status));
      rfPoll();
    } catch (e) {
      var m = document.getElementById("rfmsg"); m.hidden = false; m.className = "rfmsg err";
      m.textContent = "Refresh not started: " + e.message; rfButtons(false);
      console.log("[REFRESH] not started: " + e.message);
    }
  }
  rfPoll();                                          // a refresh already running -> show its progress
</script>"""


# ============================================================ SECTION 4: routes
@refresh_bp.post("/brands/refresh")
def refresh_start():
    """Body {"brand": "Afnan"} = one brand, {"brand": null} = ALL brands. Admins only."""
    if not is_admin():
        log(f"[REFRESH] Refused: {g.get('user')} is not an admin")
        return jsonify({"ok": False, "error": "admins only"}), 403
    brand = ((request.get_json(silent=True) or {}).get("brand") or "").strip() or None
    if not _hooks["run_lock"].acquire(blocking=False):
        log(f"[REFRESH] {brand or 'ALL'} not started: another job is running")
        return jsonify({"ok": False, "error": "another job is running (a match / download / refresh) - try again in a few minutes"}), 409
    rf_state.update(running=True, brand=brand, step="starting", done=0, total=0, error=None, results={},
                    by=g.get("user"), started=datetime.utcnow().isoformat(timespec="seconds") + "Z", finished=None)
    log(f"[REFRESH] {g.get('user')} started a refresh of {brand or 'ALL brands'}")
    threading.Thread(target=run_refresh, args=(brand,), daemon=True).start()
    return jsonify({"ok": True, "brand": brand})


@refresh_bp.get("/brands/refresh/status")
def refresh_status():
    return jsonify(rf_state)


# ============================================================ SECTION 5: hooks (called once from app.py)
def init(run_lock, load_fi, load_item_master, load_our_links, match_shop, shops, update_least):
    """shops = [(key, label), ...] = every competitor + brand shop (app.SHOPS)."""
    _hooks.update(run_lock=run_lock, load_fi=load_fi, load_item_master=load_item_master,
                  load_our_links=load_our_links, match_shop=match_shop, shops=shops, update_least=update_least)
    log(f"[REFRESH] Linked to app.py: Refresh = Airtable + Item Master + our links + {len(shops)} shops + prices")

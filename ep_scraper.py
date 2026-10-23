r"""
Emirates Pride - PC part (Run 1, PC_LISTS in daily_update.py)
==============================================================
The website answers by LOCATION:
  - Render (USA) gets the Shopify product LIST (products.json) but NOT the UAE pages (404)
  - this PC (UAE) gets the UAE pages (AED prices) but NOT the product list (404)
So this script does both halves:
  STEP 1  asks Render to read the perfume LIST (/emiratespride/run) and waits until it is done
          -> emiratespride_catalog has every perfume (name, size, barcode, UAE link) WITHOUT price
  STEP 2  opens every perfume's UAE page from this PC (emiratespride.ep_fill_prices)
          -> AED price + stock filled in; not sold in the UAE -> 'Not on site'
Then daily_update.py starts the matching on Render (/competitor/match-ep).

Run it by hand (in this folder):   python ep_scraper.py
then open:                         https://competitor-table.onrender.com/competitor/match-ep?secret=XXX
FF_DATABASE_URL + RUN_SECRET: from the environment (daily_update.bat sets them), else read from daily_update.bat.

Where things are:
  SECTION 1  settings + daily_update.bat values  -> SITE, from_bat()
  SECTION 2  STEP 1 list on Render                -> render_list()
  SECTION 3  run                                  -> main(): exit 0 = OK, 1 = error, 2 = nothing priced
"""

import os
import re
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


# ============================================================ SECTION 1: settings + daily_update.bat values
SITE = os.environ.get("CT_SITE", "https://competitor-table.onrender.com")   # Render app (same as daily_update.py)
LIST_WAIT_MINUTES = 10                         # STEP 1 normally takes ~1 minute


def from_bat(name):
    """Value of an environment variable, else `set "NAME=..."` from daily_update.bat (same folder)."""
    val = os.environ.get(name)
    if val:
        return val
    try:
        with open(os.path.join(HERE, "daily_update.bat"), encoding="utf-8", errors="replace") as f:
            m = re.search(rf'set "{name}=([^"]+)"', f.read())
    except OSError:
        m = None
    if not m:
        sys.exit(f"[EP-PC] {name} not set and not found in daily_update.bat")
    return m.group(1)


# ============================================================ SECTION 2: STEP 1 list on Render
def render_list(secret):
    """Start /emiratespride/run on Render and wait for it. Returns its result dict (or raises RuntimeError)."""
    print("[EP-PC] STEP 1: Render reads the perfume list (/emiratespride/run) ...", flush=True)
    r = requests.get(f"{SITE}/emiratespride/run", params={"secret": secret}, timeout=180)  # Render may wake up first
    if r.status_code not in (202, 409):                # 409 = already running -> just wait for it
        raise RuntimeError(f"Render answered HTTP {r.status_code}: {r.text[:200]}")
    deadline = time.time() + LIST_WAIT_MINUTES * 60
    while time.time() < deadline:
        time.sleep(10)
        st = requests.get(f"{SITE}/emiratespride/status", params={"secret": secret}, timeout=60).json()
        if not st.get("running"):
            last = st.get("last") or {}
            print(f"[EP-PC] STEP 1 result: {last}", flush=True)
            if last.get("status") != "done":
                raise RuntimeError(f"list on Render failed: {last.get('error') or last}")
            return last
    raise RuntimeError(f"list on Render not finished after {LIST_WAIT_MINUTES} minutes")


# ============================================================ SECTION 3: run
def main():
    os.environ["FF_DATABASE_URL"] = from_bat("FF_DATABASE_URL")
    secret = from_bat("RUN_SECRET")
    import emiratespride as ep                         # same code as on Render (works without Flask)
    try:
        listed = render_list(secret)
    except (RuntimeError, requests.RequestException, ValueError) as ex:
        print(f"[EP-PC] STEP 1 ERROR: {ex}", flush=True)
        sys.exit(1)
    if not listed.get("saved"):
        print("[EP-PC] STEP 1 saved no perfumes -> stopping", flush=True)
        sys.exit(2)
    print(f"[EP-PC] STEP 2: AED prices from the UAE pages ({listed.get('saved')} perfume sizes listed) ...", flush=True)
    conn = ep.ep_conn()
    try:
        res = ep.ep_fill_prices(conn)
    finally:
        conn.close()
    print(f"[EP-PC] Result: {res}", flush=True)
    if not res.get("priced"):
        print("[EP-PC] No AED price read from any UAE page -> counted as failed", flush=True)
        sys.exit(2)
    print(f"[EP-PC] Done: {res['priced']} perfumes with AED price, {res['not_in_uae']} not sold in the UAE", flush=True)


if __name__ == "__main__":
    main()

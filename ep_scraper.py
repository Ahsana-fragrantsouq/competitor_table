r"""
Emirates Pride - PC scraper (Run 1, PC_LISTS in daily_update.py)
=================================================================
Why on the PC: emiratespride.com/ae/product/... answers "404" to Render's servers (USA) but works from the PC.
What it does: the SAME download as emiratespride.py (perfumes only, AED price + stock from each UAE page) and saves
into Postgres table emiratespride_catalog. daily_update.py then starts the matching on Render (/competitor/match-ep).

Run it by hand (in this folder):   python ep_scraper.py
then open:                         https://competitor-table.onrender.com/competitor/match-ep?secret=XXX
Database: FF_DATABASE_URL from the environment, else from daily_update.bat (same folder, like sql.py).

Where things are:
  SECTION 1  database link   -> database_url()
  SECTION 2  run             -> main(): emiratespride.ep_run(), exit code 0 = OK, 1 = error, 2 = nothing saved
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


# ============================================================ SECTION 1: database link
def database_url():
    """FF_DATABASE_URL from the environment (daily_update.bat sets it), else read from daily_update.bat."""
    url = os.environ.get("FF_DATABASE_URL")
    if url:
        return url
    try:
        with open(os.path.join(HERE, "daily_update.bat"), encoding="utf-8", errors="replace") as f:
            m = re.search(r'set "FF_DATABASE_URL=([^"]+)"', f.read())
    except OSError:
        m = None
    if not m:
        sys.exit("[EP-PC] FF_DATABASE_URL not set and not found in daily_update.bat")
    return m.group(1)


# ============================================================ SECTION 2: run
def main():
    os.environ["FF_DATABASE_URL"] = database_url()
    import emiratespride as ep                       # same code as on Render (works without Flask)
    print("[EP-PC] Emirates Pride: reading the shop from this PC ...", flush=True)
    ep.ep_run()
    st = ep.ep_state.get("last") or {}
    print(f"[EP-PC] Result: {st}", flush=True)
    if st.get("status") != "done":
        sys.exit(1)
    if not st.get("saved"):
        print("[EP-PC] Nothing saved (UAE pages not readable?) -> counted as failed", flush=True)
        sys.exit(2)
    print(f"[EP-PC] Done: {st.get('saved')} perfume sizes saved", flush=True)


if __name__ == "__main__":
    main()

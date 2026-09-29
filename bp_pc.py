"""
Run the Branded Perfume steps on YOUR PC (brandedperfume.com blocks Render servers, like French Fragrance).
Uses the same code as Render (branded_perfume.py) - only the internet connection is different.

Put this file in the same folder as branded_perfume.py, then:
  pip install requests psycopg2-binary flask
  set "FF_DATABASE_URL=postgresql://...render.com/french_fragrance_db"      (EXTERNAL url)

  python bp_pc.py test      -> open 3 products and show what was read (do this first)
  python bp_pc.py load      -> STEP 1: copy all French Fragrance products into branded_perfume_catalog
  python bp_pc.py run       -> STEP 2: open every Branded Perfume page -> price, inc-tax price, stock
  python bp_pc.py resume    -> STEP 2 again, only products not checked yet (after you stopped it / PC slept)

Results appear on https://competitor-table.onrender.com/competitors?tab=bp
"""

import sys
import requests
import branded_perfume as bp


def test():
    """Open 3 products with full details, so we can see if this PC is allowed and if price/stock are read."""
    conn = bp.bp_conn()
    cur = conn.cursor()
    cur.execute("SELECT product_url FROM french_fragrance_catalog WHERE product_url IS NOT NULL ORDER BY id LIMIT 3")
    urls = [u.replace(bp.FF_DOMAIN, bp.BP_DOMAIN) for (u,) in cur.fetchall()]
    cur.close()
    conn.close()

    session = requests.Session()
    for u in urls:
        r = session.get(u, headers=bp.BP_HEADERS, timeout=40)
        print(f"\n[TEST] {u}")
        print(f"[TEST] HTTP {r.status_code} | final url {r.url} | {len(r.text)} characters")
        data, status = bp.bp_fetch(session, u)
        print(f"[TEST] status={status} | read={data}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "test":
        test()
    elif cmd == "load":
        bp.bp_load()
    elif cmd == "run":
        bp.bp_run(resume=False)
    elif cmd == "resume":
        bp.bp_run(resume=True)
    else:
        print(__doc__)
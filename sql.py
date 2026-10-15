r"""
Run ONE SQL query on french_fragrance_db from your PC (no pgAdmin / psql needed).
Put this file next to daily_update.bat (it reads FF_DATABASE_URL from that file).

  python sql.py "SELECT sku, vp_price, least_price, suggested_price FROM competitor_table WHERE sku = 'AMF1085'"
  python sql.py "UPDATE competitor_table SET vp_price = 80, updated_at = NOW() WHERE sku = 'AMF1085'"

SELECT -> prints the rows.  UPDATE / INSERT / DELETE -> saved (COMMIT) and prints how many rows changed.
"""
import os
import re
import sys

import psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))


def database_url():
    """FF_DATABASE_URL from daily_update.bat (same folder)."""
    with open(os.path.join(HERE, "daily_update.bat"), encoding="utf-8", errors="replace") as f:
        m = re.search(r'set "FF_DATABASE_URL=([^"]+)"', f.read())
    if not m:
        sys.exit("[SQL] FF_DATABASE_URL not found in daily_update.bat")
    return m.group(1)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    query = sys.argv[1]
    url = database_url()
    print(f"[SQL] Database: {url.split('@')[-1]}")          # host + database only (never the password)
    print(f"[SQL] Query: {query}")
    conn = psycopg2.connect(url, sslmode="require")
    cur = conn.cursor()
    try:
        cur.execute(query)
        if cur.description:                                  # SELECT -> show the rows
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            print("[SQL] " + " | ".join(cols))
            for r in rows:
                print("      " + " | ".join("" if v is None else str(v) for v in r))
            print(f"[SQL] {len(rows)} row(s)")
        else:                                                # UPDATE / INSERT / DELETE -> save
            print(f"[SQL] {cur.rowcount} row(s) changed")
        conn.commit()
        print("[SQL] Done (saved)")
    except Exception as ex:
        conn.rollback()
        print(f"[SQL] ERROR - nothing changed: {ex}")
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
r"""
Copy the PRODUCTION database into a TEST database on the SAME Render server - from your PC, NO PostgreSQL tools needed
=====================================================================================================================
(2026-11-12, user: test website = copy of the Competitor table; "create french_fragrance_test in same database" without
installing the PostgreSQL tools.) Put this file next to daily_update.bat (it reads FF_DATABASE_URL from there, like sql.py).

  python copy_to_test.py            -> 1st time: creates french_fragrance_test + copies every table (structure + rows)
  python copy_to_test.py --fresh    -> later: EMPTIES the test database and copies production again (fresh test data)

PRODUCTION IS ONLY READ (SELECT / COPY ... TO). Everything is written into the TEST database only; the script stops if
the two names are the same. What is copied: sequences (with their current numbers), tables + columns + defaults,
primary keys / unique / check / foreign keys, indexes, views, rows. Not copied: the trigger functions - the test
website installs them itself when it starts (db_triggers.py), like production.
Afterwards the TEST address for Render = production address with the last part changed to french_fragrance_test.

Where things are:
  SECTION 1  settings          -> TEST_DB
  SECTION 2  addresses         -> database_url(), with_db()
  SECTION 3  create test db    -> create_test_db()
  SECTION 4  copy structure    -> copy_schema()
  SECTION 5  copy rows         -> copy_rows()
  SECTION 6  check + main      -> compare_counts(), main()
"""
import io
import os
import re
import sys
import time

import psycopg2
from psycopg2 import sql as pgsql

HERE = os.path.dirname(os.path.abspath(__file__))


def log(*a):
    """Print with the [COPY-TEST] tag."""
    print("[COPY-TEST]", *a, flush=True)


# ============================================================ SECTION 1: settings
TEST_DB = "french_fragrance_test"          # name of the test database (same server as production)


# ============================================================ SECTION 2: addresses
def database_url():
    """Production address: FF_DATABASE_URL env (used for local tests) else daily_update.bat in this folder."""
    if os.environ.get("FF_DATABASE_URL"):
        return os.environ["FF_DATABASE_URL"]
    path = os.path.join(HERE, "daily_update.bat")
    if not os.path.exists(path):
        sys.exit("[COPY-TEST] daily_update.bat not found - put copy_to_test.py in the PRODUCTION folder next to it")
    with open(path, encoding="utf-8", errors="replace") as f:
        m = re.search(r'set "FF_DATABASE_URL=([^"]+)"', f.read())
    if not m:
        sys.exit("[COPY-TEST] FF_DATABASE_URL not found in daily_update.bat")
    return m.group(1)


def with_db(url, name):
    """Same address, other database name (the part after the last '/', before any '?')."""
    base, _, query = url.partition("?")
    head, _, _ = base.rpartition("/")
    return f"{head}/{name}" + (f"?{query}" if query else "")


def db_name(url):
    return url.partition("?")[0].rpartition("/")[2]


def connect(url):
    """Render needs SSL; a local test database (localhost) does not."""
    local = "@localhost" in url or "@127.0.0.1" in url
    return psycopg2.connect(url) if local else psycopg2.connect(url, sslmode="require")


def show(url):
    """Address for the log WITHOUT the password: host/database."""
    return url.split("@")[-1].partition("?")[0]


# ============================================================ SECTION 3: create test db
def create_test_db(src_url):
    """CREATE DATABASE french_fragrance_test on the production server (only when it does not exist yet)."""
    conn = connect(src_url)
    conn.autocommit = True                     # CREATE DATABASE cannot run inside a transaction
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,))
    if cur.fetchone():
        log(f"Test database {TEST_DB} already exists")
    else:
        cur.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(TEST_DB)))
        log(f"Test database {TEST_DB} CREATED")
    conn.close()


# ============================================================ SECTION 4: copy structure
def q(name):
    """Quoted name for SQL text."""
    return '"' + name.replace('"', '""') + '"'


def copy_schema(src, dst):
    """Sequences, tables (columns + defaults), constraints, indexes, views: production -> test (structure only)."""
    s, d = src.cursor(), dst.cursor()
    # extensions used by production (e.g. pg_trgm) - first, tables / indexes may need them
    s.execute("SELECT extname FROM pg_extension WHERE extname <> 'plpgsql'")
    for (ext,) in s.fetchall():
        try:
            d.execute(f"CREATE EXTENSION IF NOT EXISTS {q(ext)}")
            log(f"extension {ext}")
        except Exception as ex:
            dst.rollback()
            log(f"extension {ext} NOT created ({str(ex).strip()[:120]}) - continuing")
    # sequences (SERIAL id columns) - before the tables that use them
    s.execute("SELECT sequencename, data_type::text FROM pg_sequences WHERE schemaname = 'public' ORDER BY 1")
    rows = s.fetchall()
    seqs = [r[0] for r in rows]
    for name, typ in rows:                             # same number type as production (SERIAL = integer)
        d.execute(f"CREATE SEQUENCE IF NOT EXISTS {q(name)} AS {typ}")
    log(f"{len(seqs)} sequences")
    # tables + columns (type, NOT NULL, default)
    s.execute("SELECT c.oid, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
              "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY 2")
    tables = s.fetchall()
    for oid, name in tables:
        s.execute("""SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
                            pg_get_expr(ad.adbin, ad.adrelid), a.attidentity, a.attgenerated
                       FROM pg_attribute a LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
                      WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""", (oid,))
        cols = []
        for col, typ, notnull, default, ident, generated in s.fetchall():
            part = f"{q(col)} {typ}"
            if generated == "s":
                part += f" GENERATED ALWAYS AS ({default}) STORED"
            elif ident in ("a", "d"):
                part += f" GENERATED {'ALWAYS' if ident == 'a' else 'BY DEFAULT'} AS IDENTITY"
            elif default is not None:
                part += f" DEFAULT {default}"
            if notnull:
                part += " NOT NULL"
            cols.append(part)
        d.execute(f"CREATE TABLE {q(name)} ({', '.join(cols)})")
    log(f"{len(tables)} tables")
    # sequences belong to their id column (as SERIAL makes it)
    s.execute("""SELECT seq.relname, tab.relname, a.attname
                   FROM pg_depend dep
                   JOIN pg_class seq ON seq.oid = dep.objid AND seq.relkind = 'S'
                   JOIN pg_class tab ON tab.oid = dep.refobjid
                   JOIN pg_attribute a ON a.attrelid = tab.oid AND a.attnum = dep.refobjsubid
                   JOIN pg_namespace n ON n.oid = seq.relnamespace
                  WHERE n.nspname = 'public' AND dep.deptype = 'a'""")
    for seq, tab, col in s.fetchall():
        d.execute(f"ALTER SEQUENCE {q(seq)} OWNED BY {q(tab)}.{q(col)}")
    dst.commit()
    return [t[1] for t in tables], seqs


def copy_constraints_indexes_views(src, dst):
    """After the rows are in: keys / unique / checks, then foreign keys, then other indexes, then views."""
    s, d = src.cursor(), dst.cursor()
    s.execute("""SELECT t.relname, c.conname, pg_get_constraintdef(c.oid), c.contype
                   FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
                   JOIN pg_namespace n ON n.oid = t.relnamespace
                  WHERE n.nspname = 'public' AND c.contype IN ('p', 'u', 'c', 'x', 'f')
                  ORDER BY (c.contype = 'f'), t.relname, c.conname""")
    cons = s.fetchall()
    for tab, name, definition, kind in cons:
        d.execute(f"ALTER TABLE {q(tab)} ADD CONSTRAINT {q(name)} {definition}")
    log(f"{len(cons)} keys / constraints")
    s.execute("""SELECT i.indexdef FROM pg_indexes i
                  WHERE i.schemaname = 'public'
                    AND i.indexname NOT IN (SELECT conname FROM pg_constraint)""")
    idx = [r[0] for r in s.fetchall()]
    for definition in idx:
        d.execute(definition)
    log(f"{len(idx)} other indexes")
    s.execute("SELECT viewname, definition FROM pg_views WHERE schemaname = 'public' ORDER BY 1")
    views = s.fetchall()
    for name, definition in views:
        d.execute(f"CREATE VIEW {q(name)} AS {definition}")
    if views:
        log(f"{len(views)} views")
    dst.commit()


# ============================================================ SECTION 5: copy rows
def copy_rows(src, dst, tables, seqs):
    """Every row of every table (COPY = fast), then the sequences' current numbers (new rows get the next id)."""
    s, d = src.cursor(), dst.cursor()
    total = 0
    for name in tables:
        s.execute("""SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
                       JOIN pg_namespace n ON n.oid = c.relnamespace
                      WHERE n.nspname = 'public' AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
                        AND a.attgenerated = '' ORDER BY a.attnum""", (name,))
        cols = ", ".join(q(r[0]) for r in s.fetchall())
        buf = io.StringIO()
        started = time.time()
        s.copy_expert(f"COPY (SELECT {cols} FROM {q(name)}) TO STDOUT", buf)   # production: READ only
        buf.seek(0)
        d.copy_expert(f"COPY {q(name)} ({cols}) FROM STDIN", buf)
        dst.commit()
        d.execute(f"SELECT COUNT(*) FROM {q(name)}")
        n = d.fetchone()[0]
        total += n
        log(f"  {name}: {n} rows ({time.time() - started:.1f} s)")
    for name in seqs:
        s.execute(f"SELECT last_value, is_called FROM {q(name)}")
        last, called = s.fetchone()
        d.execute("SELECT setval(%s, %s, %s)", (q(name), last, called))
    dst.commit()
    log(f"{total} rows copied, {len(seqs)} sequence numbers set")


# ============================================================ SECTION 6: check + main
def compare_counts(src, dst, tables):
    """Rows per table in production vs test - all must be the same."""
    s, d = src.cursor(), dst.cursor()
    bad = []
    for name in tables:
        s.execute(f"SELECT COUNT(*) FROM {q(name)}")
        d.execute(f"SELECT COUNT(*) FROM {q(name)}")
        a, b = s.fetchone()[0], d.fetchone()[0]
        if a != b:
            bad.append(f"{name}: production {a}, test {b}")
    return bad


def main():
    fresh = "--fresh" in sys.argv
    src_url = database_url()
    if db_name(src_url) == TEST_DB:
        sys.exit("[COPY-TEST] FF_DATABASE_URL already points to the TEST database - nothing done")
    dst_url = with_db(src_url, TEST_DB)
    log(f"Production (read only): {show(src_url)}")
    log(f"Test (written):         {show(dst_url)}")
    create_test_db(src_url)

    src, dst = connect(src_url), connect(dst_url)
    src.set_session(readonly=True)                     # production: the session itself refuses any change
    try:
        d = dst.cursor()
        d.execute("SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public'")
        if d.fetchone()[0]:
            if not fresh:
                sys.exit("[COPY-TEST] The test database already has tables. To replace them with a fresh copy of "
                         "production run:  python copy_to_test.py --fresh")
            log("--fresh: emptying the TEST database")
            d.execute("DROP SCHEMA public CASCADE")
            d.execute("CREATE SCHEMA public")
            d.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")          # as a new database has it
            d.execute("COMMENT ON SCHEMA public IS 'standard public schema'")
            dst.commit()
        started = time.time()
        tables, seqs = copy_schema(src, dst)
        copy_rows(src, dst, tables, seqs)
        copy_constraints_indexes_views(src, dst)
        bad = compare_counts(src, dst, tables)
        if bad:
            log("WARNING - row counts differ (production changed during the copy?): " + "; ".join(bad))
        else:
            log(f"CHECK OK: all {len(tables)} tables have the same number of rows")
        log(f"Done in {time.time() - started:.0f} s. Test address = production address with the last part "
            f"changed to /{TEST_DB}")
    except SystemExit:
        raise
    except Exception as ex:
        dst.rollback()
        log(f"ERROR: {ex}")
        log("Production was NOT changed. Fix the problem, then run:  python copy_to_test.py --fresh")
        sys.exit(1)
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    main()

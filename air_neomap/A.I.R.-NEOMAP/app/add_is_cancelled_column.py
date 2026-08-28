"""
Adds the new is_cancelled column to the existing services table.

models.py alone won't apply this to a database that already exists --
db.create_all() (called in app/__init__.py on every startup) only
creates tables that are missing entirely; it never alters an existing
table's columns. Run this once against your real database file after
deploying the updated models.py/routes.py/index.html, the same way
add_event_columns.py was used for the earlier venue/start_time/etc.
columns.

Usage: point DB_PATH at whichever file your app is actually using.
Locally that's usually instance/neomap.db (see add_event_columns.py);
in the Fly.io deployment it's /data/neomap.db (see add_columns.py).
Run this from inside that same environment.
"""
import sqlite3
import sys

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "instance/neomap.db"

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

cur.execute("PRAGMA table_info(services)")
existing_columns = [row[1] for row in cur.fetchall()]

if "is_cancelled" in existing_columns:
    print("Column already exists, skipping: is_cancelled")
else:
    print("Adding column: is_cancelled")
    # SQLite's ALTER TABLE ADD COLUMN requires a constant default when
    # the column is NOT NULL, not an expression -- 0 (false) matches
    # the Boolean default=False on the new model column, and backfills
    # every existing row as "not cancelled," which is the correct
    # historical default: nothing that already exists in this table
    # was ever cancelled under the old schema, since there was no way
    # to mark it so.
    cur.execute("ALTER TABLE services ADD COLUMN is_cancelled BOOLEAN NOT NULL DEFAULT 0")

conn.commit()
conn.close()
print("Done.")
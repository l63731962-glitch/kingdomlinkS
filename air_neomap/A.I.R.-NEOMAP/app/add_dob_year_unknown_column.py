"""
Adds the new dob_year_unknown column to the existing members table.

Same situation as add_is_cancelled_column.py: models.py alone won't
apply this to a database that already exists, since db.create_all()
(called in app/__init__.py on every startup) only creates tables that
are missing entirely, never alters an existing table's columns. Run
this once against your real database file after deploying the updated
models.py/routes.py/index.html/attendance_logic.py.

Usage: point DB_PATH at whichever file your app is actually using.
On the Fly.io deployment that's /data/neomap.db.
"""
import sqlite3
import sys

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "instance/neomap.db"

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

cur.execute("PRAGMA table_info(members)")
existing_columns = [row[1] for row in cur.fetchall()]

if "dob_year_unknown" in existing_columns:
    print("Column already exists, skipping: dob_year_unknown")
else:
    print("Adding column: dob_year_unknown")
    # Same reasoning as the is_cancelled migration: SQLite's ALTER
    # TABLE ADD COLUMN needs a constant default when NOT NULL. 0
    # (false) is correct here too -- every existing member's
    # date_of_birth, if set, was entered as a real full date under the
    # old schema (there was no month-only option before this), so
    # nothing already on file is actually year-unknown.
    cur.execute("ALTER TABLE members ADD COLUMN dob_year_unknown BOOLEAN NOT NULL DEFAULT 0")

conn.commit()
conn.close()
print("Done.")
"""
Adds the missing consecutive_missed_weeks column to the existing
cell_groups table on Postgres (Render production database).

Same root cause as add_is_cancelled_column.py and
add_dob_year_unknown_column.py: db.create_all() (called in
app/__init__.py on every startup) only creates tables that are
missing entirely -- it never alters an existing table's columns.
cell_groups already existed in production before
consecutive_missed_weeks was added to the CellGroup model in
models.py, so the column was never added to the live database.

This version targets Postgres (psycopg2) instead of sqlite3, since
Render's DATABASE_URL points at Postgres, not a local .db file.

Usage:
    python add_consecutive_missed_weeks_column.py

Reads DATABASE_URL from the environment -- run this with the same
DATABASE_URL Render uses. Easiest way: open a Shell tab on your
Render service (Render dashboard -> your service -> Shell) and run
it there, since DATABASE_URL is already set in that environment.
"""
import os
import sys

try:
    import psycopg2
except ImportError:
    print("psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL environment variable is not set.")
    print("Run this from the Render Shell tab, where DATABASE_URL is already set,")
    print("or set it manually: DATABASE_URL=postgres://... python add_consecutive_missed_weeks_column.py")
    sys.exit(1)

# SQLAlchemy sometimes uses postgres:// but psycopg2 needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = True
cur = conn.cursor()

cur.execute("""
    SELECT column_name FROM information_schema.columns
    WHERE table_name = 'cell_groups'
""")
existing_columns = [row[0] for row in cur.fetchall()]

if "consecutive_missed_weeks" in existing_columns:
    print("Column already exists, skipping: consecutive_missed_weeks")
else:
    print("Adding column: consecutive_missed_weeks")
    # Default 0 -- every existing cell group starts with a clean
    # slate, same reasoning as the is_cancelled/dob_year_unknown
    # migrations: nothing already on file was tracked against this
    # counter under the old schema, so there's nothing to backfill
    # except zero.
    cur.execute(
        "ALTER TABLE cell_groups ADD COLUMN consecutive_missed_weeks INTEGER NOT NULL DEFAULT 0"
    )
    print("Done.")

cur.close()
conn.close()

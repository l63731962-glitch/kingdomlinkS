import sqlite3

conn = sqlite3.connect("/data/neomap.db")
cur = conn.cursor()

new_columns = [
    ("venue", "VARCHAR(200)"),
    ("start_time", "VARCHAR(10)"),
    ("end_time", "VARCHAR(10)"),
    ("description", "TEXT"),
    ("visible_to", "VARCHAR(30) DEFAULT 'everyone'"),
]

for col_name, col_type in new_columns:
    try:
        cur.execute(f"ALTER TABLE services ADD COLUMN {col_name} {col_type}")
        print(f"Added: {col_name}")
    except sqlite3.OperationalError as e:
        print(f"Skipped {col_name}: {e}")

conn.commit()
conn.close()
print("Done.")
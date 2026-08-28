import sqlite3

conn = sqlite3.connect("instance/neomap.db")
cur = conn.cursor()

new_columns = [
    ("venue", "VARCHAR(200)"),
    ("start_time", "VARCHAR(10)"),
    ("end_time", "VARCHAR(10)"),
    ("description", "TEXT"),
    ("visible_to", "VARCHAR(30) DEFAULT 'everyone'"),
]

cur.execute("PRAGMA table_info(services)")
existing_columns = [row[1] for row in cur.fetchall()]

for col_name, col_type in new_columns:
    if col_name not in existing_columns:
        print(f"Adding column: {col_name}")
        cur.execute(f"ALTER TABLE services ADD COLUMN {col_name} {col_type}")
    else:
        print(f"Column already exists, skipping: {col_name}")

conn.commit()
conn.close()
print("Done.")
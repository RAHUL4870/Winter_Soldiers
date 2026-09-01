import sqlite3

con = sqlite3.connect("./data/nexafreight.db")
cur = con.cursor()
sql = cur.execute("SELECT sql FROM sqlite_master WHERE name='position_reports';").fetchone()[0]
print("Existing DDL:\n", sql)

indices = cur.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='position_reports';").fetchall()
print("Indices:\n", indices)

# Ensure unique index exists on (leg_id, reported_at)
cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_position_reports_leg_reported_at ON position_reports(leg_id, reported_at);")
con.commit()
print("Unique index verified/created.")
con.close()

import duckdb
con = duckdb.connect()
con.execute("INSTALL spatial; LOAD spatial;")
p = "data/height_cache/overture_29.56_106.58.parquet"
n = con.execute(f"SELECT count(*) FROM '{p}'").fetchone()[0]
n_h = con.execute(f"SELECT count(*) FROM '{p}' WHERE height IS NOT NULL AND height > 0").fetchone()[0]
n_f = con.execute(f"SELECT count(*) FROM '{p}' WHERE num_floors IS NOT NULL AND num_floors > 0").fetchone()[0]
print(f"Total: {n:,}")
print(f"With height: {n_h}")
print(f"With num_floors: {n_f}")
if n_f > 0:
    rows = con.execute(f"SELECT min(num_floors), max(num_floors), avg(num_floors) FROM '{p}' WHERE num_floors IS NOT NULL AND num_floors > 0").fetchone()
    print(f"num_floors: min={rows[0]}, max={rows[1]}, avg={rows[2]:.1f}")

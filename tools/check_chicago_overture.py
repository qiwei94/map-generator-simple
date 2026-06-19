import duckdb
con = duckdb.connect()
con.execute("INSTALL spatial; LOAD spatial;")
p = "data/height_cache/overture_41.88_-87.62.parquet"
n = con.execute(f"SELECT count(*) FROM '{p}'").fetchone()[0]
n_h = con.execute(f"SELECT count(*) FROM '{p}' WHERE height IS NOT NULL AND height > 0").fetchone()[0]
n_f = con.execute(f"SELECT count(*) FROM '{p}' WHERE num_floors IS NOT NULL AND num_floors > 0").fetchone()[0]
n_n = con.execute(f"SELECT count(*) FROM '{p}' WHERE names IS NOT NULL").fetchone()[0]
print(f"Total: {n:,}")
print(f"With height: {n_h:,} ({n_h/n*100:.1f}%)")
print(f"With num_floors: {n_f:,} ({n_f/n*100:.1f}%)")
print(f"With names: {n_n:,} ({n_n/n*100:.1f}%)")
print(f"With height OR num_floors: {(n_h+n_f)/n*100:.1f}%")

if n_h > 0:
    rows = con.execute(f"""
        SELECT min(height), percentile_cont(0.25) WITHIN GROUP (ORDER BY height),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY height),
               percentile_cont(0.75) WITHIN GROUP (ORDER BY height), max(height)
        FROM '{p}' WHERE height IS NOT NULL AND height > 0
    """).fetchone()
    print(f"Height: min={rows[0]:.0f} p25={rows[1]:.0f} p50={rows[2]:.0f} p75={rows[3]:.0f} max={rows[4]:.0f}m")

# Top 20 tallest buildings
print("\nTop 20 tallest:")
for row in con.execute(f"""
    SELECT names.primary, height, num_floors, class
    FROM '{p}' WHERE height >= 100
    ORDER BY height DESC LIMIT 20
""").fetchall():
    name = row[0] or "(unnamed)"
    print(f"  {name:35s} h={row[1]:6.1f}m floors={row[2] or '-':>3}")

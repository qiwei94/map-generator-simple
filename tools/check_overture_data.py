import duckdb

con = duckdb.connect()
con.execute("INSTALL spatial; LOAD spatial;")
p = "data/height_cache/hangzhou_buildings.parquet"

# 高度 >= 50m 的建筑（地标级别）
print("=== Buildings with height >= 50m ===")
n_tall = con.execute(
    f"SELECT count(*) FROM '{p}' WHERE height >= 50"
).fetchone()[0]
print(f"Total: {n_tall}")

print("\nTop 30 tallest:")
for row in con.execute(
    f"""SELECT names.primary, height, num_floors, subtype, class
    FROM '{p}'
    WHERE height >= 50
    ORDER BY height DESC
    LIMIT 30"""
).fetchall():
    name = row[0] or "(unnamed)"
    floors = row[2] if row[2] else "-"
    subtype = row[3] or "-"
    cls = row[4] or "-"
    print(f"  {name:35s} h={row[1]:6.1f}m  floors={floors:>3}  type={subtype}/{cls}")

# 高度 30-50m (中等建筑)
n_mid = con.execute(
    f"SELECT count(*) FROM '{p}' WHERE height >= 30 AND height < 50"
).fetchone()[0]
print(f"\n=== Buildings with 30-50m height: {n_mid} ===")

# 高度 15-30m
n_low = con.execute(
    f"SELECT count(*) FROM '{p}' WHERE height >= 15 AND height < 30"
).fetchone()[0]
print(f"=== Buildings with 15-30m height: {n_low} ===")

# 对比: OSM 有高度的建筑 vs Overture 有高度的
# OSM 72527 buildings, 14.4% have height = ~10,444 buildings
# Overture 257,138 buildings, 2.3% have height = ~5,855
# 但 Overture 可能覆盖了一些 OSM 没有高度的大型建筑
print(f"\n=== Summary ===")
print(f"Overture total: 257,138 buildings")
print(f"Overture with height >= 50m: {n_tall}")
print(f"Overture with height 30-50m: {n_mid}")
print(f"Overture with height 15-30m: {n_low}")
print(f"Overture with height < 15m: {5153 - n_tall - n_mid - n_low}")

con.close()

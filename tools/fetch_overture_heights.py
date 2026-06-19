"""用 DuckDB 从 S3 直接查询 Overture Maps 杭州建筑高度数据。

只下载 bbox 内的数据，不需要下载全球数据集。
"""
import duckdb
import time
import os

t0 = time.time()
con = duckdb.connect()
con.execute("INSTALL spatial; LOAD spatial;")
con.execute("SET s3_region='us-west-2'")

out_dir = "data/height_cache"
os.makedirs(out_dir, exist_ok=True)
out_path = os.path.join(out_dir, "hangzhou_buildings.parquet")

# 西湖 bbox: 30.13, 120.01, 30.36, 120.29 (稍微扩大)
print("Querying Overture Maps S3 for Hangzhou buildings...")

# 先查看最新 release 版本
try:
    con.execute("""
    COPY(
      SELECT id, height, names, geometry
      FROM read_parquet(
        's3://overturemaps-us-west-2/release/2025-05-20.0/theme=buildings/type=building/*',
        hive_partitioning=1
      )
      WHERE bbox.xmin BETWEEN 119.99 AND 120.31
        AND bbox.ymin BETWEEN 30.11 AND 30.38
    ) TO '""" + out_path.replace("\\", "/") + """'
    """)
except Exception as e:
    print(f"2025-05-20 failed: {e}")
    print("Trying 2025-04-23...")
    con.execute("""
    COPY(
      SELECT id, height, names, geometry
      FROM read_parquet(
        's3://overturemaps-us-west-2/release/2025-04-23.0/theme=buildings/type=building/*',
        hive_partitioning=1
      )
      WHERE bbox.xmin BETWEEN 119.99 AND 120.31
        AND bbox.ymin BETWEEN 30.11 AND 30.38
    ) TO '""" + out_path.replace("\\", "/") + """'
    """)

size_mb = os.path.getsize(out_path) / (1024 * 1024)
n = con.execute(
    f"SELECT count(*) FROM read_parquet('{out_path.replace(chr(92), '/')}')"
).fetchone()[0]
print(f"Done: {n:,} buildings, {size_mb:.1f} MB, {time.time()-t0:.1f}s")

# 统计有高度的比例
n_h = con.execute(
    f"SELECT count(*) FROM read_parquet('{out_path.replace(chr(92), '/')}') "
    f"WHERE height IS NOT NULL AND height > 0"
).fetchone()[0]
print(f"With height: {n_h:,} ({n_h/n*100:.1f}%)")

# 高度分布
print("\nHeight distribution:")
rows = con.execute(
    f"SELECT "
    f"  min(height) as min_h, "
    f"  percentile_cont(0.25) WITHIN GROUP (ORDER BY height) as p25, "
    f"  percentile_cont(0.50) WITHIN GROUP (ORDER BY height) as p50, "
    f"  percentile_cont(0.75) WITHIN GROUP (ORDER BY height) as p75, "
    f"  max(height) as max_h "
    f"FROM read_parquet('{out_path.replace(chr(92), '/')}') "
    f"WHERE height IS NOT NULL AND height > 0 AND height < 500"
).fetchone()
print(f"  min={rows[0]:.1f}m, p25={rows[1]:.1f}m, p50={rows[2]:.1f}m, p75={rows[3]:.1f}m, max={rows[4]:.1f}m")

# 看看有名字的建筑
n_named = con.execute(
    f"SELECT count(*) FROM read_parquet('{out_path.replace(chr(92), '/')}') "
    f"WHERE names IS NOT NULL"
).fetchone()[0]
print(f"\nWith names: {n_named:,} ({n_named/n*100:.1f}%)")

con.close()

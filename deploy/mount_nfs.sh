#!/bin/bash
# 新节点挂载 NFS 数据
mkdir -p /root/map-cache
if ! mountpoint -q /root/map-cache; then
  mount -t nfs -o ro,sync,noatime 172.16.164.53:/root/map-cache /root/map-cache
fi
echo "--- mounted? ---"
mountpoint /root/map-cache && echo MOUNTED
echo "--- pbf count ---"
ls /root/map-cache/pbf_cache/*.osm.pbf 2>/dev/null | wc -l
echo "--- dem ---"
du -sh /root/map-cache/dem_cache 2>/dev/null | head -1

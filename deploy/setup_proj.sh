#!/bin/bash
# 新节点：解压项目 + 配置数据软链 + 目录
cd /root && tar xzf /tmp/proj.tgz
cd /root/map-generator-simple
mkdir -p output tmp data
# pbf_cache 软链到 NFS 数据
rm -rf pbf_cache
ln -s /root/map-cache/pbf_cache pbf_cache
echo "--- verify ---"
ls pbf_cache/*.osm.pbf 2>/dev/null | wc -l
/usr/local/python3.9/bin/python3.9 -c "print('proj python ok')"

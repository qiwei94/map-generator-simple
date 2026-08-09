#!/bin/bash
# all-in-one: web+计算 同机，本地模式（不需要 worker-pull）
systemctl disable worker.service 2>/dev/null
systemctl stop worker.service 2>/dev/null

cat > /etc/systemd/system/studio.service << 'EOF'
[Unit]
Description=Journey Relief Studio (all-in-one)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/map-generator-simple
Environment="STUDIO_PORT=80"
Environment="MAP_GEN_CACHE_DIR=/root/map-cache"
Environment="PYTHONIOENCODING=utf-8"
Environment="PATH=/opt/pyshim:/usr/local/python3.9/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart=/usr/local/python3.9/bin/python3.9 webapp/server.py
Restart=always
RestartSec=5
StandardOutput=append:/var/log/studio.log
StandardError=append:/var/log/studio.log

[Install]
WantedBy=multi-user.target
EOF
# 关键：把项目内 dem_cache / pbf_cache 软链到本地数据盘。
# elevation 的离线瓦片只查 项目目录/dem_cache/srtm/Nxx/xxx.hgt；若项目里没有
# 这个目录，它会去 AWS 下载 DEM 瓦片，该源在国内云被黑洞 -> 0% CPU 无限卡死。
ln -sfn /root/map-cache/dem_cache /root/map-generator-simple/dem_cache
ln -sfn /root/map-cache/pbf_cache /root/map-generator-simple/pbf_cache

systemctl daemon-reload
systemctl enable studio.service
systemctl restart studio.service
sleep 4
echo "--- status ---"
systemctl is-active studio.service
echo "--- api test ---"
curl -s http://127.0.0.1:80/api/cities | head -c 120; echo

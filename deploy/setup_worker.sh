#!/bin/bash
# 新节点：配置 worker systemd 服务
cat > /etc/systemd/system/worker.service << 'EOF'
[Unit]
Description=Journey Relief Compute Worker
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/map-generator-simple
Environment="MAP_GEN_CACHE_DIR=/root/map-cache"
Environment="PYTHONIOENCODING=utf-8"
ExecStart=/usr/local/python3.9/bin/python3.9 tools/cloud_worker.py --server http://172.16.164.53 --token jr2025_secret_worker --poll-interval 3
Restart=always
RestartSec=5
StandardOutput=append:/var/log/worker.log
StandardError=append:/var/log/worker.log

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable worker.service
systemctl restart worker.service
sleep 3
echo "--- status ---"
systemctl is-active worker.service
echo "--- log ---"
tail -5 /var/log/worker.log

#!/bin/bash
# 让 tools/osmium 的 shebang(python3) 指向带 pyosmium 的 python3.9
mkdir -p /opt/pyshim
ln -sf /usr/local/python3.9/bin/python3.9 /opt/pyshim/python3
# 验证 shim
/opt/pyshim/python3 -c "import osmium; print('shim pyosmium ok')"
# worker.service 加 PATH（shim 在最前）
grep -q 'PATH=/opt/pyshim' /etc/systemd/system/worker.service || \
sed -i 's|Environment="PYTHONIOENCODING=utf-8"|Environment="PYTHONIOENCODING=utf-8"\nEnvironment="PATH=/opt/pyshim:/usr/local/python3.9/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"|' /etc/systemd/system/worker.service
systemctl daemon-reload
systemctl restart worker
sleep 3
systemctl is-active worker

#!/bin/bash
# 在新节点解压 python 环境并验证
cd / && tar xzf /tmp/py39.tgz
/usr/local/python3.9/bin/python3.9 -c "import numpy,shapely,trimesh,geopandas,pyproj,scipy,PIL; print('PYENV_OK')"

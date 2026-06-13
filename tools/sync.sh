#!/bin/bash
# ============================================================
# 双向同步脚本 (rsync + WSL)
# 使用方式（在 WSL 中运行）：
#   ./tools/sync.sh pull    # 从远程服务器拉取到本地
#   ./tools/sync.sh push    # 从本地推送到远程服务器
#   ./tools/sync.sh dry-run # 试运行，查看差异
# ============================================================

set -e

LOCAL=$(cd "$(dirname "$0")/.." && pwd)
REMOTE="root@8.136.0.235:/data/pipeline_code"
EXCLUDES="--exclude=.git --exclude=.DS_Store --exclude=__pycache__ --exclude=*.pyc --exclude=cache/ --exclude=pbf_cache/ --exclude=output/ --exclude=tmp/"
RSYNC_OPTS="-avz --progress $EXCLUDES"

case "${1:-pull}" in
  pull)
    echo ">>> 从远程服务器拉取到本地..."
    rsync $RSYNC_OPTS "$REMOTE/" "$LOCAL/"
    echo ">>> 拉取完成"
    ;;
  push)
    echo ">>> 从本地推送到远程服务器..."
    rsync $RSYNC_OPTS "$LOCAL/" "$REMOTE/"
    echo ">>> 推送完成"
    ;;
  dry-run)
    echo ">>> 试运行：查看将要变化的文件..."
    rsync $RSYNC_OPTS --dry-run "$REMOTE/" "$LOCAL/"
    echo ">>> 试运行结束"
    ;;
  *)
    echo "用法: $0 {pull|push|dry-run}"
    exit 1
    ;;
esac

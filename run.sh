#!/usr/bin/env sh
# 用法: ./run.sh /path/to/xiaomi_camera_videos
set -e
if [ -z "$1" ]; then
  echo "用法: ./run.sh <录像目录>"
  exit 1
fi
DIR=$(cd "$(dirname "$0")" && pwd)
exec python3 "$DIR/server.py" --root "$1"

#!/usr/bin/env sh
# 不带参数启动：在网页里选文件夹，下次自动记住
# 也可以 ./run.sh /path/to/xiaomi_camera_videos 直接指定目录
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
exec python3 "$DIR/server.py" "$@"

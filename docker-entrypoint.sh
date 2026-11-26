#!/bin/sh
# 容器启动脚本：把环境变量翻译成 server.py 的命令行参数。
# 所有参数都有默认值，直接 docker run 也能跑起来。
set -e

ROOT="${CAM_ROOT:-/videos}"
CACHE="${CAM_CACHE:-/cache}"

if [ ! -d "$ROOT" ]; then
    echo "[!] 录像目录 $ROOT 不存在。" >&2
    echo "    请把米家导出的目录挂进来，例如 -v /vol1/1000/xiaomi_camera_videos:/videos:ro" >&2
    mkdir -p "$ROOT"
fi

mkdir -p "$CACHE" 2>/dev/null || true

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[!] 镜像内没有找到 ffmpeg，无法转码播放。" >&2
    exit 2
fi

echo "[*] 录像目录 : $ROOT"
echo "[*] 缓存目录 : $CACHE (上限 ${CAM_CACHE_MAX_GB:-20} GB)"

exec python3 /app/server.py \
    --root "$ROOT" \
    --cache "$CACHE" \
    --host "${CAM_HOST:-0.0.0.0}" \
    --port "${CAM_PORT:-8765}" \
    --cache-max-gb "${CAM_CACHE_MAX_GB:-20}" \
    --height "${CAM_HEIGHT:-720}" \
    --fps "${CAM_FPS:-15}" \
    --crf "${CAM_CRF:-26}" \
    --no-browser \
    "$@"

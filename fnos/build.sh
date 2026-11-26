#!/usr/bin/env bash
# 打包飞牛 fnOS 应用（.fpk）
#
#   ./build.sh                              # 用 manifest 里的版本号
#   FNPACK=/path/to/fnpack ./build.sh       # 指定 fnpack 路径
#   IMAGE_REPO=ghcr.io/you/xiaomi-camera-viewer ./build.sh
#
# 依赖：fnpack（https://developer.fnnas.com/docs/cli/fnpack）、python3
# 产物：当前目录下的 xiaomi-cam-viewer_<version>.fpk
set -euo pipefail

cd "$(dirname "$0")"

VERSION="$(sed -n 's/^version=//p' manifest | head -1 | tr -d '\r')"
if [ -z "$VERSION" ]; then
    echo "manifest 里没有读到 version" >&2
    exit 1
fi

IMAGE_REPO="${IMAGE_REPO:-ghcr.io/szjmhxd/xiaomi-camera-viewer}"
IMAGE="${IMAGE_REPO}:${VERSION}"
FNPACK_BIN="${FNPACK:-fnpack}"

if ! command -v "$FNPACK_BIN" >/dev/null 2>&1; then
    echo "找不到 fnpack，先下载对应平台的二进制：" >&2
    echo "  https://developer.fnnas.com/docs/cli/fnpack" >&2
    echo "也可以 FNPACK=/path/to/fnpack ./build.sh" >&2
    exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cp -r manifest ICON.PNG ICON_256.PNG app cmd config wizard "$WORK"/
if [ -f ../LICENSE ]; then
    cp ../LICENSE "$WORK"/LICENSE
fi

# 让 compose 里的镜像 tag 始终等于 manifest 版本，两处不会漂移
PYBIN="$(command -v python3 || command -v python)"
if [ -z "$PYBIN" ]; then
    echo "找不到 python3 / python" >&2
    exit 1
fi
"$PYBIN" - "$WORK/app/docker/docker-compose.yaml" "$IMAGE" <<'PY'
import re
import sys

path, image = sys.argv[1], sys.argv[2]
src = open(path, encoding="utf-8").read()
src, n = re.subn(r"(?m)^(\s*image:\s*).*$", lambda m: m.group(1) + image, src, count=1)
if not n:
    sys.exit("docker-compose.yaml 里没有找到 image 行")
open(path, "w", encoding="utf-8").write(src)
print(f"image -> {image}")
PY

chmod +x "$WORK"/cmd/*

echo "打包 ${VERSION} ..."
# fnpack 的产物落在「当前目录」而不是 --directory（1.2.x/1.3.x 实测如此），
# 所以切进 WORK 再执行，两种行为都能覆盖到。
# 签名：默认用本地导入的开发者证书；没配证书时 FNPACK_FLAGS=--nosign 跳过
# （fnpack 原话：For local debugging only——自用 NAS 手动安装够用）。
FNPACK_FLAGS="${FNPACK_FLAGS:-}"
# shellcheck disable=SC2086
( cd "$WORK" && "$FNPACK_BIN" build --directory "$WORK" $FNPACK_FLAGS )

OUT="$(ls -1t "$WORK"/*.fpk 2>/dev/null | head -1 || true)"
if [ -z "$OUT" ]; then
    echo "fnpack 没有产出 .fpk" >&2
    exit 1
fi

DEST="xiaomi-cam-viewer_${VERSION}.fpk"
cp "$OUT" "./$DEST"
echo "完成：$(pwd)/$DEST"

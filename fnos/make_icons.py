#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成飞牛应用图标（ICON.PNG / ICON_256.PNG 与桌面入口图标）。

    python make_icons.py

图形是深橙渐变圆角方块 + 白色播放键 + 一条时间轴刻度，64px 下仍然清晰。
改了配色重新跑一次即可，产物直接覆盖。
"""

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SS = 8          # 超采样倍数，缩小后边缘才不锯齿


def gradient(size: int, top: str, bottom: str) -> Image.Image:
    img = Image.new("RGB", (1, size))
    t = tuple(int(top[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(bottom[i:i + 2], 16) for i in (1, 3, 5))
    px = img.load()
    for y in range(size):
        k = y / max(1, size - 1)
        px[0, y] = tuple(round(t[i] + (b[i] - t[i]) * k) for i in range(3))
    return img.resize((size, size))


def render(size: int) -> Image.Image:
    n = size * SS
    img = gradient(n, "#ff9a4d", "#f25400").convert("RGBA")

    # 圆角蒙版：主体不铺满，留一点边距更像系统图标
    pad = round(n * 0.045)
    radius = round(n * 0.24)
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [pad, pad, n - pad - 1, n - pad - 1], radius=radius, fill=255)

    d = ImageDraw.Draw(img)

    # 播放三角
    cx, cy = n * 0.5, n * 0.445
    w, h = n * 0.21, n * 0.245
    d.polygon([(cx - w * 0.55, cy - h), (cx - w * 0.55, cy + h), (cx + w * 0.85, cy)],
              fill=(255, 255, 255, 255))

    # 时间轴：一条细轨道，上面压着几段"有录像"的粗块
    bar_y = round(n * 0.72)
    track_h = max(2, round(n * 0.018))
    seg_h = max(3, round(n * 0.042))
    left, right = round(n * 0.19), round(n * 0.81)
    d.rounded_rectangle([left, bar_y, right, bar_y + track_h],
                        radius=track_h // 2, fill=(255, 255, 255, 105))
    for x0, x1 in ((0.23, 0.43), (0.50, 0.63), (0.69, 0.78)):
        d.rounded_rectangle(
            [round(n * x0), bar_y - (seg_h - track_h) // 2,
             round(n * x1), bar_y + seg_h - (seg_h - track_h) // 2],
            radius=seg_h // 2, fill=(255, 255, 255, 255))

    img.putalpha(mask)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    targets = [
        (HERE / "ICON.PNG", 64),
        (HERE / "ICON_256.PNG", 256),
        (HERE / "app" / "ui" / "images" / "icon_64.png", 64),
        (HERE / "app" / "ui" / "images" / "icon_256.png", 256),
    ]
    for path, size in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        render(size).save(path, "PNG", optimize=True)
        print(f"{path.relative_to(HERE)}  {size}x{size}  {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()

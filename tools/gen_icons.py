"""生成 PWA 图标（PNG，纯标准库）。

画一个蓝色圆角方块 + 白色闪电。用射线法做多边形填充，不依赖任何图形库。
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(r"E:\app\yischool-power\web\icons")
OUT.mkdir(parents=True, exist_ok=True)

BG = (37, 99, 235)        # #2563eb
FG = (255, 255, 255)

# 闪电多边形（归一化坐标 0..1），大致是 ⚡ 的轮廓
BOLT = [
    (0.56, 0.06), (0.26, 0.54), (0.46, 0.54),
    (0.40, 0.94), (0.74, 0.42), (0.52, 0.42),
]


def _inside(px: float, py: float, poly: list[tuple[float, float]]) -> bool:
    """射线法：点是否在多边形内。"""
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > py) != (yj > py):
            xint = (xj - xi) * (py - yi) / (yj - yi) + xi
            if px < xint:
                inside = not inside
        j = i
    return inside


def make(size: int, radius_ratio: float = 0.22, bolt_scale: float = 0.62) -> bytes:
    """生成一张 size×size 的 PNG。"""
    r = size * radius_ratio
    # 闪电缩放到中间
    cx = cy = 0.5
    poly = [((x - cx) * bolt_scale + cx, (y - cy) * bolt_scale + cy) for x, y in BOLT]

    rows = []
    for y in range(size):
        row = bytearray()
        row.append(0)  # filter type 0
        for x in range(size):
            px, py = (x + 0.5) / size, (y + 0.5) / size
            # 圆角矩形判定
            dx = max(r - px * size, 0, px * size - (size - r))
            dy = max(r - py * size, 0, py * size - (size - r))
            corner = (dx * dx + dy * dy) ** 0.5
            if corner > r:
                row.extend((0, 0, 0, 0))
                continue
            # 抗锯齿：只在圆角半径足够大时做。
            # r 很小时（方形图标）edge 取 0，否则 (r-corner)/edge 会算出极小值，
            # 整张图会变成近乎透明。
            a = 255
            edge = 1.5 if r >= 2 else 0.0
            if edge and corner > r - edge:
                a = int(255 * max(0.0, min(1.0, (r - corner) / edge)))
            col = FG if _inside(px, py, poly) else BG
            row.extend((col[0], col[1], col[2], a))
        rows.append(bytes(row))

    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    return png


if __name__ == "__main__":
    for s in (192, 512):
        (OUT / f"icon-{s}.png").write_bytes(make(s))
    # iOS 主屏图标：不能带透明，铺满
    (OUT / "apple-touch-icon.png").write_bytes(make(180, radius_ratio=0.001))
    (OUT / "favicon.png").write_bytes(make(64, radius_ratio=0.18))
    for f in sorted(OUT.glob("*.png")):
        print(f"  {f.name}  {f.stat().st_size} bytes")

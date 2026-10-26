"""Çini yıldızını bağımlılık olmadan PNG'ye çizer; yalnız geliştirmede çalıştırılır."""

import math
import struct
import zlib
from pathlib import Path

POINTS = [
    (12, 2),
    (14.8, 5.2),
    (19.2, 4.8),
    (18.8, 9.2),
    (22, 12),
    (18.8, 14.8),
    (19.2, 19.2),
    (14.8, 18.8),
    (12, 22),
    (9.2, 18.8),
    (4.8, 19.2),
    (5.2, 14.8),
    (2, 12),
    (5.2, 9.2),
    (4.8, 4.8),
    (9.2, 5.2),
]


def inside(x, y):
    count = 0
    for (ax, ay), (bx, by) in zip(POINTS, POINTS[1:] + POINTS[:1], strict=True):
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            count += 1
    return count % 2


def edge(x, y):
    distance = float("inf")
    for (ax, ay), (bx, by) in zip(POINTS, POINTS[1:] + POINTS[:1], strict=True):
        dx, dy = bx - ax, by - ay
        ratio = max(0, min(1, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
        distance = min(distance, math.hypot(x - ax - ratio * dx, y - ay - ratio * dy))
    return distance <= 0.6


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def render(size, outline):
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            coverage = (
                sum(
                    (edge if outline else inside)(
                        (x + (sx + 0.5) / 4) * 24 / size, (y + (sy + 0.5) / 4) * 24 / size
                    )
                    for sy in range(4)
                    for sx in range(4)
                )
                / 16
            )
            if outline:
                rows.extend((255, 255, 255, round(coverage * 255)))
            else:
                rows.extend(
                    (
                        round(28 * (1 - coverage) + 255 * coverage),
                        round(63 * (1 - coverage) + 255 * coverage),
                        round(148 * (1 - coverage) + 255 * coverage),
                        255,
                    )
                )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    destination = Path(__file__).resolve().parents[1] / "src" / "katip" / "teams" / "assets"
    destination.mkdir(exist_ok=True)
    for name, size, outline in (("color.png", 192, False), ("outline.png", 32, True)):
        (destination / name).write_bytes(render(size, outline))

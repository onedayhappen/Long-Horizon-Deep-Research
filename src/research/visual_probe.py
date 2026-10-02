"""Small randomized actual-image transport probe, not a scientific vision benchmark."""
from __future__ import annotations

import secrets
import struct
import zlib

from .visual_models import VisualError, digest, strict_json

GLYPHS = (
    ("111", "101", "101", "101", "111"), ("010", "110", "010", "010", "111"),
    ("111", "001", "111", "100", "111"), ("111", "001", "111", "001", "111"),
    ("101", "101", "111", "001", "001"), ("111", "100", "111", "001", "111"),
    ("111", "100", "111", "101", "111"), ("111", "001", "001", "001", "001"),
    ("111", "101", "111", "101", "111"), ("111", "101", "111", "001", "111"),
)


def challenge_png(marker):
    scale, margin = 8, 16
    width, height = (len(marker) * 4 - 1) * scale + 2 * margin, 5 * scale + 2 * margin
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            cx, cy = (x - margin) // scale, (y - margin) // scale
            digit, gx = divmod(cx, 4)
            black = 0 <= digit < len(marker) and 0 <= cy < 5 and gx < 3 and GLYPHS[int(marker[digit])][cy][gx] == "1"
            row.append(0 if black else 255)
        rows.append(bytes(row))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")

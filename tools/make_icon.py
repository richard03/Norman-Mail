"""Vygeneruje assets/mailer.ico (bez externích knihoven)."""
import math, struct, zlib
from pathlib import Path

BLUE, WHITE = (37, 99, 235), (255, 255, 255)

def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)

def sample(x, y):
    """Barva (r,g,b,a) v normalizovaných souřadnicích 0..1."""
    r = 0.2  # zaoblený čtverec
    cx, cy = min(max(x, r), 1 - r), min(max(y, r), 1 - r)
    if math.hypot(x - cx, y - cy) > r or not (0 <= x <= 1 and 0 <= y <= 1):
        return (0, 0, 0, 0)
    # obálka
    l, t, rr, b, w = 0.2, 0.3, 0.8, 0.72, 0.055
    on_border = (l - w / 2 <= x <= rr + w / 2 and t - w / 2 <= y <= b + w / 2) and not (l + w / 2 < x < rr - w / 2 and t + w / 2 < y < b - w / 2)
    on_flap = min(seg_dist(x, y, l, t, 0.5, 0.53), seg_dist(x, y, rr, t, 0.5, 0.53)) < w / 2
    return (*WHITE, 255) if on_border or on_flap else (*BLUE, 255)

def render(size, ss=4):
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    c = sample((px + (sx + .5) / ss) / size, (py + (sy + .5) / ss) / size)
                    for i in range(3): acc[i] += c[i] * c[3]
                    acc[3] += c[3]
            a = acc[3] / (ss * ss)
            row += bytes([int(acc[i] / acc[3]) if acc[3] else 0 for i in range(3)] + [int(a)])
        rows.append(bytes(row))
    return rows

def png(size):
    raw = b"".join(render(size))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")

sizes = [16, 24, 32, 48, 64, 128, 256]
images = [png(s) for s in sizes]
out = struct.pack("<HHH", 0, 1, len(sizes))
offset = 6 + 16 * len(sizes)
for s, data in zip(sizes, images):
    out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
    offset += len(data)
out += b"".join(images)
dest = Path(__file__).resolve().parent.parent / "assets" / "mailer.ico"
dest.write_bytes(out)
(dest.parent / "mailer-256.png").write_bytes(images[-1])
print("OK", dest, len(out), "B")

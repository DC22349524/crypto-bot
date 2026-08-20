#!/usr/bin/env python
"""
生成桌面快捷方式用的 .ico 图标（纯标准库，PNG-in-ICO，无需 PIL）。

输出：
  assets/交易.ico   深色 K 线 + 上升趋势线
  assets/日志.ico   终端风日志界面

跑法：C:\\Users\\<USER>\\freqtrade\\.venv\\Scripts\\python.exe make_icons.py
"""

import os
import struct
import zlib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSET_DIR = os.path.join(BASE_DIR, "assets")
SIZES = [256, 64, 32, 16]
SS = 4  # 超采样倍数，让圆角边缘平滑


def hexc(s, a=255):
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), a)


class Canvas:
    """按 [0,1] 归一化坐标绘制，内部超采样，最后降采样输出 RGBA。"""

    def __init__(self, size):
        self.n = size * SS
        self.ss = SS
        self.px = bytearray(self.n * self.n * 4)

    def _idx(self, x, y):
        return (y * self.n + x) * 4

    def blend(self, x, y, c):
        i = self._idx(x, y)
        sa = c[3] / 255.0
        da = self.px[i + 3] / 255.0
        oa = sa + da * (1 - sa)
        if oa <= 0:
            return
        for k in range(3):
            self.px[i + k] = round((c[k] * sa + self.px[i + k] * da * (1 - sa)) / oa)
        self.px[i + 3] = round(oa * 255)

    def grad_rect(self, x0, y0, x1, y1, ctop, cbot, r=0.0):
        """圆角矩形 + 纵向渐变背景。"""
        ix0, ix1 = int(x0 * self.n), int(x1 * self.n)
        iy0, iy1 = int(y0 * self.n), int(y1 * self.n)
        rr = int(r * self.n)
        for yy in range(iy0, iy1):
            t = (yy - iy0) / max(1, (iy1 - iy0 - 1))
            cr = round(ctop[0] + (cbot[0] - ctop[0]) * t)
            cg = round(ctop[1] + (cbot[1] - ctop[1]) * t)
            cb = round(ctop[2] + (cbot[2] - ctop[2]) * t)
            ca = round(ctop[3] + (cbot[3] - ctop[3]) * t)
            color = (cr, cg, cb, ca)
            for xx in range(ix0, ix1):
                dx = min(xx - ix0, ix1 - 1 - xx)
                dy = min(yy - iy0, iy1 - 1 - yy)
                if dx < rr and dy < rr:
                    cx = ix0 + rr if xx < ix0 + rr else ix1 - 1 - rr
                    cy = iy0 + rr if yy < iy0 + rr else iy1 - 1 - rr
                    ex, ey = xx - cx, yy - cy
                    if ex * ex + ey * ey > rr * rr:
                        continue
                self.blend(xx, yy, color)

    def rect(self, x0, y0, x1, y1, color, r=0.0):
        self.grad_rect(x0, y0, x1, y1, color, color, r)

    def vbar(self, x, y0, y1, w, color):
        """竖向矩形（蜡烛实体/影线）。"""
        ix = int(x * self.n)
        iw = max(1, int(w * self.n))
        iy0, iy1 = int(y0 * self.n), int(y1 * self.n)
        for yy in range(iy0, iy1):
            for xx in range(ix, ix + iw):
                if 0 <= xx < self.n:
                    self.blend(xx, yy, color)

    def hline(self, y, x0, x1, thick, color):
        iy = int(y * self.n)
        it = max(1, int(thick * self.n))
        ix0, ix1 = int(x0 * self.n), int(x1 * self.n)
        for yy in range(iy, iy + it):
            for xx in range(ix0, ix1):
                self.blend(xx, yy, color)

    def line(self, x0, y0, x1, y1, thick, color):
        """粗线（趋势线）。"""
        x0, y0 = x0 * self.n, y0 * self.n
        x1, y1 = x1 * self.n, y1 * self.n
        steps = max(1, int(abs(x1 - x0) + abs(y1 - y0)))
        rad = max(1, int(thick * self.n / 2))
        for i in range(steps + 1):
            t = i / steps
            cx = round(x0 + (x1 - x0) * t)
            cy = round(y0 + (y1 - y0) * t)
            for dy in range(-rad, rad + 1):
                for dx in range(-rad, rad + 1):
                    if dx * dx + dy * dy <= rad * rad:
                        xx, yy = cx + dx, cy + dy
                        if 0 <= xx < self.n and 0 <= yy < self.n:
                            self.blend(xx, yy, color)

    def render(self):
        s = self.ss
        n = self.n // s
        out = bytearray()
        for oy in range(n):
            for ox in range(n):
                r = g = b = 0.0
                a = 0.0
                for dy in range(s):
                    for dx in range(s):
                        i = self._idx(ox * s + dx, oy * s + dy)
                        aa = self.px[i + 3] / 255.0
                        r += self.px[i] * aa
                        g += self.px[i + 1] * aa
                        b += self.px[i + 2] * aa
                        a += aa
                cnt = s * s
                if a > 0:
                    r /= a
                    g /= a
                    b /= a
                ra = a / cnt
                out += bytes((round(r), round(g), round(b), round(ra * 255)))
        return bytes(out)


# ---------------------------------------------------------------- PNG / ICO 编码

def _chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def make_png(w, h, rgba):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            i = (y * w + x) * 4
            raw += rgba[i:i + 4]
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def make_ico(path, pngs):
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    body = b""
    for w, h, png in pngs:
        header += struct.pack(
            "<BBBBHHII",
            w if w < 256 else 0, h if h < 256 else 0, 0, 0, 1, 32, len(png), offset,
        )
        body += png
        offset += len(png)
    with open(path, "wb") as f:
        f.write(header + body)


# ---------------------------------------------------------------- 两个图标的绘制

def draw_trading(size):
    """K 线 + 上升趋势线。"""
    c = Canvas(size)
    c.grad_rect(0, 0, 1, 1, hexc("1c2f52"), hexc("0d1626"), r=0.18)
    if size >= 48:
        # 三根蜡烛
        c.vbar(0.295, 0.50, 0.74, 0.11, hexc("34d399"))   # 绿多
        c.vbar(0.295, 0.42, 0.86, 0.018, hexc("34d399"))  # 影线
        c.vbar(0.495, 0.36, 0.62, 0.11, hexc("fbbf24"))   # 金多
        c.vbar(0.495, 0.28, 0.72, 0.018, hexc("fbbf24"))
        c.vbar(0.695, 0.58, 0.80, 0.11, hexc("f87171"))   # 红空
        c.vbar(0.695, 0.50, 0.88, 0.018, hexc("f87171"))
        c.line(0.16, 0.88, 0.40, 0.68, 0.016, hexc("fcd34d"))
        c.line(0.40, 0.68, 0.72, 0.36, 0.016, hexc("fcd34d"))
    else:
        # 小尺寸简化：两根蜡烛
        c.vbar(0.38, 0.42, 0.68, 0.22, hexc("34d399"))
        c.vbar(0.38, 0.34, 0.80, 0.028, hexc("34d399"))
        c.vbar(0.66, 0.52, 0.76, 0.20, hexc("f87171"))
        c.vbar(0.66, 0.44, 0.86, 0.028, hexc("f87171"))
    return c.render()


def draw_logs(size):
    """终端风日志界面。"""
    c = Canvas(size)
    c.grad_rect(0, 0, 1, 1, hexc("161b22"), hexc("0d1117"), r=0.18)
    dot = size >= 48
    if dot:
        # 顶栏三个圆点
        for x, col in ((0.27, "ff5f57"), (0.35, "febc2e"), (0.43, "28c840")):
            c.rect(x - 0.022, 0.075, x + 0.022, 0.119, hexc(col), r=0.02)
        c.hline(0.155, 0.06, 0.94, 0.006, hexc("30363d"))
        rows = [(0.27, 0.80, "c9d1d9"), (0.40, 0.62, "8b949e"),
                (0.53, 0.50, "8b949e"), (0.66, 0.44, "3fb950")]
    else:
        rows = [(0.30, 0.72, "c9d1d9"), (0.50, 0.56, "3fb950"), (0.70, 0.50, "8b949e")]
    for i, (y, x1, col) in enumerate(rows):
        c.hline(y, 0.10, x1, 0.035, hexc(col))
        if i == 0:
            c.rect(0.10, y, 0.135, y + 0.035, hexc("3fb950"), r=0.005)  # 提示符
            c.hline(y, 0.155, x1, 0.035, hexc(col))
    return c.render()


def main():
    os.makedirs(ASSET_DIR, exist_ok=True)
    for name, draw in (("交易", draw_trading), ("日志", draw_logs)):
        entries = []
        for s in SIZES:
            rgba = draw(s)
            png = make_png(s, s, rgba)
            entries.append((s, s, png))
        path = os.path.join(ASSET_DIR, name + ".ico")
        make_ico(path, entries)
        print("生成", path, os.path.getsize(path), "bytes")


if __name__ == "__main__":
    main()

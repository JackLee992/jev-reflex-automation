#!/usr/bin/env python3
"""
canvas_grid.py —— 把「画布类」界面数字化成文本网格，喂给 Jev。

为什么需要它：无障碍树对 canvas/自绘 UI 只给一个节点。真机实测玩吧俄罗斯方块，
主棋盘就是 `content-desc="俄罗斯方块主棋盘"`、**子节点数 = 0**：没有任何格子信息。
Jev 不看图，所以在这种屏幕上它只能瞎猜（实测选键 conf=0.47，几乎是均匀分布）。

解决办法不是"让 Jev 看图"，而是**把像素结构化成文本**再问它 —— 这也是 TypeSafe 官方
游戏 demo 的做法。本模块把棋盘区域切成 R×C 网格，对每格取中心区域主色，输出：
  · 每格占用/颜色的字符矩阵（'.'=空，字母=颜色）
  · 每列高度、洞数、最大高度、平地程度等**已经算好的数值特征**
    （算数要留在代码里 —— Jev 明确不擅长计数/算术）

零依赖：自带最小 PNG 解码（zlib + 标准库），不依赖 PIL。
用法：
  python3 canvas_grid.py screen.png 51 402 741 1785 10 20
"""
from __future__ import annotations

import struct
import sys
import zlib


# --------------------------------------------------------- 最小 PNG 解码
def read_png(path: str):
    """返回 (width, height, getpixel(x,y)->(r,g,b))。支持 8-bit RGB/RGBA/灰度。"""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, w = 8, [], None
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, depth, color = struct.unpack(">IIBB", body[:10])
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break
        pos += 12 + ln
    if depth != 8:
        raise ValueError(f"unsupported bit depth {depth}")
    nch = {0: 1, 2: 3, 4: 2, 6: 4}[color]
    raw = zlib.decompress(b"".join(idat))
    stride = w * nch
    rows, prev = [], bytearray(stride)
    p = 0
    for _ in range(h):
        ft = raw[p]; p += 1
        line = bytearray(raw[p:p + stride]); p += stride
        if ft == 1:
            for i in range(nch, stride):
                line[i] = (line[i] + line[i - nch]) & 0xFF
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                c = prev[i - nch] if i >= nch else 0
                b = prev[i]
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        rows.append(bytes(line)); prev = line

    def get(x, y):
        r = rows[y]; i = x * nch
        if nch >= 3:
            return (r[i], r[i + 1], r[i + 2])
        v = r[i]
        return (v, v, v)

    return w, h, get


# --------------------------------------------------------- 颜色 → 符号
def classify(rgb, bg=None, bg_thresh=48):
    """把格子主色映射成单字符。

    真机踩坑（两个都触发过）：
    1. 不能假设"棋盘底色是深色"。玩吧 AI 对战是深色底（空格≈(23,32,47)），经典模式
       却是**浅色底**（空格≈(254,239,244) 淡粉白）。写死"暗=空"会把浅色棋盘读成全满。
    2. 浅色主题下相邻空格之间有几个灰度点的渐变/阴影，只按"与 bg 的距离"判会把它们
       当成灰色方块（G），进而让 heights 把空列算成满列。所以**先判是否接近中性灰白**
       ——高亮度且低饱和的一律当空，真方块都是高饱和的。
    """
    r, g, b = rgb
    mx, mn = max(rgb), min(rgb)
    sat = mx - mn
    # 高亮度 + 低饱和 = 浅色主题的空格/阴影/网格线，绝不是方块
    if mx > 200 and sat < 45:
        return "."
    if bg is not None:
        if abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2]) <= 60:
            return "."
    elif mx < bg_thresh:
        return "."
    if sat < 28:                             # 低饱和 = 灰（干扰行），必须够暗才算
        return "G" if 90 < mx <= 200 else "."
    if mx == r:
        h = (60 * (g - b) / sat) % 360
    elif mx == g:
        h = 60 * (2 + (b - r) / sat)
    else:
        h = 60 * (4 + (r - g) / sat)
    for lo, hi, ch in ((345, 360, "R"), (0, 18, "R"), (18, 45, "O"), (45, 70, "Y"),
                       (70, 165, "E"), (165, 200, "C"), (200, 255, "B"),
                       (255, 290, "P"), (290, 345, "M")):
        if lo <= h < hi:
            return ch
    return "?"


def detect_bg(get, box, cols, rows_n):
    """取棋盘最上面两行的众数颜色作为"空格"基准 —— 顶部几乎总是空的。"""
    x1, y1, x2, y2 = box
    cw, ch = (x2 - x1) / cols, (y2 - y1) / rows_n
    tally = {}
    for r in range(2):
        for c in range(cols):
            px = get(int(x1 + c * cw + cw / 2), int(y1 + r * ch + ch / 2))
            tally[px] = tally.get(px, 0) + 1
    return max(tally, key=tally.get)


def detect_hud_panels(get, box, cols, rows_n, bg):
    """找出**画在 canvas 内部、a11y 看不见**的 HUD 面板（如「下一块」预览框）。

    真机踩坑：玩吧经典模式把预览面板画进 canvas，a11y 树里只有 wb-canvas 一个节点，
    所以靠 a11y 拿 HUD bounds 拿不到，预览方块被当成盘面方块（heights[9] 恒为 18）。
    这类面板的底色与棋盘底色**明显不同**（奶白 vs 粉），据此把整格判为 HUD。
    返回需要置空的 (row, col) 集合。
    """
    x1, y1, x2, y2 = box
    cw, ch = (x2 - x1) / cols, (y2 - y1) / rows_n
    out = set()
    for r in range(rows_n):
        for c in range(cols):
            px = get(int(x1 + c * cw + cw / 2), int(y1 + r * ch + ch / 2))
            # 面板底色：亮、低饱和，但与棋盘底色差得明显
            if (min(px) > 235 and max(px) - min(px) < 22
                    and abs(px[0] - bg[0]) + abs(px[1] - bg[1]) + abs(px[2] - bg[2]) > 12):
                out.add((r, c))
    return out


def cell_color(get, x0, y0, cw, ch, bg=None, inset=0.30):
    """取格子中心区域的众数颜色，避开网格线与圆角。"""
    step_x = max(1, int(cw * 0.12))
    step_y = max(1, int(ch * 0.12))
    xs = range(int(x0 + cw * inset), max(int(x0 + cw * (1 - inset)),
                                         int(x0 + cw * inset) + 1), step_x)
    ys = range(int(y0 + ch * inset), max(int(y0 + ch * (1 - inset)),
                                         int(y0 + ch * inset) + 1), step_y)
    tally = {}
    for x in xs:
        for y in ys:
            k = classify(get(x, y), bg)
            tally[k] = tally.get(k, 0) + 1
    if not tally:
        return "."
    filled = {k: v for k, v in tally.items() if k != "."}
    total = sum(tally.values())
    # 要求**多数**采样点同色才算占用。早期用 25% 阈值，结果棋盘边缘被相邻 UI 蹭到的
    # 半格（如 r2c9 只有 2/9 个点是色块）被误判成方块，heights 随之失真。
    if filled:
        top = max(filled, key=filled.get)
        if filled[top] >= total * 0.55:
            return top
    return "."


def digitize(png, box, cols, rows_n, mask_boxes=()):
    """把 box=[x1,y1,x2,y2] 区域切成 cols×rows_n 网格，返回字符矩阵。

    mask_boxes: 需要抹掉的 HUD 区域（屏幕绝对坐标）。真机踩坑：玩吧经典模式把
    「下一块」预览面板**画在棋盘右上角之上**，数字化会把预览方块当成盘面上的方块
    （实测 r2c9 常驻一个假 'R'，让 heights[9]=18）。这类 HUD 的 bounds 能从 a11y
    树拿到，传进来直接置空即可 —— 又一次说明图像与 a11y 要配合，不能二选一。
    """
    _, _, get = read_png(png)
    bg = detect_bg(get, box, cols, rows_n)
    x1, y1, x2, y2 = box
    cw, ch = (x2 - x1) / cols, (y2 - y1) / rows_n
    hud_cells = detect_hud_panels(get, box, cols, rows_n, bg)

    def masked(r, c):
        if (r, c) in hud_cells:
            return True
        cx, cy = x1 + c * cw + cw / 2, y1 + r * ch + ch / 2
        return any(mx1 <= cx <= mx2 and my1 <= cy <= my2
                   for mx1, my1, mx2, my2 in mask_boxes)

    return ["".join("." if masked(r, c)
                    else cell_color(get, x1 + c * cw, y1 + r * ch, cw, ch, bg)
                    for c in range(cols)) for r in range(rows_n)]


# --------------------------------------------------------- 数值特征（代码算，不问模型）
def features(grid):
    rows_n, cols = len(grid), len(grid[0])
    heights, holes = [], 0
    for c in range(cols):
        top = None
        for r in range(rows_n):
            if grid[r][c] != ".":
                top = r; break
        heights.append(0 if top is None else rows_n - top)
        if top is not None:
            holes += sum(1 for r in range(top + 1, rows_n) if grid[r][c] == ".")
    full = sum(1 for r in grid if all(ch != "." for ch in r))
    bump = sum(abs(heights[i] - heights[i + 1]) for i in range(cols - 1))
    return {"heights": heights, "max_height": max(heights), "holes": holes,
            "full_rows": full, "bumpiness": bump,
            "lowest_cols": [i + 1 for i, h in enumerate(heights) if h == min(heights)],
            "board_rows": rows_n, "board_cols": cols}


def render(grid):
    """带列号的可读棋盘，供 Jev 的 state 使用（行号从上到下）。"""
    cols = len(grid[0])
    head = "   " + "".join(str((i + 1) % 10) for i in range(cols))
    return "\n".join([head] + [f"{r:2d} {row}" for r, row in enumerate(grid)])


if __name__ == "__main__":
    png = sys.argv[1]
    box = [int(v) for v in sys.argv[2:6]]
    cols, rows_n = int(sys.argv[6]), int(sys.argv[7])
    g = digitize(png, box, cols, rows_n)
    print(render(g))
    import json
    print(json.dumps(features(g), ensure_ascii=False))

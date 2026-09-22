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

try:                                    # 可选加速；没有也能跑（保持零依赖承诺）
    import numpy as _np
except ImportError:
    _np = None

try:
    from PIL import Image as _Image      # 有 PIL 就用 C 解码器，快一个数量级
except Exception:
    _Image = None


# --------------------------------------------------------- 最小 PNG 解码
def read_png(path: str, rows_needed=None):
    """返回 (width, height, getpixel(x,y)->(r,g,b))。支持 8-bit RGB/RGBA/灰度。

    优先用 PIL 的 C 解码器；装不了 PIL 时回退到下面的纯标准库实现，
    功能完全一致，只是慢（真机实测：PIL ~30ms vs 纯 Python ~450ms）。
    保留纯 Python 路径是刻意的——这个项目的卖点之一就是能在任何机器上直接跑。
    """
    if _Image is not None and _np is not None:
        with _Image.open(path) as im:
            arr = _np.asarray(im.convert("RGB"))
        h, w = arr.shape[:2]

        def get(x, y):
            px = arr[y, x]
            return (int(px[0]), int(px[1]), int(px[2]))

        return w, h, get
    return _read_png_pure(path, rows_needed)


def _read_png_pure(path: str, rows_needed=None):
    """纯标准库 PNG 解码（zlib + defilter），无第三方依赖。

    性能关键（真机实测）：defilter 必须**逐行顺序**做（每行依赖上一行），
    但我们只采样 ~200 个像素点。传 rows_needed={需要的 y 集合} 时只保留那些行，
    省掉为 2400 行各存一份 4320B 副本的开销。
    """
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
    last = max(rows_needed) if rows_needed else h - 1
    keep = set(rows_needed) if rows_needed else None
    rows: dict[int, bytes] = {}
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        if y > last:
            break
        ft = raw[p]; p += 1
        line = bytearray(raw[p:p + stride]); p += stride
        # filter 0/2 不依赖左邻像素，可整行向量化；1/3/4 必须逐字节。
        # 安卓 screencap 的 PNG 绝大多数行是 filter 0 或 2，所以这条快路径很值。
        if ft == 0:
            pass
        elif ft == 2:
            # Up 滤波：整行与上一行逐字节相加，没有行内依赖 → numpy 可整行向量化。
            # 实测这类行占 1210/2400，是最大的一块可优化量。
            if _np is not None:
                line = bytearray(
                    (_np.frombuffer(bytes(line), dtype=_np.uint8)
                     + _np.frombuffer(bytes(prev), dtype=_np.uint8)).tobytes())
            else:
                line = bytearray((line[i] + prev[i]) & 0xFF for i in range(stride))
        elif ft == 1:
            for i in range(nch, stride):
                line[i] = (line[i] + line[i - nch]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                a = line[i - nch] if i >= nch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            # Paeth：每字节依赖左邻（同行、已解码），所以无法整行向量化。
            # profile 显示这是 read_png 的大头（1121 行 × 4320 字节 × 3 次 abs
            # = 3900 万次 abs 调用）。用局部变量 + 算术取绝对值替掉 abs()。
            up = prev
            for i in range(nch):
                line[i] = (line[i] + up[i]) & 0xFF
            for i in range(nch, stride):
                a = line[i - nch]
                b = up[i]
                c = up[i - nch]
                pa = b - c
                pb = a - c
                pc = pa + pb
                if pa < 0:
                    pa = -pa
                if pb < 0:
                    pb = -pb
                if pc < 0:
                    pc = -pc
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = c
                line[i] = (line[i] + pr) & 0xFF
        if keep is None or y in keep:
            rows[y] = bytes(line)
        prev = line

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

    真机踩坑（两轮才修对）：玩吧经典模式把预览面板画进 canvas，a11y 树里只有
    wb-canvas 一个节点，拿不到 HUD bounds。
    第一版只认"面板底色"，但预览里的**方块本身是高饱和色块**（实测 r2c9 是
    (239,143,122) 的鲑鱼色），照样被当成盘面方块，heights 恒为 18。
    现在改为**面板矩形扩张**：先找底色异常的格子当种子，再把种子所在的
    行列范围整体圈成矩形——面板是连续区域，其中的彩色预览块自然一并被罩住。
    返回需要置空的 (row, col) 集合。
    """
    x1, y1, x2, y2 = box
    cw, ch = (x2 - x1) / cols, (y2 - y1) / rows_n
    seeds = set()
    for r in range(rows_n):
        for c in range(cols):
            px = get(int(x1 + c * cw + cw / 2), int(y1 + r * ch + ch / 2))
            if (min(px) > 235 and max(px) - min(px) < 22
                    and abs(px[0] - bg[0]) + abs(px[1] - bg[1]) + abs(px[2] - bg[2]) > 12):
                seeds.add((r, c))
    if not seeds:
        return set()
    # 面板通常贴边（顶部/角落）。把种子聚成一个矩形块，连同其中的彩色预览一起罩住。
    rs = {r for r, _ in seeds}
    cs = {c for _, c in seeds}
    r_lo, r_hi = min(rs), max(rs)
    c_lo, c_hi = min(cs), max(cs)
    # 只在面板确实是小块（不超过棋盘 1/3）时才启用，避免整盘被误罩
    if (r_hi - r_lo + 1) * (c_hi - c_lo + 1) > rows_n * cols // 3:
        return seeds
    return {(r, c) for r in range(r_lo, r_hi + 1) for c in range(c_lo, c_hi + 1)}


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
    # 只保留会被采样到的扫描行。cell_color 在每格 inset 0.30~0.70 区间按
    # 12% 步长取点，这里把那些 y 全算出来，defilter 仍要顺序跑（PNG 规范如此），
    # 但不再为 2400 行各存一份 4320B 的副本 —— 内存和拷贝开销都省掉。
    x1, y1, x2, y2 = box
    cw, ch = (x2 - x1) / cols, (y2 - y1) / rows_n
    step_y = max(1, int(ch * 0.12))
    wanted = {int(y1 + r * ch + ch / 2) for r in range(rows_n)}      # detect_bg / HUD 用
    for r in range(rows_n):
        y0 = y1 + r * ch
        lo, hi = int(y0 + ch * 0.30), max(int(y0 + ch * 0.70), int(y0 + ch * 0.30) + 1)
        wanted.update(range(lo, hi, step_y))
    _, _, get = read_png(png, rows_needed=wanted)
    bg = detect_bg(get, box, cols, rows_n)
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

#!/usr/bin/env python3
"""
tetris_model.py —— 俄罗斯方块的**纯代码**领域模型：七种方块、旋转、落点模拟、启发式打分。

为什么单独一个文件：TypeSafe 官方的核心原则是
  「把算术、枚举、模拟留在代码里；只把"哪个更好"交给模型」。
之前 play_tetris 用"宽度近似形态"猜旋转，既不准也违背这个原则。这里做真实建模：
识别是哪种方块 → 枚举 (形态 × 列) 的所有合法落点 → 精确模拟落地后的盘面 →
算出行业标准四特征（Dellacherie 风格）。Jev 只在打完分的候选里挑一个。

零依赖，可单独跑自检：  python3 tetris_model.py
"""
from __future__ import annotations

# 七种方块的四个朝向，(row, col) 相对坐标，已归一到左上角
SHAPES: dict[str, list[list[tuple[int, int]]]] = {
    "I": [[(0, 0), (0, 1), (0, 2), (0, 3)], [(0, 0), (1, 0), (2, 0), (3, 0)]],
    "O": [[(0, 0), (0, 1), (1, 0), (1, 1)]],
    "T": [[(0, 1), (1, 0), (1, 1), (1, 2)], [(0, 0), (1, 0), (1, 1), (2, 0)],
          [(0, 0), (0, 1), (0, 2), (1, 1)], [(0, 1), (1, 0), (1, 1), (2, 1)]],
    "S": [[(0, 1), (0, 2), (1, 0), (1, 1)], [(0, 0), (1, 0), (1, 1), (2, 1)]],
    "Z": [[(0, 0), (0, 1), (1, 1), (1, 2)], [(0, 1), (1, 0), (1, 1), (2, 0)]],
    "J": [[(0, 0), (1, 0), (1, 1), (1, 2)], [(0, 0), (0, 1), (1, 0), (2, 0)],
          [(0, 0), (0, 1), (0, 2), (1, 2)], [(0, 1), (1, 1), (2, 0), (2, 1)]],
    "L": [[(0, 2), (1, 0), (1, 1), (1, 2)], [(0, 0), (1, 0), (2, 0), (2, 1)],
          [(0, 0), (0, 1), (0, 2), (1, 0)], [(0, 0), (0, 1), (1, 1), (2, 1)]],
}


def _norm(cells):
    r0 = min(r for r, _ in cells)
    c0 = min(c for _, c in cells)
    return tuple(sorted((r - r0, c - c0) for r, c in cells))


_LOOKUP = {_norm(o): (k, i) for k, os_ in SHAPES.items() for i, o in enumerate(os_)}


def identify(cells) -> tuple[str | None, int]:
    """从盘面上抠出的格子认出是哪种方块、当前第几个朝向。认不出返回 (None, 0)。"""
    return _LOOKUP.get(_norm(cells), (None, 0))


def landing_row(heights, cells, col, rows_n):
    """给定列偏移，模拟方块自由下落的停靠行；越界或放不下返回 None。"""
    cols = len(heights)
    width = max(c for _, c in cells) + 1
    if col < 0 or col + width > cols:
        return None
    # 每一列方块最低格的相对行
    bottom = {}
    for r, c in cells:
        bottom[c] = max(bottom.get(c, -1), r)
    # 找最小下落距离：使任一列刚好压在该列现有高度上
    drop = min(rows_n - heights[col + c] - 1 - br for c, br in bottom.items())
    if drop < 0:
        return None
    return drop


def simulate(grid, heights, cells, col, rows_n):
    """把方块放到 col 后的新盘面。返回 (新grid, 消行数) 或 None。"""
    d = landing_row(heights, cells, col, rows_n)
    if d is None:
        return None
    g = [list(row) for row in grid]
    for r, c in cells:
        rr, cc = r + d, col + c
        if rr < 0 or rr >= rows_n:
            return None
        g[rr][cc] = "X"
    kept = [row for row in g if not all(ch != "." for ch in row)]
    cleared = rows_n - len(kept)
    empty = ["." * len(grid[0])] * cleared
    return ["".join(r) for r in (empty + kept)], cleared


def board_features(grid):
    """Dellacherie 风格特征。全部在代码里算 —— 不要让模型数格子。"""
    rows_n, cols = len(grid), len(grid[0])
    heights, holes = [], 0
    for c in range(cols):
        top = next((r for r in range(rows_n) if grid[r][c] != "."), None)
        heights.append(0 if top is None else rows_n - top)
        if top is not None:
            holes += sum(1 for r in range(top + 1, rows_n) if grid[r][c] == ".")
    wells = 0
    for c in range(cols):
        left = heights[c - 1] if c else rows_n
        right = heights[c + 1] if c + 1 < cols else rows_n
        wells += max(0, min(left, right) - heights[c])
    return {
        "heights": heights,
        "max_height": max(heights),
        "agg_height": sum(heights),
        "holes": holes,
        "bumpiness": sum(abs(heights[i] - heights[i + 1]) for i in range(cols - 1)),
        "wells": wells,
    }


def score(f, cleared):
    """经典线性启发式（权重来自公开的 Tetris AI 文献，代码里可调、可审）。"""
    return (0.76 * cleared - 0.51 * f["agg_height"]
            - 0.36 * f["holes"] - 0.18 * f["bumpiness"] - 0.10 * f["wells"])


def candidates(grid, piece_cells, rows_n, top_k=5):
    """枚举所有 (朝向 × 列) 落点，模拟 + 打分，返回 Top-K。

    这是"官方水准"的关键：模型只在**已经算好分的少数候选**里做选择，
    而不是让它去想象棋盘。
    """
    kind, _ = identify(piece_cells)
    orients = SHAPES[kind] if kind else [list(_norm(piece_cells))]
    base = board_features(grid)
    cols = len(grid[0])
    out = []
    for oi, cells in enumerate(orients):
        cells = list(cells)
        width = max(c for _, c in cells) + 1
        for col in range(cols - width + 1):
            sim = simulate(grid, base["heights"], cells, col, rows_n)
            if sim is None:
                continue
            g2, cleared = sim
            f2 = board_features(g2)
            out.append({
                "kind": kind or "?", "orient": oi, "col": col + 1, "width": width,
                "cleared": cleared, "holes_after": f2["holes"],
                "new_holes": max(0, f2["holes"] - base["holes"]),
                "max_height_after": f2["max_height"],
                "bumpiness_after": f2["bumpiness"],
                "score": round(score(f2, cleared), 2),
            })
    out.sort(key=lambda m: -m["score"])
    # 去掉"分数相同且形态等价"的重复项，避免劈分概率
    seen, uniq = set(), []
    for m in out:
        k = (m["col"], m["width"], m["cleared"], m["holes_after"], m["max_height_after"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(m)
    return uniq[:top_k], base


if __name__ == "__main__":
    # 自检：七种方块都能被认出；落点模拟不越界；消行能被正确识别
    ok = True
    for k, os_ in SHAPES.items():
        for i, o in enumerate(os_):
            got = identify(list(o))
            if got != (k, i):
                print(f"FAIL identify {k}/{i} -> {got}"); ok = False
    empty = ["." * 10 for _ in range(20)]
    cands, _ = candidates(empty, SHAPES["I"][0], 20)
    assert cands and all(c["new_holes"] == 0 for c in cands), "空盘放 I 不该产生洞"
    # 一行差一格 -> 放 I 竖着填不满，放对位置的单格才消行
    near = ["." * 10 for _ in range(19)] + ["XXXXXXXXX."]
    c2, _ = candidates(near, SHAPES["I"][1], 20)   # 竖 I
    best = c2[0]
    print(f"identify 自检: {'PASS' if ok else 'FAIL'}")
    print(f"空盘 I 横放 Top1: col={cands[0]['col']} score={cands[0]['score']} holes={cands[0]['new_holes']}")
    print(f"缺口盘 竖I Top1: col={best['col']} cleared={best['cleared']} score={best['score']}")
    print("PASS" if ok and best["col"] == 10 else "CHECK: 竖 I 应优先填第 10 列缺口")

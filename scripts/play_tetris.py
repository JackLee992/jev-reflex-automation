#!/usr/bin/env python3
"""
play_tetris.py —— 画布类游戏的高频闭环：数字化棋盘 → Jev 决策 → adb 执行。

这是"canvas 场景怎么办"的完整答案。关键三点（都经真机验证）：

1. **把画布变成文本**。a11y 树对棋盘只有一个空节点（子节点=0），Jev 不看图，
   所以必须先 canvas_grid.digitize() 把像素切成 10x20 字符矩阵。
   实测：不给棋盘时选列 conf=0.40 且选错；给棋盘后 conf=0.70 且选中唯一最低列。

2. **算术留在代码里**。列高/洞数/凹凸度用 Python 算好再塞进 state。
   Jev 官方明确不擅长计数与算术 —— 让它做"哪个更好"的选择，不做"有几个"的计算。

3. **高频靠候选枚举，不靠逐帧问**。每帧都问一次太慢（截图+判断 ~1.5s）。
   真正的做法是：代码枚举所有 (列, 旋转) 落点并用启发式打分，只把 **Top-K 候选**
   交给 Jev 做最终选择。算分是代码的活，选哪个是 Jev 的活。

用法：
  python3 play_tetris.py --moves 12          # 真跑
  python3 play_tetris.py --moves 3 --dry-run # 只决策不点
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from canvas_grid import digitize, features, render        # noqa: E402
from jev_reflex import ask                                 # noqa: E402

ADB = os.environ.get("ADB", os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"))
DEV = os.environ.get("ANDROID_SERIAL", "")


def adb(*a, binary=False):
    pre = [ADB] + (["-s", DEV] if DEV else [])
    r = subprocess.run(pre + list(a), capture_output=True)
    return r.stdout if binary else r.stdout.decode("utf-8", "ignore")


def dump_ui(tmp="/tmp/_tetris_ui.xml"):
    adb("shell", "uiautomator", "dump", "--compressed", "/sdcard/_t.xml")
    x = adb("exec-out", "cat", "/sdcard/_t.xml")
    open(tmp, "w", encoding="utf-8").write(x)
    return x


def find_board_and_buttons(xml):
    """从 a11y 树拿棋盘 bounds、控制键坐标、以及**压在棋盘上的 HUD** bounds。

    真机踩坑：
    1. 同一 app 不同模式控件名不同（AI 对战叫「旋转/直接落下」，经典模式叫「变换/加速」
       且 id 为 wb-tetris-*）→ 用别名表匹配，不写死。
    2. 经典模式把「下一块」预览面板画在棋盘右上角**之上**，数字化会把预览方块当成盘面
       方块 → 把落在棋盘内的文字/面板节点当作 HUD 遮罩返回，交给 digitize 置空。
    """
    ALIAS = {
        "left": ("左移",), "right": ("右移",), "rotate": ("旋转", "变换", "旋转方块"),
        "soft": ("软降", "加速"), "hard": ("直接落下", "硬降"), "hold": ("暂存",),
    }
    board, btns, nodes = None, {}, []
    for m in re.finditer(r"<node[^>]*>", xml):
        s = m.group(0)
        d = re.search(r'content-desc="([^"]*)"', s)
        t = re.search(r'\btext="([^"]*)"', s)
        rid = re.search(r'resource-id="([^"]*)"', s)
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', s)
        if not b:
            continue
        name = (d.group(1) if d and d.group(1) else (t.group(1) if t else "")).strip()
        rid_s = (rid.group(1) if rid else "").split("/")[-1]
        box = [int(v) for v in b.groups()]
        nodes.append((name, rid_s, box))
        if ("主棋盘" in name or "main board" in name.lower()
                or rid_s in ("wb-canvas",) or rid_s.endswith("-canvas")
                or rid_s.endswith("-board")):
            if board is None or (box[2] - box[0]) * (box[3] - box[1]) > \
                    (board[2] - board[0]) * (board[3] - board[1]):
                board = box
            continue
        if box[2] - box[0] <= 2 or box[3] - box[1] <= 2:
            continue
        for key, names in ALIAS.items():
            if name in names or rid_s.endswith(f"tetris-{key}") or rid_s.endswith(f"-{key}"):
                btns.setdefault(key, [(box[0] + box[2]) // 2, (box[1] + box[3]) // 2])

    # 落在棋盘矩形内、且不是棋盘本身的具名节点 = 压在棋盘上的 HUD
    hud = []
    if board:
        bx1, by1, bx2, by2 = board
        for name, rid_s, box in nodes:
            if box == board or not name:
                continue
            if box[0] >= bx1 - 4 and box[1] >= by1 - 4 and box[2] <= bx2 + 4 \
                    and box[3] <= by2 + 4:
                # 面板类 HUD 通常带文字标签（下一块/消除/分数）
                hud.append(box)
    return board, btns, hud


def screen_png(path="/tmp/_tetris.png"):
    open(path, "wb").write(adb("exec-out", "screencap", "-p", binary=True))
    return path


def piece_cells(grid):
    """悬空的连通块 = 正在下落的方块。返回 (cells, 所占列范围)。"""
    rows_n, cols = len(grid), len(grid[0])
    # 找最上面的非空行簇；下落块与堆叠之间通常有空行
    filled = [(r, c) for r in range(rows_n) for c in range(cols) if grid[r][c] != "."]
    if not filled:
        return [], (0, 0)
    tops = {}
    for r, c in filled:
        tops.setdefault(c, r)
    # 堆叠轮廓：从底部往上连续的部分；悬空块：其下方有空格
    piece = [(r, c) for r, c in filled
             if any(grid[rr][c] == "." for rr in range(r + 1, rows_n))]
    if not piece:
        return [], (0, 0)
    # 只保留最上面那一簇（下落块）
    minr = min(r for r, _ in piece)
    piece = [(r, c) for r, c in piece if r <= minr + 3]
    cs = [c for _, c in piece]
    return piece, (min(cs) + 1, max(cs) + 1)


def enumerate_moves(f, piece_span, rotations=(0, 1, 2, 3)):
    """代码枚举候选并算启发式分 —— 算术不交给模型。

    每个候选 = (目标列, 旋转次数)。旋转会改变方块占用宽度，所以宽度也要枚举：
    真机教训——只做「横移+落下」不转向，长条/T 形塞不进空位，很快堆死。
    这里用宽度近似代表旋转形态（不解析具体七种形状，够用且不依赖 app 内部）。
    """
    heights, cols = f["heights"], f["board_cols"]
    base_w = piece_span[1] - piece_span[0] + 1
    out = []
    seen = set()
    for rot in rotations:
        # 横放宽度 base_w，竖放宽度 1；旋转 1/3 视为换向
        width = base_w if rot % 2 == 0 else max(1, 4 - base_w + 1) if base_w > 1 else 2
        width = max(1, min(width, cols))
        for target in range(1, cols - width + 2):
            key = (target, width)
            if key in seen:
                continue
            seen.add(key)
            seg = heights[target - 1:target - 1 + width]
            landing = max(seg)
            newh = [landing + 1] * width
            created_holes = sum(landing - h for h in seg)
            after = heights[:target - 1] + newh + heights[target - 1 + width:]
            bump = sum(abs(after[i] - after[i + 1]) for i in range(len(after) - 1))
            out.append({"col": target, "rot": rot, "width": width,
                        "landing_height": landing, "new_holes": created_holes,
                        "bumpiness_after": bump, "max_height_after": max(after)})
    out.sort(key=lambda m: (m["new_holes"], m["max_height_after"], m["bumpiness_after"]))
    return out


def choose(grid, f, cands):
    """Top-K 候选交给 Jev 选 —— 这是它擅长的"哪个更好"，不是"算几个"。"""
    crit = {f"{m['col']}|{m['rot']}":
            (f"place piece in column {m['col']} after rotating {m['rot']} time(s) "
             f"(occupies {m['width']} column(s)): lands at height {m['landing_height']}, "
             f"creates {m['new_holes']} new holes, stack max height becomes "
             f"{m['max_height_after']}, surface bumpiness {m['bumpiness_after']}")
            for m in cands}
    q = {"move": {"type": "choice", "instructions": (
        "Tetris strategy. `board` shows the well ('.'=empty, letters=filled, row 0 is the TOP). "
        "Choose where to drop the current piece. Prefer, in order: creating ZERO new holes, "
        "keeping the stack LOW, and keeping the surface FLAT. Never pick an option that makes "
        "the stack dangerously tall."), "criteria": crit}}
    a = ask({"board": render(grid), "features": f,
             "goal": "Survive as long as possible and clear lines"}, q)["answers"]["move"]
    col, rot = a["choice"].split("|")
    return int(col), int(rot), a["confidence"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--moves", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    xml = dump_ui()
    board, btns, hud = find_board_and_buttons(xml)
    if not board or "left" not in btns:
        print("未找到棋盘或控制键；请确认游戏正在进行中。")
        print("board=", board, "buttons=", btns)
        return 2
    drop = "hard" if "hard" in btns else "soft"
    print(f"棋盘 bounds={board}  控制键={ {k: v for k, v in btns.items()} }")
    print(f"落子方式：{'硬降(直接落下)' if drop == 'hard' else '软降(加速)，无硬降键→多按几次'}")

    cols, rows_n = 10, 20
    for i in range(1, a.moves + 1):
        # 每帧先确认棋盘没被遮挡 —— 关键：canvas 数字化对"上面盖着弹窗"毫无感知，
        # 真机踩过：游戏结束遮罩盖住棋盘后，digitize 仍在读被虚化的残影，
        # 产出"方块悬空 + holes=48"的垃圾数据，而循环还在照着它点。
        # a11y 树能看到遮罩节点，所以用它当哨兵，图像只负责已确认可见的棋盘。
        xml_now = dump_ui()
        mask = re.search(r'resource-id="[^"]*(gameover|progress-mask|popup-mask)[^"]*"', xml_now)
        if mask or "游戏结束" in xml_now:
            print(f"[{i}] 检测到遮罩/游戏结束（{mask.group(0) if mask else '游戏结束'}），停止。")
            print("     → canvas 数字化必须配 a11y 哨兵，否则会读到弹窗后面的残影。")
            break
        # 棋盘几何每帧重取：模式切换/返回菜单后 canvas 会从 DOM 消失或换位置，
        # 沿用上一帧缓存的 bounds 会对着空白区域采样，产出全是噪声的"棋盘"。
        board_now, btns_now, hud = find_board_and_buttons(xml_now)
        if not board_now or "left" not in btns_now:
            print(f"[{i}] 棋盘/控制键已消失（可能回到菜单或切了模式），停止。")
            break
        board, btns = board_now, btns_now
        drop = "hard" if "hard" in btns else "soft"

        grid = digitize(screen_png(), board, cols, rows_n, hud)
        f = features(grid)
        piece, span = piece_cells(grid)
        if not piece:
            time.sleep(0.2); continue
        cands = enumerate_moves(f, span)[:6]          # 只给 Top-6
        col, rot, conf = choose(grid, f, cands)
        cur = span[0]
        print(f"[{i}] 方块列{span[0]}-{span[1]} 高度{f['heights']} 洞{f['holes']} "
              f"→ 目标列 {col} 旋转{rot} (conf={conf:.2f})")
        if a.dry_run:
            continue
        # 先转向，再横移，最后落下
        for _ in range(rot):
            if "rotate" in btns:
                x, y = btns["rotate"]
                adb("shell", "input", "tap", str(x), str(y))
                time.sleep(0.05)
        # 旋转会改变方块位置，重新观测一次当前列，避免按旧坐标横移
        grid2 = digitize(screen_png(), board, cols, rows_n, hud)
        p2, span2 = piece_cells(grid2)
        if p2:
            cur = span2[0]
        steps = col - cur
        key = "right" if steps > 0 else "left"
        for _ in range(abs(steps)):
            x, y = btns[key]
            adb("shell", "input", "tap", str(x), str(y))
            time.sleep(0.05)
        x, y = btns[drop]
        # 无硬降键时，连点软降把方块压到底
        for _ in range(1 if drop == "hard" else 18):
            adb("shell", "input", "tap", str(x), str(y))
            if drop == "soft":
                time.sleep(0.02)
        time.sleep(0.3)

    grid = digitize(screen_png(), board, 10, 20, hud)
    print("\n最终棋盘:"); print(render(grid))
    print(json.dumps(features(grid), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

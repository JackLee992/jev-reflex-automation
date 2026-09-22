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
from tetris_model import SHAPES, candidates, identify      # noqa: E402

ADB = os.environ.get("ADB", os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"))
DEV = os.environ.get("ANDROID_SERIAL", "")


def adb(*a) -> str:
    """跑一条 adb 命令，返回解码后的 stdout。二进制用 adb_bytes。"""
    return adb_bytes(*a).decode("utf-8", "ignore")


def adb_bytes(*a) -> bytes:
    pre = [ADB] + (["-s", DEV] if DEV else [])
    return subprocess.run(pre + list(a), capture_output=True).stdout


def dump_ui(tmp="/tmp/_tetris_ui.xml") -> str:
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
    open(path, "wb").write(adb_bytes("exec-out", "screencap", "-p"))
    return path


def piece_cells(grid):
    """找出正在下落的方块 = 最上面那个 4 格连通块。

    真机踩坑两次：
    1. 旧实现取"下方有空格的所有格子"，会把整个堆叠上沿也算进去。
    2. HUD（画在棋盘内的「下一块」预览）会形成一个孤立小块，位置比真方块更靠上，
       于是被当成下落方块（实测抓到 [(2,8)] 这种单格）。
    现在按 4-连通分量切分，只接受**恰好 4 格**的连通块（俄罗斯方块必然 4 格），
    并在同样合法的块里取最靠上的那个。
    """
    rows_n, cols = len(grid), len(grid[0])
    seen, comps = set(), []
    for r in range(rows_n):
        for c in range(cols):
            if grid[r][c] == "." or (r, c) in seen:
                continue
            stack, comp = [(r, c)], []
            seen.add((r, c))
            while stack:
                y, x = stack.pop()
                comp.append((y, x))
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if (0 <= ny < rows_n and 0 <= nx < cols
                            and (ny, nx) not in seen and grid[ny][nx] != "."):
                        seen.add((ny, nx))
                        stack.append((ny, nx))
            comps.append(comp)
    # 只要 4 格的连通块（俄罗斯方块必然 4 格），优先悬空的，再取最靠上的
    cands = []
    for comp in comps:
        if len(comp) != 4:
            continue
        cells = set(comp)
        floor = {}                                  # 每列最低格
        for r, c in comp:
            floor[c] = max(floor.get(c, -1), r)
        airborne = all(r + 1 >= rows_n or grid[r + 1][c] == "." or (r + 1, c) in cells
                       for c, r in floor.items())
        cands.append((not airborne, min(r for r, _ in comp), comp))
    if not cands:
        return [], (0, 0)
    comp = min(cands)[2]
    cs = [c for _, c in comp]
    return comp, (min(cs) + 1, max(cs) + 1)


def choose(grid, cands):
    """Top-K 候选交给 Jev 选 —— 它擅长"哪个更好"，不擅长"算几个"。

    criteria 的写法经 golden_tetris.py 校准过：消行必须写成显式正向事实并提到最前，
    否则模型会照字面比较 maxh/bumpiness 而放弃消行（实测 80% → 100%，conf 0.77 → 0.92）。
    """
    def desc(m):
        lead = (f"CLEARS {m['cleared']} LINE(S) — the best possible outcome; "
                f"after the clear the stack is lower than these numbers suggest. "
                if m["cleared"] else "clears no lines. ")
        return (f"{lead}Drop in column {m['col']} (orientation {m['orient']}, "
                f"{m['width']} wide): creates {m['new_holes']} new hole(s), "
                f"max stack height {m['max_height_after']}, "
                f"surface bumpiness {m['bumpiness_after']}")

    crit = {f"{m['col']}|{m['orient']}": desc(m) for m in cands}
    q = {"move": {"type": "choice", "instructions": (
        "Tetris. `board` shows the well: '.' is empty, any letter is filled, row 0 is the TOP "
        "and row 19 the BOTTOM. Choose the best placement for the current piece. Priority order, "
        "strictly in this order: (1) clear the most lines — a placement that clears a line always "
        "beats one that does not, even if its height or bumpiness numbers look worse; "
        "(2) create the fewest new holes; (3) keep the stack low; (4) keep the surface flat."),
        "criteria": crit}}
    a = ask({"board": render(grid),
             "goal": "Survive as long as possible and clear lines"}, q)["answers"]["move"]
    col, orient = a["choice"].split("|")
    return int(col), int(orient), a["confidence"]


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
    stale = 0
    for i in range(1, a.moves + 1):
        # a11y dump 要 2.2s，是整步里最贵的一项，而棋盘几何和按键在一局内不会变。
        # 所以只在**首帧**和**怀疑出事时**（连续两帧读不到方块）才 dump；
        # 其余帧只截图。实测单步 4.5s → 2.3s，方块下落造成的落点偏差随之减半。
        if i == 1 or stale >= 2:
            xml_now = dump_ui()
            mask = re.search(r'resource-id="[^"]*(gameover|progress-mask|popup-mask)[^"]*"',
                             xml_now)
            if mask or "游戏结束" in xml_now:
                print(f"[{i}] 检测到遮罩/游戏结束，停止。")
                break
            board_now, btns_now, hud = find_board_and_buttons(xml_now)
            if not board_now or "left" not in btns_now:
                print(f"[{i}] 棋盘/控制键已消失，停止。")
                break
            board, btns = board_now, btns_now
            drop = "hard" if "hard" in btns else "soft"
            stale = 0

        grid = digitize(screen_png(), board, cols, rows_n, hud)
        piece, span = piece_cells(grid)
        if not piece:
            stale += 1
            time.sleep(0.15)
            continue
        stale = 0
        # 真实建模：认出方块种类 → 枚举 (朝向 × 列) 全部合法落点 → 模拟 → 打分
        cells0 = [(r, c) for r, c in piece]
        r0 = min(r for r, _ in cells0); c0 = min(c for _, c in cells0)
        norm = [(r - r0, c - c0) for r, c in cells0]
        # 盘面要去掉正在下落的方块，否则会把它自己当成障碍
        stack = [list(row) for row in grid]
        for r, c in cells0:
            stack[r][c] = "."
        stack = ["".join(r) for r in stack]
        cands, base = candidates(stack, norm, rows_n, top_k=5)
        if not cands:
            print(f"[{i}] 无合法落点，停止。"); break
        col, orient, conf = choose(stack, cands)
        kind = cands[0]["kind"]
        print(f"[{i}] {kind}块 列{span[0]}-{span[1]} 高{base['max_height']} 洞{base['holes']} "
              f"→ 列{col} 朝向{orient} conf={conf:.2f} "
              f"(消{next((m['cleared'] for m in cands if m['col']==col and m['orient']==orient),0)}行)")
        if a.dry_run:
            continue
        # 一次性把整套操作串成**单条 adb 命令**发出去。
        # 真机实测：screencap 400ms、单次 input tap 93ms。逐次截图校正会让方块在
        # 校正期间又掉好几行（一次校正循环 ≈0.5s），落点必偏；而分开发 N 条 adb
        # 命令也要 N×93ms。把 rotate/移动/落下用 ';' 串进一次 shell 调用，
        # 整套动作在 ~100ms 内打完，方块几乎来不及下落。
        cur_orient = identify(norm)[1]
        n_orients = len(SHAPES[kind]) if kind in SHAPES else 1
        n_rot = (orient - cur_orient) % n_orients
        seq = []
        if "rotate" in btns:
            rx, ry = btns["rotate"]
            seq += [f"input tap {rx} {ry}"] * n_rot
        # 旋转会改变方块的最左列。玩吧是绕包围盒左上角转的，所以旋转后
        # 最左列仍是原 span[0]；用目标朝向的宽度夹住右边界，避免撞墙空点。
        target_w = max(c for _, c in SHAPES[kind][orient]) + 1 if kind in SHAPES else 1
        want = min(col, cols - target_w + 1)
        delta = want - span[0]
        if delta:
            mx, my = btns["right" if delta > 0 else "left"]
            seq += [f"input tap {mx} {my}"] * abs(delta)
        dx, dy = btns[drop]
        if drop == "hard":
            seq.append(f"input tap {dx} {dy}")
        else:
            # 游戏自己的提示写着"长按方向或软降可连续操作"。
            # 实测：连点 20 次 = 1159ms，长按一次 = 996ms 且更可靠
            # （连点之间有间隙，方块会被判定为多次单步而非连续下落）。
            seq.append(f"input swipe {dx} {dy} {dx} {dy} 700")
        adb("shell", ";".join(seq))
        time.sleep(0.2)

    grid = digitize(screen_png(), board, 10, 20, hud)
    print("\n最终棋盘:"); print(render(grid))
    print(json.dumps(features(grid), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

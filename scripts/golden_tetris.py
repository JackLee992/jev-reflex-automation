#!/usr/bin/env python3
"""
golden_tetris.py —— 俄罗斯方块决策的 golden set 校准。

这是"官方水准"和"能跑"之间的分界线：TypeSafe 每个 cookbook 都配标注数据 + 准确率，
而不是挑几个好看的例子。这里用**代码构造的、答案唯一确定**的盘面，
逐条检验 Jev 在 Top-K 候选里是否选中最优解，并输出准确率和平均置信度。

跑法：
  python3 golden_tetris.py            # 全量
  python3 golden_tetris.py --quick    # 前 6 条
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jev_reflex import ask                              # noqa: E402
from tetris_model import SHAPES, candidates             # noqa: E402

R, C = 20, 10


def board(rows_spec: dict[int, str]) -> list[str]:
    """rows_spec: {行号: '1010...'}，1=占用。其余行全空。"""
    g = ["." * C for _ in range(R)]
    for r, bits in rows_spec.items():
        g[r] = "".join("X" if b == "1" else "." for b in bits)
    return g


# 每条: (名字, 盘面, 方块朝向, 期望列, 为什么)
CASES = [
    ("空盘竖I靠边", board({}), SHAPES["I"][1], None,
     "空盘任意列都可，仅检查不产生洞"),
    ("单缺口消行", board({19: "1111111110"}), SHAPES["I"][1], 10,
     "第10列是唯一缺口，竖I填入即消行"),
    ("左侧深井", board({19: "0111111111", 18: "0111111111"}), SHAPES["I"][1], 1,
     "第1列深2格，竖I填入消2行"),
    ("平地放O", board({19: "1111000000", 18: "1111000000"}), SHAPES["O"][0], 5,
     "右侧平地，O放在紧邻台阶处最平整"),
    ("阶梯放L", board({19: "1111111100", 18: "1111110000"}), SHAPES["L"][1], 9,
     "L竖放贴合右侧阶梯，不造洞"),
    ("避免造洞", board({19: "1101111111"}), SHAPES["O"][0], None,
     "第3列是宽1缺口，O(宽2)无论放哪都会盖洞，检查它选新洞最少的"),
    ("T填凹槽", board({19: "1110111111", 18: "1110011111"}), SHAPES["T"][0], None,
     "检查不把凹槽盖死"),
    ("高堆求生", board({r: "1111100000" for r in range(6, 20)}), SHAPES["I"][0], 6,
     "左侧堆到14高，横I必须放右侧空地"),
    ("双缺口选深", board({19: "1110111110", 18: "1111111110"}), SHAPES["I"][1], 10,
     "第10列深2，第4列深1，优先填深的消2行"),
    ("S形贴合", board({19: "1111000000", 18: "1110000000"}), SHAPES["S"][1], 4,
     "竖S恰好贴合左侧单格台阶"),
]


def ask_jev(grid, cands):
    # 关键：criteria 里把"消行"写成显式的正向事实，而不是只给一堆数字让模型自己权衡。
    # 原因（golden set 抓到的）：消行落点往往伴随更高的 maxh 和 bumpiness
    #   （竖 I 填缺口：cleared=1 但 maxh 3>2、bump 3>2），
    # 模型照字面比较数字就会选"更矮更平"的不消行落点，conf 只有 0.46~0.52。
    # 把 CLEARS N LINE(S) 提到描述最前面并注明"消行后堆叠反而下降"，歧义即消失。
    def desc(m):
        lead = (f"CLEARS {m['cleared']} LINE(S) — the best possible outcome; "
                f"after the clear the stack is lower than these numbers suggest. "
                if m["cleared"] else "clears no lines. ")
        return (f"{lead}Drop in column {m['col']} (orientation {m['orient']}, "
                f"{m['width']} wide): creates {m['new_holes']} new hole(s), "
                f"max stack height {m['max_height_after']}, "
                f"surface bumpiness {m['bumpiness_after']}")

    crit = {str(m["col"]): desc(m) for m in cands}
    q = {"move": {"type": "choice", "instructions": (
        "Tetris. `board` shows the well: '.' is empty, any letter is filled, row 0 is the TOP "
        "and row 19 the BOTTOM. Choose the best placement for the current piece. Priority order, "
        "strictly in this order: (1) clear the most lines — a placement that clears a line always "
        "beats one that does not, even if its height or bumpiness numbers look worse; "
        "(2) create the fewest new holes; (3) keep the stack low; (4) keep the surface flat."),
        "criteria": crit}}
    head = "   " + "".join(str((i + 1) % 10) for i in range(C))
    state = {"board": "\n".join([head] + [f"{i:2d} {row}" for i, row in enumerate(grid)]),
             "goal": "Survive as long as possible and clear lines"}
    a = ask(state, q)["answers"]["move"]
    return int(a["choice"]), a["confidence"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    cases = CASES[:6] if args.quick else CASES

    hits, confs, agree, lat = 0, [], 0, []
    print(f"{'case':<14}{'期望':>5}{'代码最优':>9}{'Jev':>6}{'conf':>7}  结果")
    print("-" * 62)
    for name, grid, cells, want, why in cases:
        cands, _ = candidates(grid, list(cells), R, top_k=5)
        if not cands:
            print(f"{name:<14}  无合法候选，跳过"); continue
        best = cands[0]["col"]                       # 代码算出的最优
        t = time.time(); got, cf = ask_jev(grid, cands); lat.append(time.time() - t)
        confs.append(cf)
        target = want if want is not None else best  # 未指定期望列时以代码最优为准
        ok = got == target
        hits += ok
        agree += (got == best)
        print(f"{name:<14}{str(want or '-'):>5}{best:>9}{got:>6}{cf:>7.2f}  {'✅' if ok else '❌ ' + why[:28]}")

    n = len(confs)
    print("-" * 62)
    print(f"准确率(对期望)  {hits}/{n} = {hits/n:.0%}")
    print(f"与代码最优一致  {agree}/{n} = {agree/n:.0%}")
    print(f"平均置信度      {statistics.mean(confs):.2f}   平均延迟 {statistics.mean(lat)*1000:.0f}ms")
    return 0 if hits == n else 1


if __name__ == "__main__":
    sys.exit(main())

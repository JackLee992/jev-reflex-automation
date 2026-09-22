#!/usr/bin/env python3
"""
verify.py —— 回归验证。用真机抓下来的 fixture 跑，不需要连手机。

  python3 tests/verify.py            # 全部（含 Jev API 调用）
  python3 tests/verify.py --offline  # 只跑纯逻辑 + 图像，不花钱

每条断言都对应一个**真实踩过的 bug**，不是为了凑覆盖率。
退出码 0 = 全绿。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"
sys.path.insert(0, str(ROOT / "scripts"))

ok: list[str] = []
fail: list[str] = []
skip: list[str] = []


def check(name, cond, detail=""):
    (ok if cond else fail).append(f"{name}  {detail}".rstrip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="跳过所有 Jev API 调用")
    args = ap.parse_args()

    mods = ["canvas_grid", "golden_tetris", "jev_reflex",
            "loop", "observe", "play_tetris", "tetris_model"]
    r = subprocess.run([sys.executable, "-m", "py_compile"]
                       + [str(ROOT / "scripts" / f"{m}.py") for m in mods],
                       capture_output=True, text=True)
    check(f"compile {len(mods)} modules", r.returncode == 0, r.stderr[:120])

    from canvas_grid import digitize, features
    from play_tetris import find_board_and_buttons, piece_cells
    from tetris_model import SHAPES, board_features, candidates, identify, simulate

    # ---- tetris_model：纯逻辑 ----
    bad = [(k, i) for k, os_ in SHAPES.items()
           for i, o in enumerate(os_) if identify(list(o)) != (k, i)]
    check("identify all 7 pieces x orientations", not bad, str(bad[:3]))

    c, _ = candidates(["." * 10] * 20, list(SHAPES["I"][0]), 20)
    check("empty board -> no new holes", bool(c) and all(m["new_holes"] == 0 for m in c))

    near = ["." * 10] * 19 + ["XXXXXXXXX."]
    c2, _ = candidates(near, list(SHAPES["I"][1]), 20)
    check("vertical I fills the gap and clears", c2[0]["col"] == 10 and c2[0]["cleared"] == 1,
          f"col={c2[0]['col']} cleared={c2[0]['cleared']}")

    stack = ["." * 10] * 18 + ["....XX....", "....XX...."]
    g2, cleared = simulate(stack, board_features(stack)["heights"],
                           list(SHAPES["O"][0]), 4, 20)
    rows = [r for r, row in enumerate(g2) if row.strip(".")]
    check("piece rests ON TOP, never overlaps",
          cleared == 0 and rows == [16, 17, 18, 19], f"rows={rows} cleared={cleared}")

    # ---- canvas_grid：真机截图，两种主题 ----
    fd = features(digitize(str(FIX / "tetris_live.png"), [51, 402, 741, 1785], 10, 20))
    check("dark theme digitises stably",
          fd["heights"] == [3, 3, 3, 11, 12, 12, 5, 2, 2, 1] and fd["holes"] == 17,
          str(fd["heights"]))

    # 「下一块」预览画在棋盘右上角，曾让 heights[9] 恒为 18
    ghosts = [f for f in ("f1.png", "f3.png")
              if digitize(str(FIX / f), [54, 806, 672, 2045], 10, 20)[2].strip(".")]
    check("light theme: HUD preview masked out", not ghosts, str(ghosts))

    g3 = digitize(str(FIX / "f3.png"), [54, 806, 672, 2045], 10, 20)
    p3, _ = piece_cells(g3)
    check("piece_cells returns exactly 4 cells", len(p3) == 4, f"got {len(p3)}")
    r0 = min(r for r, _ in p3)
    c0 = min(c for _, c in p3)
    check("falling piece is identifiable",
          identify([(r - r0, c - c0) for r, c in p3])[0] is not None)

    without = [list(row) for row in g3]
    for r, cc in p3:
        without[r][cc] = "."
    check("board minus falling piece is empty on a fresh game",
          board_features(["".join(r) for r in without])["holes"] == 0)

    _, _, hud = find_board_and_buttons((FIX / "h2_ui.xml").read_text(encoding="utf-8"))
    check("find_board_and_buttons returns (board, btns, hud)", isinstance(hud, list))

    # ---- observe：本轮修掉的两个真 bug ----
    names = [e["name"] for e in json.loads((FIX / "conn2.json").read_text())["elements"]]
    check("Bluetooth nav row kept (dropping it cost conf 0.43)",
          any("蓝牙" in n and "整行" in n for n in names))
    check("switches declare their state",
          any("开关" in n and ("当前开" in n or "当前关" in n) for n in names))

    # ---- jev_reflex：投机扇出 + 具名复核（要调 API）----
    if args.offline:
        skip.append("live Jev checks (--offline)")
    elif not (Path.home() / ".config/typesafe/api_key").exists():
        skip.append("live Jev checks (no api key)")
    else:
        from jev_reflex import reflex, verify_action
        st = json.loads((FIX / "dlg2.json").read_text())
        st["done_criteria"] = "the Tetris gameplay is active with falling blocks"
        res = reflex(st)
        for k in ("dialog_kind", "dismiss_ref", "screen_has_destructive_p"):
            check(f"fan-out returns {k}", k in res)
        check("save dialog classified as a decision",
              res["dialog_kind"] == "decision", res["dialog_kind"])

        lbl = {e["ref"]: e["name"] for e in st["elements"]}
        keep = verify_action(st, res["dismiss_ref"], lbl.get(res["dismiss_ref"], ""))
        check("resume -> AUTO, destroys_data low",
              keep["destroys_data_p"] < 0.5 and keep["verdict"] == "AUTO",
              f"destroys={keep['destroys_data_p']}")
        ref = next(k for k, v in lbl.items() if "重新开始" in v)
        wipe = verify_action(st, ref, lbl[ref])
        check("restart -> blocked (would delete the save)",
              wipe["destroys_data_p"] >= 0.5 and wipe["verdict"] != "AUTO",
              f"destroys={wipe['destroys_data_p']} verdict={wipe['verdict']}")

    for line in ok:
        print("  ✅", line)
    for line in skip:
        print("  ⏭ ", line)
    for line in fail:
        print("  ❌", line)
    print(f"\n{len(ok)} passed, {len(fail)} failed, {len(skip)} skipped")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())

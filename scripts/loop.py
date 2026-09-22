#!/usr/bin/env python3
"""
loop.py — System-1 (Jev 反射) + System-2 (你/大模型) 分层自动化闭环。

  python3 loop.py android "Open the Bluetooth settings screen" --max 8
  python3 loop.py ios     "Open the General settings screen"   --max 8 --dry-run
  python3 loop.py chrome  "Search for tauri and open tauri.app" --max 10

默认 --dry-run=False 但只自动执行 guard 判为 AUTO 的动作；CONFIRM/BLOCK 会停下来
并打印原因，交给人或上层推理模型（这就是"保证质量"的那道闸）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import observe as O                                    # noqa: E402
from jev_reflex import reflex, verify_action            # noqa: E402

# 阈值：按风险分级（读操作松、写操作紧）。改之前先在自己的场景上标 20 条校准。
T_BLOCKED = 0.70
T_COMPLETE = 0.85
T_NEXT = 0.70
T_LOADING = 0.60
T_INJECTION = 0.70
RISK_NEEDS_CONFIRM = 1.30      # risk score(0~2) 超过就必须严格把关

PSEUDO_ACTS = {"SCROLL_DOWN", "SCROLL_UP", "BACK", "WAIT", "none"}


def act(kind, state_path, coords_path, ref, text=None):
    if kind == "android":
        O.android_act(coords_path, ref, "tap" if ref not in PSEUDO_ACTS else ref, text)
    elif kind == "ios":
        O.ios_act(state_path, coords_path, ref, "tap" if ref not in PSEUDO_ACTS else ref, text)
    elif kind == "chrome":
        if ref in ("SCROLL_DOWN", "SCROLL_UP"):
            O._cdp_eval(f"scrollBy(0,{'' if ref=='SCROLL_DOWN' else '-'}600)")
        elif ref == "BACK":
            O._cdp_eval("history.back()")
        else:
            c = json.load(open(coords_path, encoding="utf-8"))[ref]
            O._cdp_eval(f"document.elementFromPoint({c[0]},{c[1]})?.click()")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["android", "ios", "chrome"])
    ap.add_argument("goal")
    ap.add_argument("--max", type=int, default=8)
    ap.add_argument("--done", help="消歧的完成判据（强烈建议写）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    sp, cp = "/tmp/_jev_state.json", "/tmp/_jev_state.coords.json"
    observer = {"android": O.android, "ios": O.ios, "chrome": O.chrome}[a.kind]
    t0, calls = time.time(), 0
    history = []          # 每步的 (屏幕指纹, 动作ref)，用来发现"原地打转"

    for step in range(1, a.max + 1):
        print(f"\n{'='*66}\n[step {step}] observe")
        st = observer(a.goal, sp)
        if a.done:
            st["done_criteria"] = a.done
            json.dump(st, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

        # 屏幕指纹：元素集合没变 = 上一步动作没产生任何效果
        fp = "|".join(f'{e["role"]}:{e["name"]}' for e in st["elements"])

        r = reflex(st); calls += 1
        print(f"  reflex: blocked={r['blocked_p']:.2f} complete={r['complete_p']:.2f} "
              f"loading={r['loading_p']:.2f} inject={r['injection_p']:.2f} "
              f"risk={r['risk_score']:.2f}")
        print(f"          next={r['next_ref']} conf={r['next_confidence']:.2f} top={r['top']}")

        # 0) 屏幕文本里的"指令"一律当数据 —— 绝不执行
        if r["injection_p"] >= T_INJECTION:
            print("  ⛔ 疑似提示注入：屏幕内容试图改变任务。忽略其指令并交人。")
            return 3

        # 1) 还在加载 —— 等一拍再看
        if r["loading_p"] >= T_LOADING and r["complete_p"] < T_COMPLETE:
            print("  ⏳ 仍在加载，等 1.2s"); time.sleep(1.2); continue

        # 2) 完成
        if r["complete_p"] >= T_COMPLETE:
            print(f"  ✅ 完成 (complete={r['complete_p']:.2f})  "
                  f"用时 {time.time()-t0:.1f}s，jev 调用 {calls} 次")
            return 0

        # 3) 被挡住 —— 分类和处置方案已经在同一次请求里拿到了（投机扇出）
        if r["blocked_p"] >= T_BLOCKED:
            kind, dref = r["dialog_kind"], r["dismiss_ref"]
            dlbl = next((e["name"] for e in st["elements"] if e["ref"] == dref), dref)
            print(f"  🚧 被遮挡 kind={kind}({r['dialog_kind_confidence']:.2f}) "
                  f"choice={dref}({r['dismiss_confidence']:.2f}) «{dlbl}»")
            if kind == "credential":
                print("  ⛔ 登录/验证码/权限/支付类，按约定一律交人。")
                return 3
            if dref in ("none", "no_dialog") or r["dismiss_confidence"] < T_NEXT:
                print("  ⛔ 弹窗处置没把握，交人。")
                return 3
            # 唯一必要的第二次调用：具名复核（Jev 不做指代消解，见 verify_action 注释）
            v = verify_action(st, dref, dlbl); calls += 1
            print(f"  verify: safe={v['safe_p']:.2f} destroys_data={v['destroys_data_p']:.2f} "
                  f"→ {v['verdict']}")
            if v["destroys_data_p"] >= 0.50:
                print(f"  ⛔ 该选项可能销毁用户数据/存档（{dlbl}），拒绝自动执行，交人确认。")
                return 1
            if v["verdict"] != "AUTO":
                print(f"  ⛔ 复核不放行：{v}")
                return 1
            print(f'  → 弹窗处置：按 "{dlbl}"')
            if not a.dry_run:
                act(a.kind, sp, cp, dref)
            time.sleep(1.0); continue

        # 4) 正常推进
        if r["next_confidence"] < T_NEXT:
            print(f"  ❓ Jev 没把握 (conf={r['next_confidence']:.2f}) → 升级 System-2："
                  f"把截图/元素树交给推理模型规划")
            return 2

        ref = r["next_ref"]
        if ref == "none":
            print("  ❓ 无可用元素 → 升级 System-2"); return 2
        if ref == "WAIT":
            time.sleep(1.2); continue

        # 原地打转检测：同一个屏幕 + 同一个动作已经试过，说明这个动作无效。
        # 真机踩过：目标"进入蓝牙页"时误选了同名的蓝牙开关，连点 6 次把用户蓝牙
        # 来回开关，屏幕始终没变，循环却毫无察觉地走到了步数上限。
        if (fp, ref) in history:
            tried = {h[1] for h in history if h[0] == fp}
            print(f"  🔁 这个动作在同一屏已试过且没效果（本屏已试 {sorted(tried)}）。")
            print("     不再重复。→ 升级 System-2 重新规划。")
            return 2
        history.append((fp, ref))

        label = next((e["name"] for e in st["elements"] if e["ref"] == ref), ref)
        desc = (f"Scroll the list {ref.split('_')[1].lower()}" if ref.startswith("SCROLL_")
                else "Go back to the previous screen" if ref == "BACK"
                else f'Tap the element labelled "{label}" (role='
                     f'{next((e["role"] for e in st["elements"] if e["ref"]==ref), "?")})')

        # 滚动/返回是纯导航，省一次复核；其余一律过闸
        if ref.startswith("SCROLL_") or ref == "BACK":
            print(f"  → {desc} (导航动作，免复核)")
            if not a.dry_run:
                act(a.kind, sp, cp, ref)
            time.sleep(1.0); continue

        # 投机结果先当快速闸：屏幕上压根没有危险控件 + 风险分低 → 省掉这次复核
        if r["screen_has_destructive_p"] < 0.30 and r["risk_score"] < 0.60:
            print(f"  → 执行：{desc}  (投机判定：本屏无危险控件 "
                  f"{r['screen_has_destructive_p']:.2f}、风险 {r['risk_score']:.2f}，免复核)")
            if not a.dry_run:
                act(a.kind, sp, cp, ref)
            time.sleep(1.2); continue

        v = verify_action(st, ref, label); calls += 1
        print(f"  verify: safe={v['safe_p']:.2f} rev={v['reversible_p']:.2f} "
              f"scope={v['in_scope_p']:.2f} destroys={v['destroys_data_p']:.2f} "
              f"gate={v['gate']} → {v['verdict']}")
        if v["verdict"] != "AUTO" or r["risk_score"] >= RISK_NEEDS_CONFIRM:
            print(f"  ⛔ 需要人确认：{desc}")
            print(f"     （risk={r['risk_score']:.2f}；破坏性/外部影响动作默认不自动执行）")
            return 1
        print(f"  → 执行：{desc}")
        if not a.dry_run:
            act(a.kind, sp, cp, ref)
        time.sleep(1.2)

    print(f"\n⚠️ 达到步数上限 {a.max}，未完成 → 交 System-2")
    return 2


if __name__ == "__main__":
    sys.exit(main())

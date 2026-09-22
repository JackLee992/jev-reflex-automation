#!/usr/bin/env python3
"""通过 RDP 把玩吧重置到「经典方块」的活局状态。

为什么需要它：真机调试时游戏经常停在结束遮罩上，而 `MASKED` 哨兵会让主循环
立刻退出。手动点屏幕很慢（每次 adb tap 86ms 且要先 dump 找坐标），
直接在页面里点按钮既快又不依赖坐标。

踩坑：结束弹窗的「开启下一把」会回到**模式选择菜单**而不是直接重开，
所以必须两步：先关遮罩，再点「经典方块」。
"""
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from gecko_rdp import Page, read_board  # noqa: E402

CLOSE_OVERLAY = """(function(){
  var t=Array.from(document.querySelectorAll('button,[role=button],a'))
    .filter(function(b){return /留在本局|开启下一把|再来一局/.test(b.innerText||'');});
  if(t.length){t[0].click();return 'closed:'+t[0].innerText.slice(0,8);}
  return 'no overlay button';
})()"""

ENTER_CLASSIC = """(function(){
  var t=Array.from(document.querySelectorAll('button,[role=button],a'))
    .filter(function(b){return (b.innerText||'').indexOf('经典方块')>=0;});
  if(t.length){t[0].click();return 'entered';}
  return 'not on menu';
})()"""

START_COVER = """(function(){
  var b=document.getElementById('wb-start-cover-btn');
  if(b){b.click();return 'started';}
  return 'no start cover';
})()"""

# 暂停态探测：wb-pause 的文字在暂停时变成「继续」。
# 这比"棋盘读不出来"可靠得多 —— 暂停时棋盘照样可读，只是静止。
PAUSED = """(function(){
  var b=document.getElementById('wb-pause');
  return !!(b && /继续/.test(b.innerText||''));
})()"""

RESUME = """(function(){
  var b=document.getElementById('wb-pause');
  if(b && /继续/.test(b.innerText||'')){b.click();return 'resumed';}
  return 'not paused';
})()"""


def paused(page):
    return bool(page.eval(PAUSED))


def reset(page, verbose=True):
    """把页面弄到「棋盘可读**且真的在跑**」的状态。

    两个真机踩坑：
    1. 「开启下一把」回到的是**模式选择菜单**，不是直接重开，所以要两步。
    2. 更隐蔽的一个：`read_board` 能读出棋盘 ≠ 游戏在跑。误触暂停后
       `wb-pause` 的文字会变成「继续」，棋盘静止但完全可读 —— 主循环会
       把同一帧当成新局反复决策，日志里表现为高度在 0/18/4/18 之间跳。
       所以就绪判据必须同时满足：无遮罩、能读棋盘、**且 pause 键不是「继续」**。
    """
    for step, js, wait in (("关遮罩", CLOSE_OVERLAY, 2.5),
                           ("进经典方块", ENTER_CLASSIC, 3.5),
                           ("点开始", START_COVER, 2.5)):
        r = page.eval(js)
        if verbose:
            print(f"  {step}: {r}")
        time.sleep(wait)
        b = read_board(page)
        if not isinstance(b, str) and not paused(page):
            return b
    # 走到这里可能只差一个「继续」
    if paused(page):
        page.eval(RESUME)
        if verbose:
            print("  解除暂停")
        time.sleep(1.0)
    return read_board(page)


if __name__ == "__main__":
    p = Page()
    try:
        b = reset(p)
        if isinstance(b, str):
            print("仍未就绪:", b)
            sys.exit(1)
        print("就绪，棋盘:")
        for i, row in enumerate(b):
            print(f" {i:2d} {row}")
    finally:
        p.close()

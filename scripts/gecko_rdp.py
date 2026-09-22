#!/usr/bin/env python3
"""Firefox Remote Debugging Protocol 客户端 —— 直接读 GeckoView 页面里的 JS 状态。

为什么需要这个（真机 profile，Mi MIX 2S root 机）：
    screencap -p        508 ms   ← 抓屏+设备端 PNG 编码
    JS 读棋盘(本文件)     45 ms   ← 11.2x 更快
    adb tap              86 ms
    JS click             32 ms
抓屏那条路的成本几乎全在设备端合成+编码，我们这端再怎么优化解码都没用；
直接问页面要数据就把整段开销绕过去了。

**关键踩坑：玩吧用的是 GeckoView，不是 WebView。**
a11y 树把它标成 `android.webkit.WebView`，`@webview_devtools_remote_<pid>` socket
也确实存在 —— 但 `Java.choose("android.webkit.WebView")` 实例数为 0，CDP 的
`/json/list` 永远返回空数组。真正渲染的是 `org.mozilla.geckoview.GeckoView`，
它说的是 Firefox RDP，不是 Chrome DevTools Protocol。拿 CDP 去连是死路。

启用步骤（需要 root + frida）：
    1. 设备上跑 frida-server
    2. 用 Frida 把 GeckoRuntimeSettings.setRemoteDebuggingEnabled(true)
       （见 enable_remote_debugging.js）
    3. 出现 @<pkg>/firefox-debugger-socket，adb forward 到本地端口
    4. 本文件连上去，evaluateJSAsync 执行任意 JS

协议格式：每条消息是 `<字节数>:<JSON>`，不是换行分隔。
"""
import json
import os
import socket


def ensure_forward(serial=None, port=6080, pkg="io.github.jacklee992.wanba.compat"):
    """确保 adb forward 存在 —— 不存在就建，已存在则原样保留。

    真机踩坑：`adb forward --remove-all` 会把这条也清掉（调试时很容易顺手执行），
    之后 RDP 连接直接 EOFError: RDP connection closed，看起来像 app 崩了，
    实际 app 和 socket 都好好的，只是隧道没了。所以连接前先自愈一次。
    """
    import subprocess
    adb = os.path.expanduser("~/Library/Android/sdk/platform-tools/adb")
    base = [adb] + (["-s", serial] if serial else [])
    listed = subprocess.run(base + ["forward", "--list"],
                            capture_output=True, text=True).stdout
    if f"tcp:{port}" in listed:
        return False
    subprocess.run(base + ["forward", f"tcp:{port}",
                           f"localabstract:{pkg}/firefox-debugger-socket"],
                   capture_output=True)
    return True


class RDP:
    """Firefox RDP 连接。协议是 `<len>:<json>` 前缀分帧。"""

    def __init__(self, port=6080, timeout=10):
        ensure_forward(port=port)
        self.s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.buf = b""
        self.hello = self.recv()          # 服务端主动发一条 root 包

    def recv(self):
        while b":" not in self.buf:
            d = self.s.recv(65536)
            if not d:
                raise EOFError("RDP connection closed")
            self.buf += d
        n, rest = self.buf.split(b":", 1)
        n = int(n)
        while len(rest) < n:
            rest += self.s.recv(65536)
        self.buf = rest[n:]
        return json.loads(rest[:n])

    def send(self, obj):
        b = json.dumps(obj).encode()
        self.s.sendall(str(len(b)).encode() + b":" + b)

    def req(self, obj, want=None):
        self.send(obj)
        for _ in range(60):
            m = self.recv()
            if want and want in m:
                return m
            if m.get("from") == obj.get("to") and "type" not in m:
                return m
            if "error" in m:
                return m
        return None

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


class Page:
    """连上第一个 tab 的 consoleActor，之后可以反复 eval —— 复用连接，省握手。"""

    def __init__(self, port=6080):
        self.r = RDP(port)
        self.r.req({"to": "root", "type": "getRoot"})
        tabs = self.r.req({"to": "root", "type": "listTabs"})
        if not tabs or not tabs.get("tabs"):
            raise RuntimeError("no tabs; 页面还没加载？")
        self.tab = tabs["tabs"][0]
        target = self.r.req({"to": self.tab["actor"], "type": "getTarget"}) or {}
        frame = target.get("frame") or target.get("target") or {}
        self.console = frame.get("consoleActor")
        if not self.console:
            raise RuntimeError(f"no consoleActor in {json.dumps(target)[:200]}")

    def eval(self, expr):
        """执行 JS 并返回结果。注意：表达式必须是**单个表达式**，通常包成 IIFE。"""
        self.r.send({"to": self.console, "type": "evaluateJSAsync", "text": expr})
        for _ in range(80):
            m = self.r.recv()
            if m.get("type") == "evaluationResult" or "result" in m:
                if m.get("hasException"):
                    raise RuntimeError(f"JS 异常: {json.dumps(m)[:300]}")
                return m.get("result")
        raise TimeoutError("evaluateJSAsync 无响应")

    def close(self):
        self.r.close()


# 读棋盘的 JS。三个真机踩坑都编码在里面：
#   1. 必须先查 wb-gameover-mask 的 **computed display**（元素常驻 DOM，
#      只靠 getElementById 判存在会永远是 true，结果读到的是结束遮罩的花纹）
#   2. 背景是**垂直渐变**（顶部 253,236,240 → 底部 247,219,229），
#      拿左上角当基准去比差值会整屏误判；改用**饱和度**：
#      背景是低饱和粉白（max-min 13~21），方块是高饱和亮色
#   3. 阈值必须是 120，不是 60。实测三类值泾渭分明：
#        背景 13~21 / 某个淡色 UI 元素 r2c9 = 64 / **真方块 205 或 252**
#      用 60 会把那个 64 的格子当成方块，盘面凭空多一格，
#      piece_cells 就可能拿它当下落块，决策全歪。
BOARD_JS = r"""(function(){
  var mk=document.getElementById('wb-gameover-mask');
  if(mk && getComputedStyle(mk).display!=='none') return 'MASKED';
  var c=document.getElementById('wb-canvas'); if(!c) return 'NOCANVAS';
  var x=c.getContext('2d'),W=c.width,H=c.height,cw=W/10,ch=H/20;
  var d=x.getImageData(0,0,W,H).data;
  function sat(a,b){var i=((b|0)*W+(a|0))*4,r=d[i],g=d[i+1],bl=d[i+2];
    return Math.max(r,g,bl)-Math.min(r,g,bl);}
  var g='';
  for(var r=0;r<20;r++){
    for(var k=0;k<10;k++){ g += sat(k*cw+cw/2, r*ch+ch/2) > 120 ? '#' : '.'; }
    g+='|';
  }
  return g;
})()"""


def read_board(page):
    """返回 20 行 × 10 列的字符矩阵，或 'MASKED' / 'NOCANVAS' 这两个哨兵字符串。"""
    g = page.eval(BOARD_JS)
    if g in ("MASKED", "NOCANVAS"):
        return g
    return [r for r in g.split("|") if r]


def tap(page, which):
    """按控制键。经典模式的 id：wb-tetris-left/right/rotate/softdrop（无硬降）。

    **真机踩坑：不能用 element.click()。** 它会返回 'ok'、事件也确实派发了，
    但方块纹丝不动 —— 实测连按 4 次 left，span 一直停在 (4,5)。
    游戏绑的是 pointerdown/touchstart 这类指针事件，合成 click 不触发它们。
    改成派发完整的 pointerdown→touchstart→pointerup→touchend 序列后，
    每按一次都稳定移动一列。
    """
    ids = {"left": "wb-tetris-left", "right": "wb-tetris-right",
           "rotate": "wb-tetris-rotate", "soft": "wb-tetris-softdrop"}
    eid = ids.get(which, which)
    return page.eval(f"""(function(){{
      var b=document.getElementById({eid!r}); if(!b) return 'nobtn';
      var r=b.getBoundingClientRect(), x=r.left+r.width/2, y=r.top+r.height/2;
      function fire(t,C,extra){{
        var e=new C(t,Object.assign({{bubbles:true,cancelable:true,clientX:x,clientY:y,
          pointerId:1,pointerType:'touch',isPrimary:true,button:0,buttons:1}},extra||{{}}));
        b.dispatchEvent(e);
      }}
      try{{
        fire('pointerdown',PointerEvent); fire('touchstart',Event);
        fire('pointerup',PointerEvent,{{buttons:0}}); fire('touchend',Event);
        return 'ok';
      }}catch(e){{ return 'ERR '+e; }}
    }})()""")


if __name__ == "__main__":
    p = Page()
    print("tab:", p.tab.get("title"), p.tab.get("url", "")[:60])
    b = read_board(p)
    if isinstance(b, str):
        print("哨兵:", b)
    else:
        for i, row in enumerate(b):
            print(f"{i:2d} {row}")
    p.close()

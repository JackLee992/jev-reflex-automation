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
import socket


class RDP:
    """Firefox RDP 连接。协议是 `<len>:<json>` 前缀分帧。"""

    def __init__(self, port=6080, timeout=10):
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


# 读棋盘的 JS。两个真机踩坑都编码在里面：
#   1. 必须先查 wb-gameover-mask 的 **computed display**（元素常驻 DOM，
#      只靠 getElementById 判存在会永远是 true，结果读到的是结束遮罩的花纹）
#   2. 背景是**垂直渐变**（顶部 253,236,240 → 底部 247,219,229），
#      拿左上角当基准去比差值会整屏误判；改用**饱和度**：
#      背景是低饱和粉白（max-min ≈ 20），方块是高饱和亮色（如 186,250,45，max-min = 205）
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
    for(var k=0;k<10;k++){ g += sat(k*cw+cw/2, r*ch+ch/2) > 60 ? '#' : '.'; }
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
    """按控制键。经典模式的 id：wb-tetris-left/right/rotate/softdrop（无硬降）。"""
    ids = {"left": "wb-tetris-left", "right": "wb-tetris-right",
           "rotate": "wb-tetris-rotate", "soft": "wb-tetris-softdrop"}
    eid = ids.get(which, which)
    return page.eval(f"""(function(){{var b=document.getElementById({eid!r});
      if(b){{b.click();return 'ok';}}return 'nobtn';}})()""")


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

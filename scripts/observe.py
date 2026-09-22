#!/usr/bin/env python3
"""
observe.py — 把 Android / iOS / Chrome 当前界面归一成 jev_reflex 的 state JSON。

  python3 observe.py android "<goal>" out.json
  python3 observe.py ios     "<goal>" out.json      # 需 APPIUM_SID 或自动建会话
  python3 observe.py chrome  "<goal>" out.json      # 需 Chrome --remote-debugging-port=9222

产出 out.json（state）+ out.coords.json（ref -> [x,y] 点击坐标）。

三端共用的两条硬规则（真机实测得出）：
 1. **几何去重**：同一 (label, bounds) 只保留交互性最强的一个候选。
    iOS 的 Cell 和 Button 会完全重叠 —— 不去重则概率被劈成 0.52/0.48（低于 0.7 门槛
    而卡死），去重后同一目标 0.99。
 2. **label 优先于 identifier**：iOS 的 name= 往往是 com.apple.settings.general 这种
    英文 id，人类可见的中文标题在 label= 或子 StaticText 里。用 id 当 name 会让
    中文目标匹配不上；id 降级进 hints。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET

# 交互性优先级：数字小者在几何去重时胜出
PRIO = {"Switch": 0, "TextField": 0, "Slider": 0, "Picker": 0,
        "Button": 1, "Link": 1, "Tab": 1, "Segmented": 1, "CheckBox": 1,
        "Row": 2, "Text": 3}


def dedupe(raw: list[dict]) -> list[dict]:
    """同一 (name, box) 保留交互性最强的；再按屏幕顺序编 ref。"""
    best: dict = {}
    for e in raw:
        k = (e["name"], tuple(e["box"]) if e.get("box") else None)
        if k not in best or PRIO.get(e["role"], 9) < PRIO.get(best[k]["role"], 9):
            best[k] = e
    out = sorted(best.values(), key=lambda e: (e["box"][1] if e.get("box") else 0,
                                               e["box"][0] if e.get("box") else 0))
    for i, e in enumerate(out):
        e["ref"] = f"e{i}"
    return out


def finish(kind, goal, raw, title, page_text, out, scrollable=True):
    els = dedupe(raw)
    coords = {e["ref"]: [e["box"][0] + e["box"][2] // 2, e["box"][1] + e["box"][3] // 2]
              for e in els if e.get("box")}
    state = {"app": kind, "url": f"{kind}://device", "title": title[:120], "goal": goal,
             "scrollable": scrollable, "page_text": page_text[:1200],
             "elements": [{k: e[k] for k in ("ref", "role", "name", "hints")} for e in els]}
    json.dump(state, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(coords, open(out.replace(".json", ".coords.json"), "w", encoding="utf-8"))
    print(f"[{kind}] {len(els)} 元素 (原始 {len(raw)}，几何去重 -{len(raw)-len(els)}) -> {out}")
    for e in els[:40]:
        print(f'  {e["ref"]:<5}{e["role"]:<10}{e["name"][:44]:<46}{e["hints"]}')
    return state


# ------------------------------------------------------------------ Android
ADB = os.environ.get("ADB", os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"))
A_ROLE = {"Switch": "Switch", "ToggleButton": "Switch", "CheckBox": "CheckBox",
          "RadioButton": "CheckBox", "Button": "Button", "ImageButton": "Button",
          "EditText": "TextField", "SeekBar": "Slider", "TextView": "Text"}


def _adb(*a):
    dev = os.environ.get("ANDROID_SERIAL")
    pre = [ADB] + (["-s", dev] if dev else [])
    return subprocess.run(pre + list(a), capture_output=True)


def android(goal, out):
    _adb("shell", "uiautomator", "dump", "--compressed", "/sdcard/_jev.xml")
    xml = _adb("exec-out", "cat", "/sdcard/_jev.xml").stdout.decode("utf-8", "ignore")
    root = ET.fromstring(xml)
    raw, texts, scrollable = [], [], False

    def txt(n):
        return (n.attrib.get("text") or n.attrib.get("content-desc") or "").strip()

    def subtree(n):
        parts = []
        for c in n.iter():
            t = txt(c)
            if t and t not in parts:
                parts.append(t)
        return " / ".join(parts)[:80]

    for n in root.iter("node"):
        a = n.attrib
        if a.get("scrollable") == "true":
            scrollable = True
        t = (a.get("text") or "").strip()
        if t:
            texts.append(t)
        cls = (a.get("class") or "").split(".")[-1]
        clickable = a.get("clickable") == "true"
        role = A_ROLE.get(cls, "Row" if clickable else None)
        if role is None:
            continue
        # 关键：可点击的 TextView/ImageView 是真控件（安卓桌面图标、标签页就是这样），
        # 不能因为映射成 Text 就丢掉 —— 否则整个桌面一个图标都抓不到（真机踩过）。
        if role == "Text" and clickable:
            role = "Button"
        # 纯文本（不可点）只进 page_text，不进候选集
        if not (clickable or cls in A_ROLE) or role == "Text":
            continue
        name = txt(n) or subtree(n)
        rid = (a.get("resource-id") or "").split("/")[-1]
        if not name and cls not in ("EditText", "Switch", "CheckBox"):
            continue
        # 关键：可点击"容器"的处理要分三种（真机踩过三次）：
        #  a) 无名字、包着多个可点子节点（抽屉 gesture_control_layout 包 7 个）→ 丢掉，
        #     否则它的 subtree 名字会把整屏文字拼进来并劈分概率（0.63→0.99）。
        #  b) 有名字的卡片（游戏卡片，子节点只是"收藏"星标）→ 保留，真正该点的是整张卡片。
        #  c) 无名字、只包着 1 个开关（三星"连接"页的蓝牙行 = LinearLayout 包 Switch）→
        #     **必须保留**，因为它才是"进入蓝牙设置页"的入口；开关只切状态。
        #     早期把它一并丢掉，导致候选集里只剩开关，Jev 只能在"切开关"和"更多连接设置"
        #     之间二选一，conf 0.43 卡死。
        if role == "Row":
            click_kids = [c for c in n.iter()
                          if c is not n and c.attrib.get("clickable") == "true"]
            only_toggle = (len(click_kids) == 1
                           and (click_kids[0].attrib.get("class") or "").split(".")[-1]
                           in ("Switch", "ToggleButton", "CheckBox"))
            if click_kids and not txt(n) and not only_toggle:
                continue
            if click_kids and len(click_kids) >= 3:
                continue
            if only_toggle:
                # 这一行的可见文字在子孙 TextView 里，拿 subtree 文字当名字
                inner = subtree(n) or name
                name = f"{inner}（整行，点击进入该设置页）"
        # 可点击容器里若包着多个 Switch/CheckBox，它是"开关组"而不是导航行。
        # （单开关的情况已在上面 only_toggle 分支处理成"整行进入页面"）
        if role == "Row" and "整行" not in name:
            toggles = [c for c in n.iter()
                       if c is not n and (c.attrib.get("class") or "").split(".")[-1]
                       in ("Switch", "ToggleButton", "CheckBox")]
            if toggles:
                role = "Switch"
                st_ = toggles[0].attrib.get("checked")
                name = f"{name}（开关，当前{'开' if st_ == 'true' else '关'}）"
        # 关键：开关类控件必须在名字里自曝身份和状态。
        # 真机踩过两次：三星设置里「蓝牙」开关和「蓝牙」导航行同名，Jev 无从区分，
        # 目标是"进入蓝牙设置页"时它选了开关，结果把用户蓝牙来回开关了 6 次。
        if role == "Switch":
            ck = a.get("checked")
            state = "开" if ck == "true" else ("关" if ck == "false" else "?")
            if "开关" not in name:
                name = f"{name}（开关，当前{state}；点击会切换状态，不会进入页面）"
        m = re.findall(r"-?\d+", a.get("bounds") or "")
        box = None
        if len(m) >= 4:
            x1, y1, x2, y2 = map(int, m[:4])
            box = [x1, y1, x2 - x1, y2 - y1]
            if box[2] <= 0 or box[3] <= 0:
                continue
        h = []
        if rid:
            h.append(f"id={rid}")
        if a.get("checked") == "true":
            h.append("checked")
        if a.get("checked") == "false" and role == "Switch":
            h.append("unchecked")
        if a.get("enabled") == "false":
            h.append("disabled")
        raw.append({"role": role, "name": (name or rid or cls)[:70],
                    "hints": " ".join(h), "box": box})

    win = _adb("shell", "dumpsys", "window").stdout.decode("utf-8", "ignore")
    focus_line = next((l.strip() for l in win.splitlines() if "mCurrentFocus" in l), "")
    # 只留 package/activity，别把整行 mCurrentFocus=Window{65d01af u0 ...} 塞进 state。
    # 真机实测：原始 dump 串是纯噪声，会把 complete 判断从 0.93 拉低到 0.69
    # —— 官方明确说过"无关上下文会降低准确率"。
    m_act = re.search(r"(\S+)/(\S+?)\}", focus_line)
    activity = f"{m_act.group(1)}/{m_act.group(2)}" if m_act else "android"
    # 屏幕标题：a11y 树里第一条不可点的短文本通常就是页面标题
    title = next((t for t in texts if 0 < len(t) <= 24), "") or activity
    return finish("android", goal, raw, f"{title}  [{activity}]",
                  " | ".join(dict.fromkeys(texts))[:1200], out, scrollable)


def android_act(coords_path, ref, action="tap", text=None):
    c = json.load(open(coords_path, encoding="utf-8"))
    if action == "tap":
        x, y = c[ref]
        _adb("shell", "input", "tap", str(x), str(y))
    elif action == "type":
        _adb("shell", "input", "text", text.replace(" ", "%s"))
    elif action in ("SCROLL_DOWN", "SCROLL_UP"):
        y1, y2 = (1600, 500) if action == "SCROLL_DOWN" else (500, 1600)
        _adb("shell", "input", "swipe", "540", str(y1), "540", str(y2), "250")
    elif action == "BACK":
        _adb("shell", "input", "keyevent", "4")


# ---------------------------------------------------------------------- iOS
APPIUM = os.environ.get("APPIUM_URL", "http://127.0.0.1:4723")
I_ROLE = {"XCUIElementTypeButton": "Button", "XCUIElementTypeLink": "Link",
          "XCUIElementTypeCell": "Row", "XCUIElementTypeSwitch": "Switch",
          "XCUIElementTypeTextField": "TextField", "XCUIElementTypeStaticText": "Text",
          "XCUIElementTypeSecureTextField": "TextField", "XCUIElementTypeTabBarButton": "Tab",
          "XCUIElementTypeSegmentedControl": "Segmented", "XCUIElementTypeSlider": "Slider",
          "XCUIElementTypePickerWheel": "Picker", "XCUIElementTypeSearchField": "TextField"}


def _http(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(APPIUM.rstrip("/") + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def ios_session():
    """复用现有会话（关键：每次 dump 都新建会话会重启 WDA，慢且会丢屏幕状态）。
    newCommandTimeout=0 —— 否则 Appium 默认 60s 无命令就杀会话，下一次 dump 拿 404。"""
    sid = os.environ.get("APPIUM_SID")
    if sid:
        try:
            _http("GET", f"/session/{sid}/source", timeout=30)
            return sid
        except Exception:
            pass                                    # 失效则往下重建
    try:
        for s in _http("GET", "/sessions").get("value") or []:
            return s["id"]
    except Exception:
        pass
    caps = {"platformName": "iOS", "appium:automationName": "XCUITest",
            "appium:noReset": True, "appium:wdaLaunchTimeout": 300000,
            "appium:newCommandTimeout": 0}
    if os.environ.get("IOS_UDID"):
        caps["appium:udid"] = os.environ["IOS_UDID"]
    if os.environ.get("IOS_BUNDLE"):
        caps["appium:bundleId"] = os.environ["IOS_BUNDLE"]
    if os.environ.get("IOS_TEAM"):          # 真机需要签名
        caps["appium:xcodeOrgId"] = os.environ["IOS_TEAM"]
        caps["appium:xcodeSigningId"] = "Apple Development"
    sid = _http("POST", "/session",
                {"capabilities": {"alwaysMatch": caps, "firstMatch": [{}]}},
                timeout=420)["value"]["sessionId"]
    print(f"[ios] 新建会话 {sid}（建议 export APPIUM_SID={sid} 复用）", file=sys.stderr)
    return sid


def ios(goal, out):
    sid = ios_session()
    src = _http("GET", f"/session/{sid}/source")["value"]
    root = ET.fromstring(src)
    raw, texts, scrollable = [], [], False
    for n in root.iter():
        a = n.attrib
        cls = a.get("type", n.tag)
        if cls in ("XCUIElementTypeScrollView", "XCUIElementTypeTable",
                   "XCUIElementTypeCollectionView"):
            scrollable = True
        role = I_ROLE.get(cls)
        if role is None:
            continue
        if a.get("visible") == "false" or a.get("enabled") == "false":
            continue
        # label = 人类可见文案（优先）；name 常是 accessibility identifier
        label = (a.get("label") or "").strip()
        ident = (a.get("name") or "").strip()
        if not label:
            label = " / ".join(dict.fromkeys(
                (c.attrib.get("label") or c.attrib.get("name") or "").strip()
                for c in n.iter() if c is not n
                and c.attrib.get("type") == "XCUIElementTypeStaticText"
                and (c.attrib.get("label") or c.attrib.get("name"))))
        if role == "Text":
            if label:
                texts.append(label)
            continue
        if not label and cls not in ("XCUIElementTypeSwitch", "XCUIElementTypeTextField"):
            continue
        h = []
        if ident and ident != label:
            h.append(f"id={ident}")
        if a.get("value"):
            h.append(f"value={str(a['value'])[:20]}")
        box = None
        try:
            box = [int(float(a["x"])), int(float(a["y"])),
                   int(float(a["width"])), int(float(a["height"]))]
            if box[2] <= 0 or box[3] <= 0:
                continue
        except (KeyError, ValueError):
            pass
        raw.append({"role": role, "name": label[:70], "hints": " ".join(h), "box": box})

    app = root.find(".//XCUIElementTypeApplication")
    title = (app.attrib.get("name") if app is not None else "iOS") or "iOS"
    st = finish("ios", goal, raw, title, " | ".join(dict.fromkeys(texts))[:1200], out, scrollable)
    st["_session"] = sid
    json.dump(st, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return st


def ios_act(state_path, coords_path, ref, action="tap", text=None):
    sid = json.load(open(state_path, encoding="utf-8"))["_session"]
    if action == "tap":
        x, y = json.load(open(coords_path, encoding="utf-8"))[ref]
        _http("POST", f"/session/{sid}/actions", {"actions": [{
            "type": "pointer", "id": "finger1", "parameters": {"pointerType": "touch"},
            "actions": [{"type": "pointerMove", "duration": 0, "x": x, "y": y},
                        {"type": "pointerDown", "button": 0},
                        {"type": "pause", "duration": 60},
                        {"type": "pointerUp", "button": 0}]}]})
    elif action == "type":
        _http("POST", f"/session/{sid}/wda/keys", {"value": list(text)})
    elif action in ("SCROLL_DOWN", "SCROLL_UP"):
        _http("POST", f"/session/{sid}/execute/sync",
              {"script": "mobile: swipe",
               "args": [{"direction": "up" if action == "SCROLL_DOWN" else "down"}]})
    elif action == "BACK":
        _http("POST", f"/session/{sid}/execute/sync",
              {"script": "mobile: pressButton", "args": [{"name": "home"}]})


# ------------------------------------------------------------------- Chrome
CDP = os.environ.get("CDP_URL", "http://127.0.0.1:9222")


def _cdp_eval(expr):
    import http.client
    tabs = json.load(urllib.request.urlopen(f"{CDP}/json/list", timeout=10))
    page = next(t for t in tabs if t["type"] == "page")
    ws = page["webSocketDebuggerUrl"]
    # 极简 websocket 客户端（避免依赖）
    import base64, os as _os, socket, struct
    u = re.match(r"ws://([^:/]+):(\d+)(.*)", ws)
    host, port, path = u.group(1), int(u.group(2)), u.group(3)
    k = base64.b64encode(_os.urandom(16)).decode()
    s = socket.create_connection((host, port), timeout=15)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
              f"Connection: Upgrade\r\nSec-WebSocket-Key: {k}\r\n"
              f"Sec-WebSocket-Version: 13\r\n\r\n".encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)

    def send(obj):
        p = json.dumps(obj).encode()
        hdr = b"\x81"
        n = len(p)
        mask = _os.urandom(4)
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(p)))

    def recv():
        while True:
            h = s.recv(2)
            if len(h) < 2:
                return None
            ln = h[1] & 127
            if ln == 126:
                ln = struct.unpack(">H", s.recv(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", s.recv(8))[0]
            d = b""
            while len(d) < ln:
                d += s.recv(ln - len(d))
            if h[0] & 0x0F == 1:
                return json.loads(d)

    send({"id": 1, "method": "Runtime.evaluate",
          "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}})
    while True:
        m = recv()
        if m and m.get("id") == 1:
            s.close()
            return m["result"]["result"].get("value")


JS = r"""(() => {
  const out=[], seen=new Set();
  const SEL='a,button,input,select,textarea,[role=button],[role=link],[role=tab],[role=checkbox],[role=switch],[onclick],summary';
  const vis=el=>{const r=el.getBoundingClientRect();const s=getComputedStyle(el);
    return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'&&parseFloat(s.opacity)>0.05
      &&r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth;};
  const role=el=>{const t=el.tagName.toLowerCase();const ty=(el.type||'').toLowerCase();
    if(t==='a')return'Link'; if(t==='button'||el.getAttribute('role')==='button')return'Button';
    if(t==='select')return'Picker'; if(t==='textarea')return'TextField';
    if(t==='input'){if(['checkbox','radio'].includes(ty))return'CheckBox';
      if(['submit','button','reset'].includes(ty))return'Button'; return'TextField';}
    return'Row';};
  document.querySelectorAll(SEL).forEach(el=>{
    if(!vis(el))return;
    const r=el.getBoundingClientRect();
    let name=(el.getAttribute('aria-label')||el.innerText||el.value||el.placeholder||
              el.getAttribute('title')||el.getAttribute('name')||'').trim().replace(/\s+/g,' ');
    const h=[]; if(el.type)h.push('type='+el.type); if(el.id)h.push('id='+el.id);
    if(el.href)h.push('href='+el.href.slice(0,60)); if(el.disabled)h.push('disabled');
    if(el.checked)h.push('checked');
    const box=[Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)];
    const k=name+'|'+box.join(',');
    if(seen.has(k))return; seen.add(k);
    if(!name&&role(el)!=='TextField')return;
    out.push({role:role(el),name:name.slice(0,70),hints:h.join(' '),box});
  });
  const sc=document.documentElement.scrollHeight>innerHeight+20;
  return JSON.stringify({elements:out,title:document.title,url:location.href,
    text:(document.body.innerText||'').replace(/\s+/g,' ').slice(0,1500),scrollable:sc});
})()"""


def chrome(goal, out):
    d = json.loads(_cdp_eval(JS))
    return finish("chrome", goal, d["elements"], f'{d["title"]} — {d["url"]}',
                  d["text"], out, d["scrollable"])


if __name__ == "__main__":
    kind, goal, out = sys.argv[1], sys.argv[2], sys.argv[3]
    {"android": android, "ios": ios, "chrome": chrome}[kind](goal, out)

# jev-reflex-automation

给 computer-use / 手机自动化装一个 **System-1 反射层**：用 [Jev](https://typesafe.ai)
（校准概率模型，~0.7s、约千分之一美分/次）做高频小判断，大模型只在真正需要规划时才上场。

浏览器 / Android / iOS / macOS 四端共用同一套判断逻辑，因为 DOM、AccessibilityNodeInfo、
XCUIElement、macOS AX 本质是**同构的无障碍树**。

> 本仓库所有数字都来自真机实测（Android 三星 SM-G9910 / iOS iPhone 17 模拟器 + 真机 /
> Chrome），不是设计稿。踩过的坑都写在代码注释和下面的「实测教训」里。

---

## 快速开始

```bash
# 1. 装 jev CLI 并配 key（~/.config/typesafe/api_key）
jev doctor          # 应显示 healthy

# 2. Android：看一眼当前屏被归一成什么
export ANDROID_SERIAL=$(adb devices | awk 'NR==2{print $1}')
python3 scripts/observe.py android "Open the Bluetooth settings screen" /tmp/s.json

# 3. 让 Jev 做一次反射判断（6 个问题并行，一次请求）
python3 scripts/jev_reflex.py reflex /tmp/s.json

# 4. 跑完整闭环（只自动执行 guard 判为 AUTO 的动作）
python3 scripts/loop.py android "Open the Bluetooth settings screen" \
  --done "the visible screen is the Bluetooth settings page itself, with the Bluetooth on/off switch (regardless of ON or OFF)"
```

---

## 架构：三层，各司其职

```
┌── 观察层 observe.py ───────────────────────────────────────┐
│  Android  uiautomator dump      →  归一 state JSON        │
│  iOS      Appium/WDA  /source   →  （同一个 schema）       │
│  Chrome   CDP Runtime.evaluate  →                         │
└───────────────────────────────────────────────────────────┘
                          ↓
┌── 决策层 jev_reflex.py（System-1）────────────────────────┐
│  reflex()        一次请求问 9 个（投机扇出）：               │
│      blocked / complete / loading / injection / risk /    │
│      next / dialog_kind / dismiss / has_destructive       │
│  verify_action() 唯一必要的第二次调用：具名控件最终复核       │
└───────────────────────────────────────────────────────────┘
                          ↓
┌── 执行层 loop.py ──────────────────────────────────────────┐
│  AUTO → 执行   CONFIRM → 问人   BLOCK → 拦截                │
│  置信度低 / 原地打转 / 要多步规划 → 升级 System-2            │
└───────────────────────────────────────────────────────────┘
```

**分工原则**：Jev 只回答「哪个更好 / 是不是」，**算术和枚举留在代码里**
（它明确不擅长计数、日期、多跳推理）。

---

## 核心模式：投机扇出（speculative fan-out）

这是 TypeSafe 官方的头号模式。官方文档把「先问类别、拿到答案再问下一个」明确称为
**the wrong way: sequential API calls**：优化了问题数量，却在延迟和成本上惨败。

所以 `reflex()` 把**分支才用得到的问题也一并问掉**——弹窗分类、弹窗处置方案、
本屏有无危险控件——大多数轮次用不上，但问了几乎不花钱，省掉整整一次往返。
代码负责挑哪些答案算数。

本机实测（玩吧存档弹窗，9 个问题）：

| 方式 | 请求数 | token | 延迟 |
|---|---|---|---|
| 顺序调用（官方称 the wrong way） | 9 | 4845 | 10879 ms |
| **投机扇出（官方推荐）** | **1** | **1747** | **892 ms** |

→ **便宜 2.8 倍、快 12.2 倍**（官方 13 问 GDPR 基准：便宜 12.2x、快 10.0x）

**什么时候才发第二个请求？** 只有当你需要第一次的答案才能构造第二次的 state 时。
本项目里只有一处：`verify_action()` 把选中的控件**具名**后复核（原因见教训 7）。

---

## 五类决策

| 判断 | 回答 | 真机实测 |
|---|---|---|
| `blocked` | 有弹窗/登录墙/验证码挡路吗？ | cookie 横幅 **0.99**、登录墙 **0.98**、真实确认弹窗 **0.87** |
| `complete` | 目标达成没有？ | 蓝牙页 **0.89**（修 title 噪声前只有 0.69） |
| `next` | 下一步操作哪个元素**或动作**？ | 蓝牙整行 **0.99**、游戏卡片 **0.98**、经典模式 **1.00** |
| `dialog_kind` + `dismiss` | 弹窗是什么类型？该按哪个？ | 存档弹窗 `decision` **0.99**、选中「继续上次」**0.99** |
| `verify_action` | 这个具名控件安全吗？会毁数据吗？ | 继续上次 destroys **0.04**→AUTO；重新开始 **0.94**→拦截 |

---

## 实测教训（每一条都踩过，已写进代码）

### 1. 候选集里必须有「动作」，不能只有元素
目标滚出视口时，只给元素会让模型硬选一个可见项（实测误选「搜索设置」0.65）。
加入 `SCROLL_UP/DOWN/BACK/WAIT/none` 伪元素后，正确选 `SCROLL_UP` **0.80**。

### 2. iOS 的 `name` 是 identifier，不是给人看的标签
`name="com.apple.settings.general"`，中文标题在 `label` 或子 `StaticText`。
用 name 当显示名会让中文目标匹配不上 → **label 优先，id 降级进 hints**。

### 3. 判「完成」必须消歧 + 喂屏幕文本
只喂按钮时完成度 0.13（假阴性）；加 `page_text` 升到 0.66；把判据写死
（"标题是蓝牙、含开关，**与开关开关状态无关**"）才到 **0.93**。

### 4. 几何去重：iOS 的 Cell 和 Button 完全重叠
同一个「通用」行同时是 Cell 和 Button，概率被劈成 0.52/0.48（低于 0.70 门槛直接卡死）。
按 `(label, bounds)` 去重、保留交互性最强的那个 → **0.99**。

### 5. 可点击容器要分三种
- 无名字、包多个可点子节点（抽屉 `gesture_control_layout` 包 7 个）→ **丢掉**，
  否则 subtree 名字把整屏文字拼进来，conf 0.63。
- 有名字的卡片（游戏卡片，子节点只是「收藏」星标）→ **保留**，
  否则只剩小星标按钮，护栏会合理地判成越权而拒绝。
- 无名字、只包 **1 个开关**（三星「连接」页的蓝牙行 = LinearLayout 包 Switch）→
  **必须保留**，它才是进入蓝牙页的入口。早期一并丢掉后，候选集里只剩开关，
  Jev 只能在「切开关」和「更多连接设置」间二选一，**conf 0.43 卡死**；修好后 **0.99**。

### 6. 可点击的 TextView/ImageView 是真控件
安卓桌面图标就是 `clickable=true` 的 TextView。按类名映射成 "Text" 丢掉的话，
**整个桌面一个图标都抓不到**。

### 7. Jev 不做指代消解，问题必须字面自足
问「`choice` 选中的那个选项会不会毁数据」→ 继续上次/重新开始都得 0.5~0.68（没用）。
把控件**具名**塞进 state 再问 → 继续上次 **0.04**、重新开始 **0.94**（完全可用）。
这就是 `verify_action()` 必须单独发一次请求的原因——不是懒，是官方规则里
「需要第一次的答案才能构造第二次 state」的那种情况。

### 8. 开关必须自曝身份和状态，否则 agent 会静默改用户设置
三星设置里「蓝牙」开关和「蓝牙」导航行**同名**。目标是「进入蓝牙设置页」时，
agent 选了开关，**把用户蓝牙来回开关了 6 次**而毫无察觉。
现在开关名字里带「（开关，当前关；点击会切换状态，不会进入页面）」。

### 9. 循环必须检测「原地打转」
上面那 6 次误触之所以没被发现，是因为循环只管步数上限，不管**有没有进展**。
现在每步记录 (屏幕指纹, 动作ref)，同屏同动作重复出现即判定无效 → 升级 System-2。

### 10. 别把无关上下文塞进 state
`title` 原来是整行 `mCurrentFocus=Window{65d01af u0 com.android.settings/...}`。
这串噪声把 complete 从 **0.88 拉低到 0.69**（直接卡在 0.85 门槛下）。
改成「页面标题 + package/activity」后恢复。官方早就说过「无关上下文会降低准确率」。

### 11. canvas 数字化必须配 a11y 哨兵
游戏结束遮罩盖住棋盘后，`digitize` 仍在读被虚化的残影，产出「方块悬空 + holes=48」
的垃圾数据，而循环还在照着它点。a11y 树能看到遮罩节点 → 用它当哨兵。

### 12. 棋盘几何每帧重取；HUD 要遮
- 模式切换后 canvas 从 DOM 消失，沿用缓存 bounds 会对着空白采样。
- 「下一块」预览面板**画在棋盘右上角之上**，会被当成盘面方块（假 'R'，heights[9]=18）。

### 13. 不能假设棋盘底色是深色
同一个 app：AI 对战深色底 (23,32,47)，经典模式**浅色底** (254,239,244)。
写死「暗=空」会把浅色棋盘读成全满。→ 取顶部两行众数当 bg，按**距离**判空；
再加「高亮度+低饱和=空」兜住浅色主题的阴影噪声，并要求 **55% 多数**采样同色才算占用。

---

## canvas / 自绘 UI 怎么办

无障碍树对 canvas 只给一个空节点（实测玩吧棋盘 `content-desc="俄罗斯方块主棋盘"`、
**子节点数 = 0**）。Jev 不看图，所以在这种屏幕上它只能瞎猜。

**解法不是「让 Jev 看图」，而是把像素结构化成文本再问它**（TypeSafe 官方游戏 demo 同思路）：

```
scripts/canvas_grid.py   零依赖 PNG 解码 → 10×20 字符矩阵 + 代码算好的数值特征
```

实测对比（同一屏，目标「把方块放到安全的列」）：

| 输入 | 结果 |
|---|---|
| 只有 a11y 树（没有棋盘） | 选 col 1，conf **0.40** —— 错，且接近均匀分布 |
| 加上数字化棋盘 + 特征 | 选 col 10，conf **0.70** —— 正是唯一最低列 |

数字化结果经 vision 独立核对，**逐格吻合**，但耗时 ~0.1s、零成本（vision 要 ~30s）。

### 高频操作的正确姿势
不要逐帧问模型。**代码枚举所有 (列, 旋转) 落点并算启发式分，只把 Top-K 交给 Jev 选**
—— 算分是代码的活，选哪个是 Jev 的活。见 `scripts/play_tetris.py`。

---

## 真机验证过的闭环

从**桌面**开始、全程无人工介入（Android 三星 SM-G9910）：

```
蓝牙任务：设置首页 → 连接页 → 蓝牙页
  step1 next=e2 conf=0.99  (投机判定无危险控件 → 免复核，直接执行)
  step2 next=e4 conf=0.99  verify safe=0.98 destroys=0.03 → AUTO
  step3 complete=0.89 → ✅ 完成
  3 步 / 4 次 jev 调用 / 16.4s；蓝牙状态全程未被误改
```

```
玩吧任务：桌面(17图标) → 抽屉搜索 → 打开 app → 俄罗斯方块 → 经典模式 → 游戏中
  玩吧 0.85 → 游戏卡片 0.98 → 经典方块 1.00
```

安全闸双向验证（玩吧存档弹窗）：

| 候选 | destroys_data | 裁决 |
|---|---|---|
| 继续上次 | **0.04** | AUTO（放行） |
| 重新开始 | **0.94** | CONFIRM（拦截，不删档） |

---

## 已知限制（诚实清单）

- **`play_tetris.py` 的旋转是用宽度近似形态**，没解析七种真实方块。能跑通闭环、
  能选对列（加旋转后存活从 3 手→5 手），但玩不精。
- 经典模式没有硬降键，靠连点「加速」压落，落点不如硬降精确。
- iOS 真机 WDA 因签名证书冲突未跑通（见下），iOS 路径在**模拟器**上验证通过。
- 阈值是在这几个场景上调的，换场景**必须重新校准**（先标 20 条已知样本）。

### iOS 真机 WDA 签名问题
钥匙串里有**两张同名** `Apple Development: li yilin (9QJ9KZ63P3)` 证书，
xcodebuild 挑中的那张不在 provisioning profile 里 → `code 65`。
解法：删掉旧的那张（保留 `notAfter` 较晚的），或在 Xcode 里手动指定。
模拟器不需要签名，可直接用。

---

## 成本

单次 reflex 约 400~1300 输入 token = **$0.00002~0.00006**，0.7~1s。
一个 50 步任务每步一次反射，总成本约 **半美分**。

---

## 文件

| 文件 | 作用 |
|---|---|
| `scripts/observe.py` | 三端观察适配器（Android/iOS/Chrome → 统一 state） |
| `scripts/jev_reflex.py` | 五个决策原语（reflex / guard / pick_dismiss） |
| `scripts/loop.py` | System-1 + System-2 分层闭环，带安全闸 |
| `scripts/canvas_grid.py` | canvas 数字化（零依赖 PNG 解码 + 特征提取） |
| `scripts/play_tetris.py` | 高频游戏示例：枚举 → Top-K → Jev 选 |

## License

MIT

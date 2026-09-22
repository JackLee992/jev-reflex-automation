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
┌── 决策层 jev_reflex.py（System-1，~0.8s，$0.000005）──────┐
│  reflex()  一次请求并行问 6 个：                            │
│    blocked / complete / loading / injection / risk / next  │
│  guard()   动作前护栏：safe / reversible / in_scope        │
│  pick_dismiss()  弹窗分类 + 毁数据探针                      │
└───────────────────────────────────────────────────────────┘
                          ↓
┌── 执行层 loop.py ──────────────────────────────────────────┐
│  AUTO → 执行   CONFIRM → 问人   BLOCK → 拦截                │
│  置信度低 / 要多步规划 → 升级 System-2（大模型）              │
└───────────────────────────────────────────────────────────┘
```

**分工原则**：Jev 只回答「哪个更好 / 是不是」，**算术和枚举留在代码里**
（它明确不擅长计数、日期、多跳推理）。

---

## 五个决策原语

| 原语 | 回答 | 真机实测 |
|---|---|---|
| `blocked` | 有弹窗/登录墙/验证码挡路吗？ | cookie 横幅 **0.99**、登录墙 **0.98**、真实确认弹窗 **0.87** |
| `complete` | 目标达成没有？ | 消歧问法 **0.93**，泛化问法只有 0.66（见教训 3） |
| `next` | 下一步操作哪个元素**或动作**？ | 蓝牙行 **0.98**、游戏卡片 **0.98**、经典模式 **1.00** |
| `guard` | 这个动作安全吗？可逆吗？在目标范围内吗？ | 进页面 safe **0.97**→AUTO；切开关 **0.68**→CONFIRM |
| `injection` | 屏幕文字在试图给 agent 下指令吗？ | 注入文本 **0.98** |

`reflex()` 把前面全部塞进**一次请求**并行问。实测：3 问 642 token / 1731ms，
7 问 750 token / 895ms —— **多问 4 个问题几乎不要钱也不要时间**，所以该问的一次问全。

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

### 5. 可点击容器要分两种
- 无名字的布局容器（抽屉 `gesture_control_layout` 包 7 个可点子节点）→ **丢掉**，
  否则它的 subtree 名字把整屏文字拼进来，conf 0.63。
- 有名字的卡片（游戏卡片，子节点只是「收藏」星标）→ **保留**，
  否则只剩小星标按钮，guard 会合理地把它判成越权而拒绝。
修好后同一目标 **0.98**。

### 6. 可点击的 TextView/ImageView 是真控件
安卓桌面图标就是 `clickable=true` 的 TextView。按类名映射成 "Text" 丢掉的话，
**整个桌面一个图标都抓不到**。

### 7. Jev 不做指代消解，问题必须字面自足
问「`choice` 选中的那个选项会不会毁数据」→ 继续上次/重新开始都得 0.5~0.68（没用）。
把控件**具名**塞进 state 再问 → 继续上次 **0.04**、重新开始 **0.94**（完全可用）。
所以 `pick_dismiss` 用**两次调用**：先选，再具名复核。

### 8. 弹窗要先分类，再决定怎么处置
「最小授权关闭」的逻辑用在存档弹窗上会选中【重新开始】——**删档**。
现在先分 `obstacle / decision / credential`，再配一个毁数据探针 + 硬闸。

### 9. canvas 数字化必须配 a11y 哨兵
游戏结束遮罩盖住棋盘后，`digitize` 仍在读被虚化的残影，产出「方块悬空 + holes=48」
的垃圾数据，而循环还在照着它点。a11y 树能看到遮罩节点 → 用它当哨兵。

### 10. 棋盘几何每帧重取；HUD 要遮
- 模式切换后 canvas 从 DOM 消失，沿用缓存 bounds 会对着空白采样。
- 「下一块」预览面板**画在棋盘右上角之上**，会被当成盘面方块（假 'R'，heights[9]=18）。
  → 把落在棋盘内的具名节点当 HUD 遮罩置空。

### 11. 不能假设棋盘底色是深色
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

## 已知限制（诚实清单）

- **`play_tetris.py` 目前不会旋转方块**，只做「横移 + 落下」。所以它能跑通闭环、
  能选对列，但玩不好——长条和 T 形需要转向才能塞进空位。这是下一步要补的。
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
